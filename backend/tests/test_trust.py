from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select

from app.auth import issue_token
from app.computer import active_control, computer_key, take_control
from app.config import MODELS
from app.main import app
from app.persistence.models import (AgentProfile, AgentTask, AuditEvent, Budget, ChannelMember, ComputerControl,
                                    Message, TaskApproval, TaskCacheUsage, TaskUsage, User, WorkspaceRole, db, init_db)
from app.workspace.service import service
from app.runtime.harness import build_harness
from app.tasks import enqueue, next_task, update_task
from app.tasks import usage as usage_module
from app.tasks.usage import UsageRecorder, task_usage
from app.tasks.worker import execute, process, record_activity, server_action
from app.workspace.audit import record, summarize, summarize_args
from app.workspace.permissions import ensure_owner, role_of
from test_tasks import ScriptedModel, isolate_queue, job, teammate  # noqa: F401  (job is a fixture)


def _person(name: str, role: str | None = None, channel_id: int | None = None) -> tuple[User, dict]:
    """A new account, never the owner (the job fixture's user can be the suite's oldest account)."""
    init_db()
    with db() as session:
        user = User(username=uuid4().hex, display_name=name, password_hash="unused")
        session.add(user)
        session.commit()
        session.refresh(user)
        if role:
            session.add(WorkspaceRole(user_id=user.id, role=role))
        if channel_id:
            session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
    return user, _auth(user.id)


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {issue_token(user_id)}"}


def _pending_approval(task: AgentTask) -> str:
    approval_id = uuid4().hex
    update_task(task.id, status="waiting")
    with db() as session:
        session.add(TaskApproval(id=approval_id, task_id=task.id, channel_id=task.channel_id, agent_id=task.agent_id,
                                 action="Email the client"))
        session.commit()
    return approval_id


