from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from contextlib import contextmanager

from fastapi.testclient import TestClient
from langchain.agents.middleware.internal_call_transformer import internal_call_metadata
from sqlalchemy.exc import OperationalError
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import app.tasks.drafts as drafts
import app.workspace.service as service_module
from app.auth import issue_token
from app.main import app
from app.persistence.models import AgentTask, Message, MessageDraft, db
from app.runtime.harness import build_harness
from app.runtime.helpers.coder import coder
from app.runtime.tools import hosted
from app.tasks import enqueue, hand_off, update_task
from app.tasks.drafts import Draft, answer_text, save_draft
from app.tasks.worker import complete, execute, process
from test_tasks import ScriptedModel, job, teammate  # noqa: F401  (job is a fixture)

CODE_BLOCKS = [
    {"type": "text", "text": "Let me compute that."},
    {"type": "code_interpreter_call", "id": "ci-1", "status": "completed", "code": "2340 * 0.17",
     "outputs": [{"type": "logs", "logs": "397.8"}]},
    {"type": "text", "text": "17% of 2,340 is **397.8**."},
]


class StreamingModel(ScriptedModel):
    """Streams each scripted reply word by word, then its tool calls, like a provider stream."""

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self._generate(messages, stop, run_manager, **kwargs).generations[0].message
        words = reply.content.split(" ") if isinstance(reply.content, str) and reply.content else []
        for i, word in enumerate(words):
            chunk = AIMessageChunk(content=word + (" " if i < len(words) - 1 else ""))
            if run_manager:
                run_manager.on_llm_new_token(chunk.content, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)
        if reply.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                for i, c in enumerate(reply.tool_calls)]))


class BindingModel(ScriptedModel):
    bound: list = []

    def bind_tools(self, tools, **kwargs):
        self.bound.append(tools)
        return self


def recording(monkeypatch) -> list[str]:
    writes: list[str] = []
    original = drafts.save_draft

    def record(task, content):
        writes.append(content)
        return original(task, content)
    monkeypatch.setattr(drafts, "save_draft", record)
    monkeypatch.setattr(drafts, "DRAFT_INTERVAL", 0)
    return writes


def run(task, saver, model):
    return execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw))


def draft_row(task_id):
    with db() as session:
        return session.get(MessageDraft, task_id)


def test_draft_streams_only_the_final_answer_and_is_removed_on_completion(tmp_path, monkeypatch, job):
    task, _, _ = job
    writes = recording(monkeypatch)
    model = StreamingModel(responses=[
        AIMessage(content="Let me save a note", tool_calls=[{
            "name": "write_file", "args": {"file_path": "/note.txt", "content": "x"}, "id": "file-1"}]),
        AIMessage(content="The final answer is four"),
    ])

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "drafts.db")) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            return await run(task, saver, model)
    result = asyncio.run(scenario())
    assert result == "The final answer is four"
    assert "Let me save a note" in writes  # the thinking-out-loud text streamed...
    reset = writes.index("Let me save a note") + 1
    assert writes[reset] == ""  # ...and was dropped when the tool call started
    assert all("Let me" not in w for w in writes[reset:])
    assert writes[-1] == "The final answer is four"
    assert draft_row(task.id).content == "The final answer is four"
    complete(task, result)
    assert draft_row(task.id) is None


def test_report_back_tasks_do_not_stream(tmp_path, monkeypatch, job):
    task, _, _ = job
    writer = teammate()
    child_id = hand_off(task, teammate=writer.name, request="Draft it", report_back=True, call_id="no-draft")["task_id"]
    with db() as session:
        child = session.get(AgentTask, child_id)
    writes = recording(monkeypatch)

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "report.db")) as saver:
            await saver.setup()
            update_task(child.id, status="running")
            return await run(child, saver, StreamingModel(responses=[AIMessage(content="Here is the draft")]))
    assert asyncio.run(scenario()) == "Here is the draft"
    assert writes == [] and draft_row(child.id) is None


