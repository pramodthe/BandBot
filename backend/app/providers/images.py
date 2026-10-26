"""Paid image requests are journaled before dispatch and never blindly replayed."""
import asyncio
import base64
import hashlib
import io
from uuid import NAMESPACE_URL, uuid4, uuid5

from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.persistence.models import (db, AgentTask, Document, TaskDocument, TaskDocumentSource,
                                    MessageComponent, ImageGeneration, ImageGenerationDisplay)
from app.providers.models import ProviderOperation
from app.providers.connections import resolve
from app.providers.contracts import ProviderError


async def request_image(connection, model, prompt, *, size="1024x1024", quality="medium",
                        transparent_background=False, safety_identifier=""):
    if connection.provider == "openai":
        from openai import AsyncOpenAI
        async with AsyncOpenAI(api_key=connection.secret["api_key"], base_url="https://api.openai.com/v1",
                               max_retries=0, timeout=240) as sdk:
            result = await sdk.images.generate(model=model, prompt=prompt, n=1, size=size, quality=quality,
                background="transparent" if transparent_background else "opaque", output_format="png",
                extra_body={"safety_identifier": safety_identifier})
            if not result.data or not result.data[0].b64_json:
                raise ValueError("No image bytes returned")
            return base64.b64decode(result.data[0].b64_json, validate=True), result.usage.model_dump(mode="json") if result.usage else {}
    if connection.provider == "gemini":
        from google import genai
        from google.genai import types
        async with genai.Client(api_key=connection.secret["api_key"], vertexai=False,
                http_options=types.HttpOptions(base_url="https://generativelanguage.googleapis.com", timeout=180000,
                    retry_options=types.HttpRetryOptions(attempts=1))).aio as sdk:
            result = await sdk.models.generate_content(model=model, contents=prompt,
                config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]))
            for part in result.parts or []:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    return part.inline_data.data, result.usage_metadata.model_dump(mode="json") if result.usage_metadata else {}
        raise ValueError("The model did not return an image")
    raise ProviderError("images", "unsupported_provider")


def require_active_task(session, task):
    from app import documents
    documents.agent_authorize(session, task)
    stored = session.get(AgentTask, task.id)
    if stored is None or stored.cancel_requested or stored.reply_message_id is not None or stored.status in {"cancelled", "failed"}:
        raise ProviderError("images", "task_stopped", detail="This task is cancelled or finished. A new image needs a new human request.")