def _activity(client: TestClient, channel_id: int, headers: dict, **params) -> list[dict]:
    """The channel's audit events, oldest first."""
    response = client.get(f"/api/channels/{channel_id}/audit", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return list(reversed(response.json()))


def test_oldest_account_owns_the_workspace_and_only_the_owner_changes_roles():
    _person("Seed")
    with db() as session:
        ensure_owner(session)
        ensure_owner(session)
        owners = session.exec(select(WorkspaceRole).where(WorkspaceRole.role == "owner")).all()
        oldest = session.exec(select(User).order_by(User.created_at, User.id)).first()
    assert [o.user_id for o in owners] == [oldest.id]
    _, admin = _person("Ada", "admin")
    max_user, member = _person("Max")
    owner = _auth(oldest.id)
    with TestClient(app) as client:
        assert client.put(f"/api/users/{max_user.id}/role", json={"role": "admin"}, headers=admin).status_code == 403
        assert client.put(f"/api/users/{oldest.id}/role", json={"role": "member"}, headers=owner).status_code == 409
        assert client.put(f"/api/users/{max_user.id}/role", json={"role": "owner"}, headers=owner).status_code == 422
        assert client.put(f"/api/users/{max_user.id}/role", json={"role": "admin"}, headers=owner).status_code == 200
        roles = client.get("/api/bootstrap", headers=member).json()["workspace_roles"]
        assert roles[str(max_user.id)] == "admin" and roles[str(oldest.id)] == "owner"
        assert client.put(f"/api/users/{max_user.id}/role", json={"role": "member"}, headers=owner).status_code == 200
        with db() as session:
            assert role_of(session, max_user.id) == "member"
        assert client.get("/api/audit", headers=member).status_code == 403
        changes = [e["detail"] for e in client.get("/api/audit", headers=owner).json() if e["action"] == "role_changed"]
        assert {"user": "Max", "role": "member"} in changes and {"user": "Max", "role": "admin"} in changes


def test_approvals_follow_the_channel_policy(job):
    task, _, _ = job
    _, member = _person("Max", channel_id=task.channel_id)
    _, admin = _person("Ada", "admin", task.channel_id)
    with TestClient(app) as client:
        first = _pending_approval(task)
        # No policy keeps the old behavior: any member decides.
        assert client.post(f"/api/approvals/{first}", json={"approve": True}, headers=member).status_code == 200
        url = f"/api/channels/{task.channel_id}/policy"
        assert client.put(url, json={"approvers": "admins"}, headers=member).status_code == 403
        assert client.put(url, json={"approvers": "admins"}, headers=admin).json() == {
            "approvers": "admins", "computer_control": "members"}
        channel = next(c for c in client.get("/api/bootstrap", headers=member).json()["channels"]
                       if c["id"] == task.channel_id)
        assert channel["policy"]["approvers"] == "admins"
        second = _pending_approval(task)
        denied = client.post(f"/api/approvals/{second}", json={"approve": True}, headers=member)
        assert denied.status_code == 403 and "admins" in denied.json()["detail"]
        assert client.post(f"/api/approvals/{second}", json={"approve": False}, headers=admin).status_code == 200
        actions = [e["action"] for e in _activity(client, task.channel_id, member)]
        assert actions == ["approval_approved", "policy_changed", "approval_denied"]
    with db() as session:
        notes = [m.content for m in session.exec(select(Message).where(Message.channel_id == task.channel_id))]
    assert any("only admins can approve agent actions" in n for n in notes)


def test_deleting_agents_and_taking_over_computers_need_permission(job):
    task, _, _ = job
    _, member = _person("Max", channel_id=task.channel_id)
    _, admin = _person("Ada", "admin", task.channel_id)
    writer = teammate()
    with TestClient(app) as client:
        assert client.delete(f"/api/agents/{writer.id}", headers=member).status_code == 403
        assert client.delete(f"/api/agents/{writer.id}", headers=admin).status_code == 200
        with db() as session:
            assert session.get(AgentProfile, writer.id) is None
        live = f"/api/tasks/{task.id}/computer/live"
        release = f"/api/tasks/{task.id}/computer/control"
        # Under the default policy members connect; it fails later only because no computer is configured.
        assert client.post(live, json={"control": False}, headers=member).status_code != 403
        client.put(f"/api/channels/{task.channel_id}/policy", json={"computer_control": "admins"}, headers=admin)
        for control in (True, False):
            denied = client.post(live, json={"control": control}, headers=member)
            assert denied.status_code == 403 and "admins" in denied.json()["detail"]
        assert client.post(live, json={"control": False}, headers=admin).status_code != 403
        take_control(computer_key(task), user_id=0, user_name="Ada")
        assert client.delete(release, headers=member).status_code == 403
        assert client.post(f"/api/tasks/{task.id}/computer/stop", headers=member).status_code == 403
        assert active_control(computer_key(task)) is not None
        assert client.delete(release, headers=admin).status_code == 200
        assert active_control(computer_key(task)) is None
        events = client.get("/api/audit", headers=admin).json()
        assert any(e["action"] == "agent_deleted" and e["detail"] == {"agent": writer.name} for e in events)


def test_activity_lists_tool_calls_approvals_and_handoffs_in_order(tmp_path, job):
    task, _, user = job
    writer = teammate()
    model = ScriptedModel(responses=[
        AIMessage(content="", id="call-model-1", tool_calls=[{"name": "request_approval", "args": {
            "action": "Email the client", "details": "Use token=abc123secretvalue"}, "id": "ask-1"}],
            usage_metadata={"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280}),
        AIMessage(content="", id="call-model-2", tool_calls=[{"name": "ask_teammate", "args": {
            "teammate": writer.name, "request": "Draft the follow-up", "report_back": False}, "id": "hand-1"}],
            usage_metadata={"input_tokens": 1500, "output_tokens": 40, "total_tokens": 1540}),
        AIMessage(content="Emailed and handed off.", id="call-model-3",
                  usage_metadata={"input_tokens": 1600, "output_tokens": 10, "total_tokens": 1610}),
    ])
    path = str(tmp_path / "audit.db")

    def run():
        async def scenario():
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                await saver.setup()
                with db() as session:
                    current = session.get(AgentTask, task.id)
                update_task(task.id, status="running")
                await process(current, saver, executor=lambda t, s: execute(
                    t, s, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw)))
        asyncio.run(scenario())

    run()
    with db() as session:
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
    member = _auth(user.id)
    with TestClient(app) as client:
        assert client.post(f"/api/approvals/{approval.id}", json={"approve": True}, headers=member).status_code == 200
        run()
        with db() as session:
            assert session.get(AgentTask, task.id).status == "completed"
        events = _activity(client, task.channel_id, member, task_id=task.id)
        assert [(e["action"], e["detail"].get("tool")) for e in events] == [
            ("tool_call", "request_approval"), ("approval_requested", None), ("approval_approved", None),
            ("tool_call", "ask_teammate"), ("handoff", None)]
        assert events[0]["detail"]["args"]["details"] == "Use token=[hidden]"
        assert events[1]["detail"]["details"] == "Use token=[hidden]"
        assert events[2]["actor_type"] == "human" and events[2]["actor_name"] == "Tester"
        assert events[4]["detail"]["teammate"] == writer.name
        listed = next(t for t in client.get(f"/api/channels/{task.channel_id}/tasks", headers=member).json()
                      if t["id"] == task.id)
        assert listed["usage"] == {"input_tokens": 4300, "output_tokens": 130, "cost": None}
        usage = client.get(f"/api/channels/{task.channel_id}/usage", headers=member).json()
        assert usage["tokens"] == 4430 and usage["budget"] is None


def test_tool_calls_and_usage_are_recorded_once_and_without_secrets(job):
    task, agent, _ = job
    message = AIMessage(content=[
        {"type": "text", "text": "Checking"},
        {"type": "code_interpreter_call", "id": "ci-1", "status": "completed", "code": "print('sk-private-test')",
         "container_id": "provider-container", "outputs": [{"type": "logs", "logs": "sk-private-test"}]},
    ], id="model-call-9", tool_calls=[{"name": "fetch_page", "id": "fetch-1", "args": {
        "url": "https://example.com/?api_key=abcdef123456", "api_key": "sk-live", "Authorization": "Bearer abc",
        "key": "style"}}], usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    assert server_action(message) == "Running code in a quick sandbox"
    assert server_action(AIMessage(content="Done")) == ""
    record_activity(task, (), message)
    record_activity(task, (), message)  # A replayed stream update.
    record_activity(task, ("tools:helper",), AIMessage(content="", id="helper-1", tool_calls=[
        {"name": "web_search", "id": "search-1", "args": {"query": "news"}}]))
    recorder = UsageRecorder(task, agent.model)
    for _ in range(2):  # A replayed model call.
        asyncio.run(recorder.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=uuid4()))
    with db() as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.task_id == task.id).order_by(AuditEvent.id)).all()
        usage = session.exec(select(TaskUsage).where(TaskUsage.task_id == task.id)).all()
    assert [e.detail["tool"] for e in events] == ["fetch_page", "code_interpreter", "web_search"]
    assert events[1].detail == {"tool": "code_interpreter"}
    assert events[0].detail["args"] == {"url": "https://example.com/?api_key=[hidden]", "api_key": "[hidden]",
                                        "Authorization": "[hidden]", "key": "style"}
    assert events[2].detail["via"] == "helper"
    assert [(u.input_tokens, u.output_tokens, u.model) for u in usage] == [(10, 5, agent.model)]
    assert summarize("x" * 500).endswith("…") and len(summarize("x" * 500)) == 200
    assert summarize_args({"cmd": "curl -H 'Authorization: Bearer sk-abcdefghijklmnop'"})["cmd"].count("[hidden]") == 1


