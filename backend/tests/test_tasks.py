from __future__ import annotations
import asyncio
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import Field
from sqlmodel import select
from app.runtime.tools import tools_for
from app.auth import encrypt_secret, issue_token
from app.runtime.harness import build_harness
from app.main import app
from app.persistence.models import AgentProfile, AgentTask, Channel, ChannelMember, DirectMessage, Message, User, db, init_db
from app.tasks import enqueue, recover_tasks, update_task
from app.tasks.worker import complete, execute, process


class ScriptedModel(BaseChatModel):
    """Run the actual Deep Agents graph and tools without provider credentials."""
    responses: list
    index: int = 0

    @property
    def _llm_type(self):
        return "offline-scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.responses[self.index]
        self.index += 1
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


@pytest.fixture
def job():
    init_db()
    key = uuid4().hex
    with db() as session:
        agent = AgentProfile(name=f"Agent-{key[:8]}", role="master", is_master=True,
                             band_agent_id=key, band_api_key_enc=encrypt_secret("offline"))
        channel = Channel(name=key, band_room_id=key)
        user = User(username=key, display_name="Tester", password_hash="unused")
        session.add_all([agent, channel, user])
        session.commit()
        session.refresh(agent)
        session.refresh(channel)
        session.refresh(user)
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.commit()
    task = enqueue(agent_id=agent.id, channel_id=channel.id, room_id=key,
                   source_id=key, prompt="Create a draft and verify it")
    return task, agent, user


def test_checkpoint_survives_failure_and_new_worker(tmp_path, job):
    task, _, _ = job
    first = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "write_file", "args": {"file_path": "/draft.txt", "content": "Verified draft"}, "id": "file-1"
    }]), RuntimeError("simulated worker failure")])
    async def scenario():
        path = str(tmp_path / "checkpoints.db")
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            with pytest.raises(RuntimeError, match="simulated"):
                await execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=first, **kw))
        recover_tasks()
        with db() as session:
            assert session.get(AgentTask, task.id).status == "queued"
        second = ScriptedModel(responses=[AIMessage(content="Finished draft")])
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            result = await execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=second, **kw))
            assert result == "Finished draft"
        with db() as session:
            assert session.get(AgentTask, task.id).artifacts == [{"path": "/draft.txt", "content": "Verified draft"}]
    asyncio.run(scenario())


def test_real_subagent_delegation(tmp_path, job):
    task, _, _ = job
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "args": {
            "subagent_type": "critic", "description": "Review: 2 + 2 = 4. Return a verdict."}, "id": "delegate-1"}]),
        AIMessage(content="The arithmetic is correct."),
        AIMessage(content="Reviewed: 2 + 2 = 4."),
    ])
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "subagents.db")) as saver:
            await saver.setup()
            result = await execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw))
            assert "Reviewed" in result
            graph = build_harness(task, job[1], saver, model=model)
            snapshot = await graph.aget_state({"configurable": {"thread_id": task.id}})
            messages = snapshot.values["messages"]
            assert any(getattr(m, "name", None) == "task" for m in messages)
    asyncio.run(scenario())


def test_memory_is_persistent_and_scoped(job):
    task, _, _ = job
    tools = {t.name: t for t in tools_for(task)}
    tools["remember"].invoke({"key": "style", "content": "Concise prose; source: channel request"})
    assert tools["recall_memory"].invoke({"query": "style"})[0]["source_task_id"] == task.id
    other = task.model_copy(update={"channel_id": task.channel_id + 10000})
    assert tools_for(other)[0].invoke({}) == []
    other = task.model_copy(update={"agent_id": task.agent_id + 10000})
    assert tools_for(other)[0].invoke({}) == []


def test_completion_is_idempotent_and_cancellation_wins(job):
    task, _, _ = job
    update_task(task.id, status="running")
    complete(task, "Answer")
    complete(task, "Answer")
    with db() as session:
        assert len(session.exec(select(Message).where(Message.band_message_id == f"task:{task.id}")).all()) == 1
    second = enqueue(agent_id=task.agent_id, channel_id=task.channel_id, room_id=task.room_id,
                     source_id="cancel", prompt="cancel me")
    update_task(second.id, status="running", cancel_requested=True)
    complete(second, "Must not appear")
    with db() as session:
        assert session.get(AgentTask, second.id).status == "cancelled"
        assert not session.exec(select(Message).where(Message.band_message_id == f"task:{second.id}")).first()