async def generate(task, binding, prompt, call_id, *, title="Generated image", size="1024x1024",
                   quality="medium", transparent_background=False):
    from app import documents
    from app.tasks.usage import over_budget, BudgetExceeded
    if not prompt.strip() or not title.strip() or len(title) > 100:
        raise ValueError("Provide a nonempty prompt and a title of up to 100 characters")
    if size not in {"1024x1024", "1536x1024", "1024x1536"} or quality not in {"low", "medium", "high"}:
        raise ValueError("Unsupported image options")
    operation_id = "image:" + hashlib.sha256((task.id + ":" + call_id).encode()).hexdigest()
    document_id = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
    # Old local-preview checkpoints must not dispatch an already-paid image again.
    legacy_id = uuid5(NAMESPACE_URL, f"band:image:{task.id}:{call_id}").hex
    with db() as session:
        documents.agent_authorize(session, task)
        legacy = session.get(ImageGeneration, legacy_id)
        if legacy:
            doc = session.get(Document, legacy.document_id)
            if legacy.status == "completed" and doc and doc.status not in {"deleting", "deleted"}:
                return {"status": "completed", "document_id": doc.id, "name": doc.name,
                        "url": f"/api/documents/{doc.id}/download", "model": legacy.model}
            raise ProviderError("images", "uncertain_request",
                detail="This image request was already dispatched before the upgrade. Do not regenerate it automatically.")
        if session.exec(select(ImageGeneration).where(ImageGeneration.task_id == task.id,
                ImageGeneration.status != "completed")).first():
            raise ProviderError("images", "uncertain_request",
                detail="An earlier image request in this task needs review before a new human request.")
    connection = resolve(binding, "images")
    if connection.provider != "openai" and (size != "1024x1024" or quality != "medium" or transparent_background):
        raise ProviderError("images", "unsupported_options", detail="Custom image size, quality and transparency require an OpenAI image connection.")
    with db() as session:
        documents.agent_authorize(session, task)
        existing = session.get(ProviderOperation, operation_id)
        if existing:
            doc = session.get(Document, document_id)
            if existing.status == "completed" and doc and doc.status == "ready":
                return existing.result
            raise ProviderError("images", "uncertain_request", connection_id=connection.id,
                detail="This image request was already dispatched. Its outcome needs review before a separate new request; retrying cannot send it again.")
        require_active_task(session, task)
        reason = over_budget(session, task.agent_id, task.channel_id)
        if reason:
            raise BudgetExceeded(reason)
        pending = session.exec(select(ProviderOperation).where(ProviderOperation.task_id == task.id,
            ProviderOperation.service == "images", ProviderOperation.status != "completed")).first()
        if pending:
            raise ProviderError("images", "uncertain_request", connection_id=connection.id,
                detail="Another image request in this task is pending or uncertain. A retry needs a new human request.")
        session.add(ProviderOperation(id=operation_id, task_id=task.id, service="images", binding=binding, status="running"))
        session.add(ImageGenerationDisplay(id=operation_id, title=title.strip(), size=size))
        try:
            session.commit()
        except IntegrityError:
            raise ProviderError("images", "request_in_progress") from None
    path = None
    temporary = None
    published = False
    try:
        data, usage = await request_image(connection, binding["model"], prompt.strip()[:24000], size=size, quality=quality,
            transparent_background=transparent_background,
            safety_identifier=hashlib.sha256(f"band:{task.channel_id}:{task.agent_id}".encode()).hexdigest())
        if usage:
            from app.providers.context import usage_scope
            from app.tasks.usage import record_usage
            from langchain_core.messages import AIMessage
            given = int(usage.get("input_tokens") or usage.get("prompt_token_count") or 0)
            output_tokens = int(usage.get("output_tokens") or (usage.get("candidates_token_count") or 0) + (usage.get("thoughts_token_count") or 0))
            with usage_scope(binding, "images"):
                record_usage(task, AIMessage(content="", id=operation_id, response_metadata={"model_name": binding["model"]},
                    usage_metadata={"input_tokens": given, "output_tokens": output_tokens, "total_tokens": given + output_tokens}), binding["model"])
        if len(data) > documents.LIMIT:
            raise ValueError("Generated image exceeds the document limit")
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            output = io.BytesIO()
            image.save(output, format="PNG")
            data = output.getvalue()
        if len(data) > documents.LIMIT:
            raise ValueError("Generated PNG exceeds the document limit")
        artifact = Document(id=document_id, channel_id=task.channel_id, uploaded_by=0,
            name="generated-image-" + document_id[:8] + ".png", media_type="image/png", size=len(data), status="ready")
        result = {"status": "completed", "document_id": document_id, "url": f"/api/documents/{document_id}/download", "name": artifact.name,
                  "provider": binding["provider"], "model": binding["model"], "usage": usage}
        with db() as session:
            require_active_task(session, task)
            if documents.database_files():
                documents.save_original(session, artifact, data)
            else:
                path = documents.path_for(artifact)
                temporary = path.with_name(f"{path.name}.{uuid4().hex}.pending")
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_bytes(data)
                temporary.replace(path)
            session.add(artifact)
            session.add(TaskDocument(task_id=task.id, document_id=document_id))
            session.add(TaskDocumentSource(task_id=task.id, document_id=document_id))
            session.add(MessageComponent(task_id=task.id, tool_call_id=call_id, kind="image",
                props={"documentId": document_id, "title": title.strip(), "model": binding["model"]}))
            row = session.get(ProviderOperation, operation_id)
            row.status = "completed"
            row.result = result
            session.add(row)
            session.commit()
            published = True
        return result
    except BaseException as error:
        with db() as session:
            row = session.get(ProviderOperation, operation_id)
            row.status = "uncertain"
            row.result = {"error": "Image generation was not confirmed. No automatic retry was sent."}
            session.add(row)
            session.commit()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ProviderError("images", "uncertain_request", connection_id=connection.id,
            detail="Image generation was not confirmed. Check the original provider before requesting another paid image.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if path is not None and not published:
            path.unlink(missing_ok=True)