@pytest.mark.parametrize("args, secret", [
    ({"command": "git clone https://bot:hunter2secret@github.com/org/repo"}, "hunter2secret"),
    ({"command": "git clone https://ghtoken12345@github.com/org/repo"}, "ghtoken12345"),
    ({"command": 'curl -H "Authorization: Basic dXNlcjpwYXNz" https://x'}, "dXNlcjpwYXNz"),
    ({"command": "curl -H 'Cookie: session=abc123; theme=dark' https://x"}, "abc123"),
    ({"command": "psql postgres://u:p4ss@h/db"}, "p4ss"),
    ({"command": "mysql -u root -phunter2 shop"}, "hunter2"),
    ({"command": "deploy --password=hunter2 --region us"}, "hunter2"),
    ({"command": "deploy --token hunter2"}, "hunter2"),
    ({"body": '{"password": "x y"}'}, "x y"),
    ({"password": "x y"}, "x y"),
    ({"headers": {"Authorization": "Basic dXNlcjpwYXNz", "Accept": "text/html"}}, "dXNlcjpwYXNz"),
    ({"requests": [{"headers": {"X-Api-Key": "k-9f8e7d"}}]}, "k-9f8e7d"),
    ({"input": ["login", {"pwd": "hunter2"}]}, "hunter2"),
])
def test_audit_summaries_hide_secrets(args, secret):
    summary = summarize_args(args)
    assert secret not in str(summary) and "[hidden]" in str(summary)