def test_task_api_auth_control_and_memory_deletion(job):
    task, _, user = job
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.get(f"/api/tasks/{task.id}").status_code == 401
        response = client.get(f"/api/channels/{task.channel_id}/tasks", headers=headers)
        assert response.status_code == 200
        assert "prompt" not in response.json()[0]
        assert client.post(f"/api/tasks/{task.id}/cancel", headers=headers).json()["status"] == "cancelled"
        assert client.post(f"/api/tasks/{task.id}/retry", headers=headers).json()["status"] == "queued"
        assert client.post(f"/api/tasks/{task.id}/retry", headers=headers).status_code == 409
        tools_for(task)[1].invoke({"key": "delete-me", "content": "temporary"})
        url = f"/api/channels/{task.channel_id}/agents/{task.agent_id}/memories"
        assert len(client.get(url, headers=headers).json()) == 1
        assert client.delete(url + "/delete-me", headers=headers).status_code == 200
        assert client.get(url, headers=headers).json() == []


def test_worker_cancels_active_execution(job):
    task, _, _ = job
    update_task(task.id, status="running", cancel_requested=True)
    async def slow(task, saver):
        await asyncio.sleep(30)
    asyncio.run(process(task, None, executor=slow))
    with db() as session:
        assert session.get(AgentTask, task.id).status == "cancelled"


def test_worker_shutdown_leaves_job_recoverable(job):
    task, _, _ = job
    update_task(task.id, status="running")
    async def slow(task, saver):
        await asyncio.sleep(30)
    async def scenario():
        running = asyncio.create_task(process(task, None, executor=slow))
        await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    asyncio.run(scenario())
    with db() as session:
        assert session.get(AgentTask, task.id).status == "running"
    recover_tasks()
    with db() as session:
        assert session.get(AgentTask, task.id).status == "queued"


def test_unknown_band_delivery_is_not_retried(job, monkeypatch):
    from types import SimpleNamespace
    import app.tasks.worker as worker
    task, _, _ = job
    update_task(task.id, status="running", reply_to="owner")
    complete(task, "Saved answer")
    with db() as session:
        task = session.get(AgentTask, task.id)
    sends = []
    async def failed_send(*args, **kwargs):
        sends.append(kwargs)
        raise TimeoutError("lost response")
    async def close(client):
        pass
    monkeypatch.setattr(worker, "AsyncRestClient", lambda **kwargs: SimpleNamespace(
        agent_api_messages=SimpleNamespace(create_agent_chat_message=failed_send)))
    monkeypatch.setattr(worker, "aclose_rest_client", close)
    async def scenario():
        await worker.deliver(task)
        await worker.deliver(task)  # Even a stale pending task cannot repeat the send.
    asyncio.run(scenario())
    assert len(sends) == 1
    assert sends[0]["request_options"]["max_retries"] == 0
    with db() as session:
        saved = session.get(AgentTask, task.id)
        assert saved.delivery_status == "unknown"
        assert saved.result == "Saved answer"


def test_task_controls_reject_nonmembers(job):
    task, _, _ = job
    with db() as session:
        user = User(username=uuid4().hex, display_name="Nonmember", password_hash="unused")
        session.add(user)
        session.commit()
        session.refresh(user)
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.post(f"/api/tasks/{task.id}/cancel", headers=headers).status_code == 403
        assert client.post(f"/api/tasks/{task.id}/retry", headers=headers).status_code == 403


def test_worker_timeout_preserves_failure_for_manual_resume(job, monkeypatch):
    from types import SimpleNamespace
    import app.tasks.worker as worker
    task, _, _ = job
    update_task(task.id, status="running")
    monkeypatch.setattr(worker, "settings", SimpleNamespace(task_timeout=0.01))
    async def slow(task, saver):
        await asyncio.sleep(30)
    asyncio.run(worker.process(task, None, executor=slow))
    with db() as session:
        saved = session.get(AgentTask, task.id)
        assert saved.status == "failed"
        assert saved.error == "Time limit reached"