def test_failed_and_cancelled_tasks_drop_their_draft(job):
    task, _, _ = job

    async def fails(task, saver):
        save_draft(task, "Half an ans")
        raise RuntimeError("provider exploded with secret details")
    update_task(task.id, status="running")
    asyncio.run(process(task, None, executor=fails))
    with db() as session:
        failed = session.get(AgentTask, task.id)
    assert failed.status == "failed" and failed.error == "RuntimeError"
    assert draft_row(task.id) is None

    async def slow(task, saver):
        save_draft(task, "Still writing")
        update_task(task.id, cancel_requested=True)
        await asyncio.sleep(30)
    update_task(task.id, status="running", cancel_requested=False)
    asyncio.run(process(task, None, executor=slow))
    with db() as session:
        assert session.get(AgentTask, task.id).status == "cancelled"
    assert draft_row(task.id) is None


def test_a_locked_database_skips_draft_writes_without_failing_the_task(tmp_path, monkeypatch, job):
    task, _, _ = job
    attempts = []

    class LockedSession:
        def __init__(self, session):
            self.session = session

        def merge(self, *args, **kwargs):
            attempts.append(1)
            raise OperationalError("INSERT INTO messagedraft", {}, sqlite3.OperationalError("database is locked"))

        def __getattr__(self, name):
            return getattr(self.session, name)

    @contextmanager
    def locked_db():
        with db() as session:
            yield LockedSession(session)
    monkeypatch.setattr(drafts, "db", locked_db)
    monkeypatch.setattr(drafts, "DRAFT_INTERVAL", 0)
    model = StreamingModel(responses=[AIMessage(content="The answer streams while the database is busy")])

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "locked.db")) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            await process(task, saver, executor=lambda t, s: run(t, s, model))
    asyncio.run(scenario())
    with db() as session:
        done = session.get(AgentTask, task.id)
        reply = session.get(Message, done.reply_message_id)
    assert done.status == "completed" and done.error is None
    assert reply.content == "The answer streams while the database is busy"
    assert len(attempts) == 1  # backed off instead of retrying every token against a locked database


def test_summarization_output_never_reaches_the_draft(job):
    task, _, _ = job
    draft = Draft(task, enabled=True)
    draft.feed((), AIMessageChunk(content="Partial answer"), {"langgraph_node": "model"})
    summary = {"langgraph_node": "model", "lc_source": "summarization"}
    draft.feed((), AIMessageChunk(content=" SUMMARY with untrusted page text"), summary)
    draft.feed((), AIMessageChunk(content=" internal call"), {"langgraph_node": "model", **internal_call_metadata()})
    assert draft.text == "Partial answer"
    assert draft_row(task.id).content == "Partial answer"


def test_drafts_reach_websocket_and_clear_when_the_reply_lands(job):
    task, _, user = job
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={issue_token(user.id)}") as ws:
            save_draft(task, "Hello wor")
            event = ws.receive_json()
            while not (event["type"] == "draft" and event["task_id"] == task.id):
                event = ws.receive_json()
            assert event == {"type": "draft", "task_id": task.id, "channel_id": task.channel_id,
                             "agent_id": task.agent_id, "content": "Hello wor"}
            update_task(task.id, status="running")
            complete(task, "Hello world")
            seen = []
            while len(seen) < 2:
                event = ws.receive_json()
                if event["type"] == "message" and event["message"]["content"] == "Hello world":
                    seen.append("message")
                if event["type"] == "draft" and event["task_id"] == task.id:
                    assert event["content"] == ""
                    seen.append("cleared")
            assert seen == ["message", "cleared"]


def test_drafts_stream_appended_text_and_resync_in_full(job, monkeypatch):
    task, _, user = job
    monkeypatch.setattr(service_module, "DRAFT_RESYNC", 2)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={issue_token(user.id)}") as ws:
            def written(content: str) -> dict:
                save_draft(task, content)
                event = ws.receive_json()
                while not (event["type"] in ("draft", "draft_append") and event["task_id"] == task.id):
                    event = ws.receive_json()
                return event

            base = {"task_id": task.id, "channel_id": task.channel_id, "agent_id": task.agent_id}
            assert written("😀 Hello") == {"type": "draft", **base, "content": "😀 Hello"}
            # The emoji is two UTF-16 units in the browser, so the offset is 8, not Python's 7.
            assert written("😀 Hello wor") == {"type": "draft_append", **base, "offset": 8, "text": " wor"}
            assert written("😀 Hello world") == {"type": "draft_append", **base, "offset": 12, "text": "ld"}
            assert written("😀 Hello world!") == {"type": "draft", **base, "content": "😀 Hello world!"}
            assert written("Rewritten") == {"type": "draft", **base, "content": "Rewritten"}