def test_audit_summaries_keep_ordinary_values():
    assert summarize_args({"command": "ssh -p 22 host && mkdir -p out", "key": "style"}) == {
        "command": "ssh -p 22 host && mkdir -p out", "key": "style"}
    assert summarize({"headers": {"Accept": "text/html"}}) == '{"headers": {"Accept": "text/html"}}'


def test_channel_activity_is_for_channel_members(job):
    task, _, user = job
    _, outsider = _person("Olga")
    with TestClient(app) as client:
        url = f"/api/channels/{task.channel_id}/audit"
        denied = client.get(url, headers=outsider)
        assert denied.status_code == 403 and "Join the channel" in denied.json()["detail"]
        assert client.get(url, params={"task_id": task.id}, headers=outsider).status_code == 403
        assert client.get(url, headers=_auth(user.id)).status_code == 200


def test_channel_budget_pauses_new_tasks_until_an_admin_raises_it(job):
    task, agent, _ = job
    _, member = _person("Max", channel_id=task.channel_id)
    _, admin = _person("Ada", "admin")
    isolate_queue({task.id})
    with db() as session:
        session.add(TaskUsage(task_id=uuid4().hex, channel_id=task.channel_id, agent_id=agent.id,
                              message_id="earlier", model=agent.model, input_tokens=4000, output_tokens=1000))
        session.commit()
    budget = {"scope": "channel", "scope_id": task.channel_id}
    with TestClient(app) as client:
        assert client.put("/api/budgets", json={**budget, "daily_tokens": 5000}, headers=member).status_code == 403
        assert client.put("/api/budgets", json={**budget, "daily_tokens": 5000}, headers=admin).status_code == 200
        assert next_task() is None
        with db() as session:
            progress = session.get(AgentTask, task.id).progress
        assert progress.startswith("Paused: #") and "5,000 tokens" in progress
        usage = client.get(f"/api/channels/{task.channel_id}/usage", headers=member).json()
        assert usage["tokens"] == 5000 and usage["budget"] == 5000
        assert usage["agents"][0]["agent_id"] == agent.id and usage["agents"][0]["tokens"] == 5000
        assert client.put("/api/budgets", json={**budget, "daily_tokens": 10000}, headers=admin).status_code == 200
        assert next_task().id == task.id
        changes = [e["detail"]["daily_tokens"] for e in _activity(client, task.channel_id, member)
                   if e["action"] == "budget_changed"]
        assert changes == [5000, 10000]
        assert client.put("/api/budgets", json={**budget, "daily_tokens": None}, headers=admin).status_code == 200
        with db() as session:
            assert session.get(Budget, ("channel", task.channel_id)) is None


def test_running_task_stops_when_its_agent_budget_runs_out(job):
    task, agent, _ = job
    update_task(task.id, status="running")

    async def scenario():
        async def spend(task, saver):
            await asyncio.sleep(0.6)
            with db() as session:
                session.add(TaskUsage(task_id=task.id, channel_id=task.channel_id, agent_id=agent.id,
                                      message_id="big", model=agent.model, input_tokens=3000, output_tokens=0))
                session.commit()
            await asyncio.sleep(30)
        with db() as session:
            session.add(Budget(scope="agent", scope_id=agent.id, daily_tokens=2000))
            session.commit()
        await asyncio.wait_for(process(task, None, executor=spend), timeout=10)
    asyncio.run(scenario())
    with db() as session:
        stopped = session.get(AgentTask, task.id)
        notices = session.exec(select(Message).where(Message.band_message_id.startswith(f"budget:{task.id}:"))).all()
        audit = session.exec(select(AuditEvent).where(AuditEvent.task_id == task.id)).all()
    assert stopped.status == "cancelled" and stopped.progress.startswith(f"Stopped: {agent.name} used")
    assert len(notices) == 1 and "retry the task" in notices[0].content
    assert [e.action for e in audit] == ["task_stopped"]