def test_missing_optional_tools_report_unavailable(job, monkeypatch):
    from types import SimpleNamespace
    import app.runtime.tools.web as module
    task, _, _ = job
    monkeypatch.setattr(module, "settings", SimpleNamespace(exa_api_key=""))
    tools = {tool.name: tool for tool in tools_for(task)}
    async def scenario():
        assert "not configured" in (await tools["web_search"].ainvoke({"query": "test"}))["error"]
        assert "not configured" in (await tools["run_python"].ainvoke({"code": "print(2)"}))["error"]
    asyncio.run(scenario())


def test_worker_replies_reach_websocket_without_band(job):
    task, _, user = job
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={issue_token(user.id)}") as ws:
            update_task(task.id, status="running")
            complete(task, "Durable reply")
            event = ws.receive_json()
            while event["type"] != "message":
                event = ws.receive_json()
            assert event["message"]["content"] == "Durable reply"
            assert event["message"]["channel_id"] == task.channel_id


def test_local_mention_is_durable_before_band_and_echo_is_deduplicated(job):
    from types import SimpleNamespace
    from app.band.listener import QueuedAgentAdapter
    from app.workspace.service import AppService
    task, agent, user = job
    svc = AppService()
    async def scenario():
        posted = await svc.post_human_message(user, task.channel_id, f"@{agent.name} verify the result")
        key = f"{agent.id}:{task.room_id}:app-message-{posted['id']}"
        with db() as session:
            saved = session.exec(select(AgentTask).where(AgentTask.source_key == key)).one()
            assert saved.status == "queued"
            before = len(session.exec(select(AgentTask).where(AgentTask.channel_id == task.channel_id)).all())
        adapter = QueuedAgentAdapter(profile_id=agent.id, reply_to="owner")
        await adapter.on_message(SimpleNamespace(id="band-echo", sender_id="owner",
            content=f"**Tester:** @{agent.name} verify the result"), None, [], None, None,
            is_session_bootstrap=True, room_id=task.room_id)
        with db() as session:
            assert len(session.exec(select(AgentTask).where(AgentTask.channel_id == task.channel_id)).all()) == before
    asyncio.run(scenario())


def test_local_mention_carries_recent_channel_context(job):
    from app.workspace.service import AppService
    task, agent, user = job
    svc = AppService()
    async def scenario():
        await svc.post_human_message(user, task.channel_id, "Our app is called Streakly.")
        posted = await svc.post_human_message(user, task.channel_id, f"@{agent.name} make the name shorter")
        key = f"{agent.id}:{task.room_id}:app-message-{posted['id']}"
        with db() as session:
            saved = session.exec(select(AgentTask).where(AgentTask.source_key == key)).one()
        assert saved.context == "Tester: Our app is called Streakly."
        assert saved.prompt == f"**Tester:** @{agent.name} make the name shorter"
    asyncio.run(scenario())


def teammate(name_prefix="Writer"):
    key = uuid4().hex[:8]
    with db() as session:
        agent = AgentProfile(name=f"{name_prefix}-{key}", role="writer", band_agent_id=f"band-{key}",
                             band_api_key_enc=encrypt_secret("offline"))
        session.add(agent)
        session.commit()
        session.refresh(agent)
    return agent


def run_scripted(task, saver, responses):
    model = ScriptedModel(responses=responses)
    return execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw))


