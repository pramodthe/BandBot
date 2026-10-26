"""Checkpoint-independent model dispatch journal.

Native SDK retries are disabled. A completed response can be replayed locally;
an interrupted request requires an explicit new task instead of a duplicate call.
"""
import hashlib
import json
from typing import Any
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import messages_to_dict, messages_from_dict, message_chunk_to_message, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy.exc import IntegrityError
from app.persistence.models import db
from app.providers.models import ProviderOperation
from app.providers.contracts import CONFIRMED_REJECTIONS, ProviderError
from app.providers.connections import resolve

class DurableModel(BaseChatModel):
    inner: Any
    binding: dict
    task_id: str
    service: str = "llm"
    tool_contract: list = []

    @property
    def _llm_type(self):
        return "band-provider"

    @property
    def _identifying_params(self):
        return {"model_name": self.binding["model"], "provider": self.binding["provider"]}

    def bind_tools(self, tools, **kwargs):
        contract = [convert_to_openai_tool(t) for t in tools]
        return self.model_copy(update={"inner": self.inner.bind_tools(tools, **kwargs), "tool_contract": contract})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("Provider models run asynchronously in the worker")

    def start(self, messages, stop):
        resolve(self.binding, self.service)
        body = {"messages": messages_to_dict(messages), "tools": self.tool_contract, "binding": self.binding, "stop": stop}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        identity = "model:" + self.task_id + ":" + self.service + ":" + digest
        with db() as session:
            existing = session.get(ProviderOperation, identity)
            if existing:
                if existing.status == "completed":
                    return identity, messages_from_dict(existing.result["messages"])[0]
                raise ProviderError(self.service, "uncertain_model_request", connection_id=self.binding["connection_id"],
                    detail="A previous model request was interrupted before its outcome was confirmed. Its checkpoint is preserved. Start a linked new task after reviewing the provider; this request will not be sent twice.")
            session.add(ProviderOperation(id=identity, task_id=self.task_id, service=self.service, binding=self.binding, status="running"))
            try:
                session.commit()
            except IntegrityError:
                raise ProviderError(self.service, "model_request_in_progress") from None
        return identity, None

    def finish(self, identity, response=None, error=None):
        with db() as session:
            row = session.get(ProviderOperation, identity)
            row.status = "completed" if response is not None else "uncertain"
            if response is not None:
                if not response.id:
                    response.id = identity
                row.result = {"messages": messages_to_dict([response])}
            else:
                # A confirmed rejection is safe to try again with the same binding.
                if getattr(error, "status_code", None) in CONFIRMED_REJECTIONS:
                    session.delete(row)
                    session.commit()
                    return
                row.result = {"error": "Model response was not confirmed. No automatic retry was sent."}
            session.add(row)
            session.commit()

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        identity, response = self.start(messages, stop)
        if response is None:
            try:
                response = await self.inner.ainvoke(messages, stop=stop, **kwargs)
                self.finish(identity, response)
            except BaseException as error:
                self.finish(identity, error=error)
                raise
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        identity, response = self.start(messages, stop)
        if response is not None:
            value = response.model_dump(exclude={"type"})
            yield ChatGenerationChunk(message=AIMessageChunk(**value))
            return
        whole = None
        try:
            async for chunk in self.inner.astream(messages, stop=stop, **kwargs):
                if not isinstance(chunk, AIMessageChunk):
                    chunk = AIMessageChunk(**chunk.model_dump(exclude={"type"}))
                whole = chunk if whole is None else whole + chunk
                yield ChatGenerationChunk(message=chunk)
            if whole is None:
                raise ValueError("Empty model stream")
            self.finish(identity, message_chunk_to_message(whole))
        except BaseException as error:
            self.finish(identity, error=error)
            raise