def test_admin_only_follow_ups(job):
    task, agent, _ = job
    _, member = _person("Max", channel_id=task.channel_id)
    _, admin = _person("Ada", "admin", task.channel_id)
    other = next(m["id"] for m in MODELS if m["id"] != agent.model)
    with TestClient(app) as client:
        denied = client.patch(f"/api/agents/{agent.id}", json={"model": other}, headers=member)
        assert denied.status_code == 403 and "model" in denied.json()["detail"]
        assert client.patch(f"/api/agents/{agent.id}", json={"model": agent.model, "instructions": "Be brief"},
                            headers=member).status_code == 200
        assert client.delete("/api/agents/999999", headers=member).status_code == 404
        assert client.delete("/api/agents/999999", headers=admin).status_code == 404
        client.put(f"/api/channels/{task.channel_id}/policy", json={"approvers": "admins"}, headers=admin)
        _pending_approval(task)
        blocked = client.post(f"/api/tasks/{task.id}/cancel", headers=member)
        assert blocked.status_code == 403 and "approval" in blocked.json()["detail"]
        assert client.post(f"/api/tasks/{task.id}/cancel", headers=admin).json()["status"] == "cancelled"
        budget = {"scope": "channel", "scope_id": task.channel_id, "daily_tokens": 5000}
        for _ in range(2):
            assert client.put("/api/budgets", json=budget, headers=admin).status_code == 200
        events = _activity(client, task.channel_id, admin)
        assert [e["action"] for e in events].count("budget_changed") == 1
        cancelled = next(e for e in events if e["action"] == "task_cancelled")
        assert cancelled["detail"] == {"approvals_denied": 1}


def test_activity_polling_reads_every_new_event(job):
    task, _, user = job
    for number in range(5):
        record("memory_forgotten", actor_type="human", actor_name="Tester", channel_id=task.channel_id,
               detail={"key": f"k{number}"})
    with TestClient(app) as client:
        url = f"/api/channels/{task.channel_id}/audit"
        newest_first = client.get(url, headers=_auth(user.id)).json()
        since = client.get(url, params={"after": newest_first[3]["id"]}, headers=_auth(user.id)).json()
    assert [e["detail"]["key"] for e in since] == ["k2", "k3", "k4"]


def test_expired_take_over_is_announced_once(job):
    task, agent, _ = job
    key = computer_key(task)
    take_control(key, user_id=0, user_name="Ada")
    with db() as session:
        control = session.get(ComputerControl, key)
        control.until = control.until - timedelta(minutes=30)
        session.add(control)
        session.commit()

    async def scenario():
        await service.end_expired_take_overs()
        await service.end_expired_take_overs()
    asyncio.run(scenario())
    with db() as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.channel_id == task.channel_id,
                                                       AuditEvent.action == "computer_control_expired")).all()
        notes = session.exec(select(Message).where(Message.channel_id == task.channel_id,
                                                   Message.content.contains("take-over"))).all()
        assert session.get(ComputerControl, key) is None
    assert [e.detail for e in events] == [{"user": "Ada", "agent": agent.name}]
    assert len(notes) == 1


def test_task_list_keeps_unfinished_tasks_in_busy_channels(job):
    task, agent, user = job
    update_task(task.id, status="running")
    for _ in range(31):
        newer = enqueue(agent_id=agent.id, channel_id=task.channel_id, room_id="room", source_id=uuid4().hex,
                        prompt="Quick question")
        update_task(newer.id, status="completed")
    with TestClient(app) as client:
        rows = client.get(f"/api/channels/{task.channel_id}/tasks", headers=_auth(user.id)).json()
    assert len(rows) == 31 and rows[-1]["id"] == task.id and rows[-1]["status"] == "running"