def test_visible_handoff_reports_back_to_requester(tmp_path, job):
    from app.persistence.models import TaskHandoff
    task, master, _ = job
    writer = teammate()
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "handoff.db")) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            status = await run_scripted(task, saver, [
                AIMessage(content="", tool_calls=[{"name": "ask_teammate", "id": "call-handoff-1", "args": {
                    "teammate": writer.name.upper(), "request": "Write a one-line tagline."}}]),
                AIMessage(content="Asked the writer for a tagline."),
            ])
            complete(task, status)
            with db() as session:
                # SQLite can reuse a deleted agent's id, so match the hand-off's own source key.
                child = session.exec(select(AgentTask).where(
                    AgentTask.source_key == f"{writer.id}:{task.room_id}:handoff-call-handoff-1")).one()
                request_msg = session.exec(select(Message).where(
                    Message.band_message_id == "handoff:call-handoff-1")).one()
                assert request_msg.author_id == master.id and not request_msg.to_humans
                assert request_msg.content == f"@{writer.name} Write a one-line tagline."
                assert request_msg.mentions == [{"type": "agent", "id": writer.id, "name": writer.name}]
                assert child.status == "queued" and child.reply_to == ""
                assert child.prompt == f"**{master.name}:** Write a one-line tagline."
                assert session.get(TaskHandoff, child.id).root_task_id == task.id
                assert session.get(ChannelMember, (task.channel_id, "agent", writer.id))

            update_task(child.id, status="running")
            answer = await run_scripted(child, saver, [AIMessage(content="Streaks that stick.")])
            complete(child, answer)
            with db() as session:
                reply = session.exec(select(Message).where(Message.band_message_id == f"task:{child.id}")).one()
                assert not reply.to_humans
                assert reply.mentions == [{"type": "agent", "id": master.id, "name": master.name}]
                follow_up = session.exec(select(AgentTask).where(
                    AgentTask.source_key == f"{master.id}:{task.room_id}:report-{child.id}")).one()
                assert "Streaks that stick." in follow_up.prompt and task.prompt in follow_up.prompt
                assert follow_up.reply_to == task.reply_to
                assert session.get(TaskHandoff, follow_up.id).kind == "report"
            # Replayed completion must not report twice.
            complete(child, answer)
            with db() as session:
                assert len(session.exec(select(AgentTask).where(
                    AgentTask.source_key.like(f"%:report-{child.id}"))).all()) == 1
    asyncio.run(scenario())


def test_handoff_is_idempotent_validated_and_bounded(job):
    from app.tasks import MAX_HANDOFFS, hand_off
    task, _, _ = job
    writer = teammate()
    first = hand_off(task, teammate=f"@{writer.name}", request="Draft it", report_back=True, call_id="same-call")
    again = hand_off(task, teammate=writer.name, request="Draft it", report_back=True, call_id="same-call")
    assert first["task_id"] == again["task_id"]
    with db() as session:
        assert len(session.exec(select(Message).where(Message.band_message_id == "handoff:same-call")).all()) == 1
    assert writer.name in hand_off(task, teammate="nobody", request="x", report_back=True, call_id="c")["error"]
    assert "1-8000" in hand_off(task, teammate=writer.name, request="  ", report_back=True, call_id="c")["error"]
    for n in range(MAX_HANDOFFS - 1):
        assert hand_off(task, teammate=writer.name, request="More", report_back=True, call_id=f"n{n}")["status"]
    assert "already used" in hand_off(task, teammate=writer.name, request="More", report_back=True,
                                      call_id="over")["error"]


def test_handoff_is_refused_in_a_direct_message(job):
    from app.tasks import hand_off
    task, _, user = job
    writer = teammate()
    with db() as session:
        session.add(DirectMessage(channel_id=task.channel_id, user_id=user.id, agent_id=task.agent_id))
        session.commit()
    assert "one-on-one" in hand_off(task, teammate=writer.name, request="Draft it", report_back=True,
                                    call_id="dm")["error"]
    with db() as session:
        assert session.get(ChannelMember, (task.channel_id, "agent", writer.id)) is None
        assert not session.exec(select(AgentTask).where(AgentTask.agent_id == writer.id)).all()


def test_handoff_without_report_back_answers_humans(job):
    from app.tasks import hand_off
    task, _, _ = job
    with db() as session:
        stored = session.get(AgentTask, task.id)
        stored.reply_to = "owner-band-id"
        session.add(stored)
        session.commit()
        session.refresh(stored)
    writer = teammate()
    child_id = hand_off(stored, teammate=writer.name, request="Tell them directly", report_back=False,
                        call_id="direct")["task_id"]
    with db() as session:
        child = session.get(AgentTask, child_id)
    assert child.reply_to == "owner-band-id"
    update_task(child.id, status="running")
    complete(child, "Hello humans")
    with db() as session:
        reply = session.exec(select(Message).where(Message.band_message_id == f"task:{child.id}")).one()
        assert reply.to_humans and reply.mentions == []
        assert not session.exec(select(AgentTask).where(AgentTask.source_key.like(f"%:report-{child.id}"))).first()