def test_code_execution_text_before_the_tool_is_not_part_of_the_answer(job):
    assert answer_text(AIMessage(content=CODE_BLOCKS)) == "17% of 2,340 is **397.8**."
    assert answer_text(AIMessage(content="Plain answer")) == "Plain answer"
    task, _, _ = job
    draft = Draft(task, enabled=True)
    draft.feed((), AIMessageChunk(content=[{"type": "text", "text": "Let me compute", "index": 0}]),
               {"langgraph_node": "model"})
    draft.feed((), AIMessageChunk(content=[{"type": "code_interpreter_call", "id": "ci-1",
                                            "status": "in_progress", "index": 1}]), {"langgraph_node": "model"})
    assert draft.text == "" and draft_row(task.id) is None
    draft.feed(("tools:helper",), AIMessageChunk(content="subagent text"), {"langgraph_node": "model"})
    draft.feed((), AIMessageChunk(content="17% is 397.8"), {"langgraph_node": "model"})
    assert draft.text == "17% is 397.8"


def test_hidden_citation_markup_becomes_links_or_disappears(job):
    cited = "Overcast. \ue200cite\ue202https://exa.ai/weather?d=1\ue202turn0search0\ue201"
    assert answer_text(AIMessage(content=cited)) == "Overcast. ([exa.ai](https://exa.ai/weather?d=1))"
    assert answer_text(AIMessage(content="A. \ue200cite\ue202turn0search0\ue201 B.")) == "A. B."
    task, _, _ = job
    draft = Draft(task, enabled=True)
    draft.feed((), AIMessageChunk(content="Overcast. \ue200cite\ue202https://exa.ai/wea"), {"langgraph_node": "model"})
    draft.flush(force=True)
    assert draft_row(task.id).content == "Overcast. "


def test_openai_code_interpreter_is_bound_when_enabled(tmp_path, monkeypatch, job):
    assert hosted.hosted_tools() == [hosted.CODE_EXECUTION]
    by_name = {"code_interpreter": hosted.CODE_EXECUTION, "run_python": object(), "sandbox_exec": object(),
               "sandbox_write_file": object(), "sandbox_export_file": object(), "request_approval": object(),
               "channel_history": object(), "recall_memory": object()}
    assert coder(by_name)["tools"][0] == hosted.CODE_EXECUTION
    assert hosted.CODE_EXECUTION not in coder({k: v for k, v in by_name.items() if k != "code_interpreter"})["tools"]

    task, agent, _ = job

    def bound_tools(name):
        model = BindingModel(responses=[AIMessage(content="397.8")], bound=[])

        async def scenario():
            async with AsyncSqliteSaver.from_conn_string(str(tmp_path / f"{name}.db")) as saver:
                await saver.setup()
                attempt = enqueue(agent_id=task.agent_id, channel_id=task.channel_id, room_id=task.room_id,
                                  source_id=f"hosted-{name}", prompt="What is 17% of 2,340?")
                return await run(attempt, saver, model)
        assert asyncio.run(scenario()) == "397.8"
        return model.bound

    def names(tools):
        return {t.get("name", t.get("type")) if isinstance(t, dict) else t.name for t in tools}

    parent = bound_tools("on")[0]  # quick sums must not boot the E2B computer
    assert "code_interpreter" in names(parent) and "run_python" not in names(parent)
    monkeypatch.setattr(hosted, "settings", dataclasses.replace(hosted.settings, fast_code_execution=False))
    assert hosted.hosted_tools() == []
    parent = bound_tools("off")[0]
    assert "code_interpreter" not in names(parent) and "run_python" in names(parent)