def _run(task: AgentTask, path: str, factory) -> None:
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            await saver.setup()
            with db() as session:
                current = session.get(AgentTask, task.id)
            update_task(task.id, status="running")
            await process(current, saver, executor=lambda t, s: execute(t, s, factory=factory))
    asyncio.run(scenario())


class SummarizeFirst(AgentMiddleware):
    """Calls a model internally and keeps only its text, as Deep Agents' summarization does."""

    def __init__(self, model):
        super().__init__()
        self.summarizer = model

    async def abefore_model(self, state, runtime):
        await self.summarizer.ainvoke("Summarize the conversation", config={"metadata": {"lc_source": "summarization"}})
        return None


def test_internal_model_calls_are_counted(tmp_path, job):
    task, agent, _ = job
    summarizer = ScriptedModel(responses=[AIMessage(content="A summary", id="summary-1", usage_metadata={
        "input_tokens": 5000, "output_tokens": 200, "total_tokens": 5200,
        "input_token_details": {"cache_read": 4000, "cache_creation": 500}})])
    model = ScriptedModel(responses=[AIMessage(content="Done.", id="answer-1", usage_metadata={
        "input_tokens": 300, "output_tokens": 20, "total_tokens": 320})])
    _run(task, str(tmp_path / "summary.db"), lambda t, p, c, **kw: create_agent(
        model, tools=[], middleware=[SummarizeFirst(summarizer)], checkpointer=c))
    with db() as session:
        assert session.get(AgentTask, task.id).status == "completed"
        rows = {u.message_id: u for u in session.exec(select(TaskUsage).where(TaskUsage.task_id == task.id))}
        cache = session.get(TaskCacheUsage, rows["summary-1"].id)
        totals = task_usage(session, [task.id])[task.id]
    assert set(rows) == {"summary-1", "answer-1"}
    assert (cache.cache_read_tokens, cache.cache_write_tokens) == (4000, 500)
    assert (totals["input_tokens"], totals["output_tokens"]) == (5300, 220)


def test_budget_refuses_the_next_model_call(tmp_path, job):
    task, agent, _ = job
    model = ScriptedModel(responses=[
        AIMessage(content="", id="plan-1", tool_calls=[{"name": "write_todos", "id": "todo-1", "args": {
            "todos": [{"content": "Check the numbers", "status": "in_progress"}]}}],
            usage_metadata={"input_tokens": 1800, "output_tokens": 300, "total_tokens": 2100}),
        AIMessage(content="Should never be produced.", id="answer-1"),
    ])
    with db() as session:
        session.add(Budget(scope="agent", scope_id=agent.id, daily_tokens=2000))
        session.commit()
    _run(task, str(tmp_path / "budget.db"), lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw))
    with db() as session:
        stopped = session.get(AgentTask, task.id)
        used = session.exec(select(TaskUsage).where(TaskUsage.task_id == task.id)).all()
    assert model.index == 1
    assert stopped.status == "cancelled" and stopped.progress.startswith(f"Stopped: {agent.name} used")
    assert [u.message_id for u in used] == ["plan-1"]


def test_costs_use_cache_rates_and_alias_prices(monkeypatch):
    monkeypatch.setattr(usage_module, "PRICES", {
        "gpt-6-luna": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
        "gpt": {"input": 100.0, "output": 100.0, "cache_read": 100.0, "cache_write": 100.0},
    })
    # Synthetic rates: 500k uncached, 400k cache reads, 100k cache writes; plus 100k output tokens.
    assert usage_module.cost("gpt-6-luna", 1_000_000, 100_000, 400_000, 100_000) == pytest.approx(
        0.5 + 0.04 + 0.125 + 0.5)
    assert usage_module.cost("gpt-6-luna-20261001", 1_000_000, 0) == 1.0
    assert usage_module.cost("unpriced-model", 1_000_000, 0) is None
    monkeypatch.setattr(usage_module, "settings", SimpleNamespace(model_prices='{"m": {"input": 2, "output": 10}}'))
    assert usage_module._prices()["m"] == {"input": 2.0, "output": 10.0, "cache_read": 2.0, "cache_write": 2.0}