def test_progress_labels_are_readable():
    from app.tasks.worker import describe_call
    assert describe_call({"name": "fetch_page", "args": {}}) == "Reading a web page"
    assert describe_call({"name": "task", "args": {"subagent_type": "researcher"}}) == "Asking the researcher helper"
    assert describe_call({"name": "ask_teammate", "args": {"teammate": "Writer"}}) == "Handing off to Writer"
    assert describe_call({"name": "custom_tool", "args": {}}) == "Using custom_tool"


def test_handoffs_and_task_activity_reach_websocket(job):
    from app.tasks import hand_off
    task, _, user = job
    writer = teammate()
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={issue_token(user.id)}") as ws:
            child_id = hand_off(task, teammate=writer.name, request="Live check", report_back=True,
                                call_id="live")["task_id"]
            seen_message = seen_activity = seen_member = False
            while not (seen_message and seen_activity and seen_member):
                event = ws.receive_json()
                if event["type"] == "channel" and event["channel"]["id"] == task.channel_id:
                    seen_member = seen_member or writer.id in event["channel"]["agent_ids"]
                if event["type"] == "message" and event["message"]["content"] == f"@{writer.name} Live check":
                    assert event["message"]["handoff"] is True and event["message"]["approval"] is None
                    seen_message = True
                if event["type"] == "task_activity" and event["task"]["id"] == child_id:
                    assert event["task"]["status"] == "queued" and event["task"]["agent_id"] == writer.id
                    seen_activity = True
            update_task(child_id, status="cancelled")
            event = ws.receive_json()
            while not (event["type"] == "task_activity" and event["task"]["id"] == child_id):
                event = ws.receive_json()
            assert event["task"]["status"] == "done"


def isolate_queue(keep: set[str]) -> None:
    """The suite shares one database; park other tests' leftover queued jobs."""
    with db() as session:
        for leftover in session.exec(select(AgentTask).where(AgentTask.status == "queued")).all():
            if leftover.id not in keep:
                leftover.status = "cancelled"
                session.add(leftover)
        session.commit()


def test_next_task_keeps_each_agent_in_order_per_channel(job):
    from datetime import datetime, timezone
    from app.tasks import next_task
    task, master, _ = job
    writer = teammate()
    second = enqueue(agent_id=master.id, channel_id=task.channel_id, room_id=task.room_id,
                     source_id="second", prompt="Second")
    other = enqueue(agent_id=writer.id, channel_id=task.channel_id, room_id=task.room_id,
                    source_id="other", prompt="Other")
    for n, item in enumerate((task, second, other)):
        update_task(item.id, created_at=datetime(2000, 1, 1, 0, 0, n, tzinfo=timezone.utc))
    isolate_queue({task.id, second.id, other.id})
    assert next_task().id == task.id
    assert next_task().id == other.id  # Master is busy in this channel; Writer is not.
    assert next_task() is None
    update_task(task.id, status="completed")
    assert next_task().id == second.id


def test_run_queue_overlaps_agents_and_respects_limits(job):
    from app.tasks.worker import run_queue
    task, master, _ = job
    writer = teammate()
    second = enqueue(agent_id=master.id, channel_id=task.channel_id, room_id=task.room_id,
                     source_id="second", prompt="Second")
    other = enqueue(agent_id=writer.id, channel_id=task.channel_id, room_id=task.room_id,
                    source_id="other", prompt="Other")
    ids = {task.id, second.id, other.id}

    def scenario(concurrency):
        isolate_queue(ids)
        active, order, peak = set(), [], [0]
        async def fake(item, saver):
            pair = (item.agent_id, item.channel_id)
            assert pair not in active, "an agent ran twice at once in one channel"
            active.add(pair)
            order.append(item.id)
            peak[0] = max(peak[0], len(active))
            await asyncio.sleep(0.2)
            active.discard(pair)
            return f"Done {item.prompt}"
        asyncio.run(run_queue(None, concurrency=concurrency, executor=fake, stop_when_idle=True))
        return order, peak[0]

    order, peak = scenario(4)
    assert peak == 2 and order.index(second.id) > order.index(task.id)
    with db() as session:
        for task_id in ids:
            done = session.get(AgentTask, task_id)
            assert done.status == "completed" and done.delivery_status == "local_only"
    for task_id in ids:
        update_task(task_id, status="queued", delivery_status="pending", result="")
    with db() as session:
        for message in session.exec(select(Message).where(Message.band_message_id.in_(
                [f"task:{task_id}" for task_id in ids]))).all():
            session.delete(message)
        session.commit()
    assert scenario(1)[1] == 1


def test_report_back_mentions_teammates_still_working(job):
    from app.tasks import hand_off
    task, master, _ = job
    task = update_task(task.id, reply_to="owner-band-id")
    researcher, writer = teammate("Researcher"), teammate()
    first = hand_off(task, teammate=researcher.name, request="Find facts", report_back=True, call_id="r1")["task_id"]
    second = hand_off(task, teammate=writer.name, request="Draft copy", report_back=True, call_id="w1")["task_id"]
    update_task(first, status="running")
    with db() as session:
        child = session.get(AgentTask, first)
    complete(child, "Facts found")
    with db() as session:
        follow_up = session.exec(select(AgentTask).where(
            AgentTask.source_key == f"{master.id}:{task.room_id}:report-{first}")).one()
    assert f"Still working on your other hand-offs: {writer.name}." in follow_up.prompt
    assert follow_up.reply_to == ""  # The interim status is not delivered to Band.
    update_task(follow_up.id, status="running")
    complete(follow_up, "Waiting on Writer.")
    with db() as session:
        interim = session.exec(select(Message).where(Message.band_message_id == f"task:{follow_up.id}")).one()
    assert not interim.to_humans
    update_task(second, status="running")
    with db() as session:
        writer_task = session.get(AgentTask, second)
    complete(writer_task, "Copy drafted")
    with db() as session:
        final = session.exec(select(AgentTask).where(
            AgentTask.source_key == f"{master.id}:{task.room_id}:report-{second}")).one()
    assert "Still working" not in final.prompt and final.reply_to == "owner-band-id"
    update_task(final.id, status="running")
    complete(final, "Here is the combined answer.")
    with db() as session:
        answer = session.exec(select(Message).where(Message.band_message_id == f"task:{final.id}")).one()
    assert answer.to_humans


class RecordingModel(ScriptedModel):
    """Also records each prompt, to prove a resumed graph sees the human decision."""
    seen: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def run_attempt(tmp_path, task, model):
    """One worker attempt with a fresh checkpoint connection, as after a restart."""
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "approvals.db")) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            await process(task, saver, executor=lambda t, s: execute(
                t, s, factory=lambda t2, p, c, **kw: build_harness(t2, p, c, model=model, **kw)))
    asyncio.run(scenario())
    with db() as session:
        return session.get(AgentTask, task.id)


def approval_call(call_id="approve-1"):
    return AIMessage(content="", tool_calls=[{"name": "request_approval", "id": call_id, "args": {
        "action": "Submit the signup form on example.com", "details": "Email: test@example.com"}}])


def tool_results(model):
    return [m.content for m in model.seen[0] if getattr(m, "type", "") == "tool"]


def test_approval_pauses_task_and_resumes_after_api_decision(tmp_path, job):
    from app.persistence.models import TaskApproval
    task, _, user = job
    paused = run_attempt(tmp_path, task, ScriptedModel(responses=[approval_call()]))
    assert paused.status == "waiting" and paused.progress == "Waiting for approval"
    with db() as session:
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
        notice = session.exec(select(Message).where(Message.band_message_id == f"approval:{approval.id}")).one()
    assert approval.action == "Submit the signup form on example.com" and approval.status == "pending"
    assert notice.to_humans and "Approval needed" in notice.content and "test@example.com" in notice.content
    # Re-running before a decision must not duplicate the request.
    run_attempt(tmp_path, task, ScriptedModel(responses=[]))
    with db() as session:
        assert len(session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).all()) == 1

    with db() as session:
        outsider = User(username=uuid4().hex, display_name="Outsider", password_hash="unused")
        session.add(outsider)
        session.commit()
        session.refresh(outsider)
    with TestClient(app) as client:
        url = f"/api/approvals/{approval.id}"
        listed = client.get(f"/api/channels/{task.channel_id}/approvals",
                            headers={"Authorization": f"Bearer {issue_token(user.id)}"}).json()
        assert [row["id"] for row in listed] == [approval.id]
        assert client.post(url, json={"approve": True},
                           headers={"Authorization": f"Bearer {issue_token(outsider.id)}"}).status_code == 403
        headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
        assert client.post(url, json={"approve": True, "note": "Go ahead"}, headers=headers).status_code == 200
        assert client.post(url, json={"approve": False}, headers=headers).status_code == 409
    with db() as session:
        assert session.get(AgentTask, task.id).status == "queued"
        assert session.exec(select(Message).where(Message.channel_id == task.channel_id,
                            Message.content == "Tester approved: Submit the signup form on example.com (Go ahead)")).one()

    model = RecordingModel(responses=[AIMessage(content="Submitted the form.")])
    done = run_attempt(tmp_path, task, model)
    assert done.status == "completed" and done.result == "Submitted the form."
    assert any('"approved": true' in r and "Go ahead" in r for r in tool_results(model))


def test_denied_approval_tells_the_agent_not_to_act(tmp_path, job):
    from app.persistence.models import TaskApproval
    from app.tasks import decide_approval
    task, _, _ = job
    run_attempt(tmp_path, task, ScriptedModel(responses=[approval_call("deny-1")]))
    with db() as session:
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
    assert decide_approval(approval.id, approve=False, decided_by="Tester").status == "denied"
    model = RecordingModel(responses=[AIMessage(content="I did not submit it.")])
    assert run_attempt(tmp_path, task, model).status == "completed"
    assert any('"approved": false' in r and "Do not perform this action" in r for r in tool_results(model))


def test_approval_inside_computer_helper_resumes_the_helper(tmp_path, job):
    task, _, _ = job
    first = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "id": "delegate-gui", "args": {
            "subagent_type": "computer", "description": "Submit the signup form; ask for approval first."}}]),
        approval_call("helper-approval"),
    ])
    assert run_attempt(tmp_path, task, first).status == "waiting"
    from app.persistence.models import TaskApproval
    from app.tasks import decide_approval
    with db() as session:
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
    decide_approval(approval.id, approve=True, decided_by="Tester")
    model = RecordingModel(responses=[AIMessage(content="Clicked submit."), AIMessage(content="Signup done.")])
    done = run_attempt(tmp_path, task, model)
    assert done.status == "completed" and done.result == "Signup done."
    # The helper's first resumed call already contains the decision: it resumed, not restarted.
    assert any('"approved": true' in r for r in tool_results(model))


def test_waiting_task_can_be_cancelled(tmp_path, job):
    task, _, user = job
    run_attempt(tmp_path, task, ScriptedModel(responses=[approval_call("cancel-me")]))
    with TestClient(app) as client:
        response = client.post(f"/api/tasks/{task.id}/cancel", headers={"Authorization": f"Bearer {issue_token(user.id)}"})
    assert response.json()["status"] == "cancelled"


def test_deleting_an_agent_leaves_nothing_for_a_reused_id(job):
    from app.computer import TaskComputer, sandbox_record, save_sandbox
    from app.persistence.models import AgentMemory, Schedule
    from app.tasks.schedules import create_schedule
    from app.workspace.service import AppService
    task, _, _ = job
    writer = teammate()
    work = enqueue(agent_id=writer.id, channel_id=task.channel_id, room_id=task.room_id,
                   source_id=f"writer-{writer.id}", prompt="Draft")
    tools_for(work)[1].invoke({"key": "style", "content": "Short sentences"})
    created = create_schedule(work, request="Weekly digest", cadence="interval", every_minutes=60, call_id="del")
    old_key = TaskComputer(work).key
    save_sandbox(old_key, sandbox_id="old-sandbox", status="stopped")
    with db() as session:
        profile = session.get(AgentProfile, writer.id)
    asyncio.run(AppService().delete_agent(profile))
    with db() as session:
        assert not session.exec(select(AgentMemory).where(AgentMemory.agent_id == writer.id)).all()
        assert not session.get(Schedule, created["id"]).enabled
        # A new agent that reuses the numeric id must get a different computer.
        reused = AgentProfile(id=writer.id, name=f"Reused-{writer.id}", role="writer",
                              band_agent_id=f"new-band-{uuid4().hex}", band_api_key_enc=encrypt_secret("offline"))
        session.add(reused)
        session.commit()
    assert sandbox_record(old_key) is None
    assert TaskComputer(work).key != old_key


def test_cancelling_a_waiting_task_closes_its_approval(tmp_path, job):
    from app.persistence.models import TaskApproval
    task, _, user = job
    run_attempt(tmp_path, task, ScriptedModel(responses=[approval_call("cancelled-approval")]))
    with db() as session:
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.post(f"/api/tasks/{task.id}/cancel", headers=headers).json()["status"] == "cancelled"
        assert client.get(f"/api/channels/{task.channel_id}/approvals", headers=headers).json() == []
        assert client.post(f"/api/approvals/{approval.id}", json={"approve": True}, headers=headers).status_code == 409
        with db() as session:
            closed = session.get(TaskApproval, approval.id)
            assert closed.status == "denied" and closed.note == "Task cancelled"
            assert not session.exec(select(Message).where(Message.channel_id == task.channel_id,
                                    Message.content.startswith("Tester approved"))).first()
        assert client.post(f"/api/tasks/{task.id}/retry", headers=headers).json()["status"] == "queued"
    # Resuming a cancelled task tells the agent its request was not approved.
    model = RecordingModel(responses=[AIMessage(content="Not submitted.")])
    assert run_attempt(tmp_path, task, model).status == "completed"
    assert any('"approved": false' in r and "Task cancelled" in r for r in tool_results(model))


def test_approval_messages_carry_the_decision(tmp_path, job):
    task, _, user = job
    run_attempt(tmp_path, task, ScriptedModel(responses=[approval_call("inline-approval")]))
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}

    def approval_message(client):
        rows = client.get(f"/api/channels/{task.channel_id}/messages", headers=headers).json()
        return next(m for m in rows if m["approval"])

    with TestClient(app) as client:
        listed = approval_message(client)
        assert listed["handoff"] is False
        assert listed["approval"] == {"id": listed["approval"]["id"], "status": "pending",
                                      "action": "Submit the signup form on example.com",
                                      "details": "Email: test@example.com", "decided_by": "", "note": ""}
        with client.websocket_connect(f"/ws?token={issue_token(user.id)}") as ws:
            client.post(f"/api/approvals/{listed['approval']['id']}", json={"approve": True}, headers=headers)
            event = ws.receive_json()
            while event["type"] != "message_updated":
                event = ws.receive_json()
        assert event["message"]["id"] == listed["id"]
        assert event["message"]["approval"]["status"] == "approved"
        assert event["message"]["approval"]["decided_by"] == "Tester"
        assert approval_message(client)["approval"]["status"] == "approved"


def test_cancelling_republishes_the_closed_approval(tmp_path, job):
    task, _, user = job
    run_attempt(tmp_path, task, ScriptedModel(responses=[approval_call("cancel-inline")]))
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={issue_token(user.id)}") as ws:
            client.post(f"/api/tasks/{task.id}/cancel", headers=headers)
            event = ws.receive_json()
            while event["type"] != "message_updated":
                event = ws.receive_json()
    assert event["message"]["approval"]["status"] == "denied"
    assert event["message"]["approval"]["note"] == "Task cancelled"


def test_context_shows_agents_by_display_name_not_band_handle(job):
    from app.tasks import channel_context
    task, _, _ = job
    writer = teammate()
    with db() as session:
        stored = session.get(AgentProfile, writer.id)
        stored.handle = f"owner.sfsu/{writer.name.lower()}"
        session.add(stored)
        session.add(Message(channel_id=task.channel_id, author_type="human", author_name="Tester",
                            content=f"@{stored.handle} draft it, not @{stored.handle}x"))
        session.commit()
        context = channel_context(session, task.channel_id)
    assert f"Tester: @{writer.name} draft it, not @owner.sfsu/{writer.name.lower()}x" in context
