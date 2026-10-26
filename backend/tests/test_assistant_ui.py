"""Real Deep Agents interrupts, fresh checkpoint connections and authenticated UI responses. Offline."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select

from app.auth import issue_token
from app.main import app
from app.persistence.models import (AgentTask, Channel, ChannelMember, ComponentResponse, DirectMessage,
                                    Message, MessageComponent, TaskApproval, TaskInput, User, db)
from app.runtime.ui import CATALOG, validate_answer, validate_ui
from app.tasks import approval_decisions, decide_approval, enqueue, recover_tasks
from app.tasks.worker import run_queue
from helpers import RecordingModel, ScriptedModel, approval_call, run_attempt

UI = {"_type": "Col", "gap": 4, "children": [
    {"_type": "Text", "value": "A few details will help me make the plan."},
    {"_type": "RadioGroup", "name": "scope", "label": "Release scope", "options": [
        {"label": "Small release", "value": "small", "description": "Finish the essentials first"},
        {"label": "Full release", "value": "full"}]},
    {"_type": "Input", "name": "budget", "label": "Budget", "inputType": "number"},
    {"_type": "CheckboxGroup", "name": "work", "label": "Include", "options": [
        {"label": "Documentation", "value": "docs"}, {"label": "Testing", "value": "tests"}]},
]}
ASK = {"title": "Plan the release", "description": "Choose what matters before I start.",
       "ui": UI, "required": ["scope"]}


def ask(call_id="ask-1", **changes):
    return AIMessage(content="", tool_calls=[{"name": "ask_user", "args": {**ASK, **changes}, "id": call_id}])


def pending(task_id):
    with db() as session:
        request = session.exec(select(TaskInput).where(TaskInput.task_id == task_id,
                                                       TaskInput.status == "pending")).one()
        return request, session.get(MessageComponent, request.component_id)


def answer(client, task, component, user, **changes):
    return client.post(f"/api/channels/{task.channel_id}/components/{component.id}/answer",
                       headers={"Authorization": f"Bearer {issue_token(user.id)}"},
                       json={"values": {"scope": "small", "budget": "1500.5", "work": ["docs"]},
                             "steering": "Draft only. Keep the plan under one week.", **changes})


def test_input_interrupt_resumes_same_graph_after_restart_and_can_ask_again(tmp_path, job):
    task, _, user = job
    first = ScriptedModel(responses=[ask()])
    waiting = run_attempt(tmp_path, task, first)
    assert waiting.status == "waiting" and first.index == 1
    request, component = pending(task.id)
    assert component.message_id and approval_decisions([request.id]) is None
    # A worker restart rediscovers the checkpoint, without invoking another model or duplicating UI.
    recover_tasks()
    assert run_attempt(tmp_path, task, ScriptedModel(responses=[])).status == "waiting"
    with db() as session:
        assert len(session.exec(select(TaskInput).where(TaskInput.task_id == task.id)).all()) == 1
        assert session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).all() == []
    with TestClient(app) as client:
        result = answer(client, task, component, user)
        assert result.status_code == 200, result.text
        assert answer(client, task, component, user).json()["response"] == result.json()["response"]
        assert answer(client, task, component, user, steering="Something different").status_code == 409
        state = client.get(f"/api/channels/{task.channel_id}/components/{component.id}",
                           headers={"Authorization": f"Bearer {issue_token(user.id)}"}).json()
        assert state["request"]["status"] == "answered"
        assert state["response"]["values"]["answers"]["budget"] == 1500.5
    with db() as session:
        assert session.get(AgentTask, task.id).status == "queued"
        assert len(session.exec(select(AgentTask).where(AgentTask.channel_id == task.channel_id)).all()) == 1
        assert len(session.exec(select(ComponentResponse).where(ComponentResponse.component_id == component.id)).all()) == 1
    followup = RecordingModel(responses=[ask("ask-2", title="One last detail")])
    assert run_attempt(tmp_path, task, followup).status == "waiting"
    result = next(m for m in followup.seen[0] if getattr(m, "name", "") == "ask_user")
    assert "Keep the plan under one week" in result.content
    assert '"budget": 1500.5' in result.content
    second, second_component = pending(task.id)
    assert second.id != request.id
    with TestClient(app) as client:
        assert answer(client, task, second_component, user).status_code == 200
    last = run_attempt(tmp_path, task, ScriptedModel(responses=[AIMessage(content="Here is the revised draft.")]))
    assert last.status == "completed" and last.id == task.id


def test_multiple_inputs_and_external_approval_all_gate_same_resume(tmp_path, job):
    task, _, user = job
    calls = ask().tool_calls + ask("ask-2", title="Choose a deadline").tool_calls + approval_call().tool_calls
    assert run_attempt(tmp_path, task, ScriptedModel(responses=[AIMessage(content="", tool_calls=calls)])).status == "waiting"
    with db() as session:
        requests = session.exec(select(TaskInput).where(TaskInput.task_id == task.id)).all()
        components = [session.get(MessageComponent, request.component_id) for request in requests]
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
    assert len(requests) == 2
    assert decide_approval(approval.id, approve=False, decided_by=user.display_name)
    with TestClient(app) as client:
        assert answer(client, task, components[0], user).status_code == 200
        with db() as session:
            assert session.get(AgentTask, task.id).status == "waiting"
        assert answer(client, task, components[1], user).status_code == 200
    resumed = RecordingModel(responses=[AIMessage(content="Kept it as a draft.")])
    assert run_attempt(tmp_path, task, resumed).status == "completed"
    approval_result = next(m for m in resumed.seen[0] if getattr(m, "name", "") == "request_approval")
    assert json.loads(approval_result.content)["approved"] is False


def test_cancelled_question_cannot_resume_until_explicit_retry_and_scope_is_enforced(tmp_path, job):
    task, _, user = job
    assert run_attempt(tmp_path, task, ScriptedModel(responses=[ask()])).status == "waiting"
    _, component = pending(task.id)
    with db() as session:
        other = User(username=uuid4().hex, display_name="Other", password_hash="unused")
        channel = Channel(name=uuid4().hex)
        session.add_all([other, channel])
        session.commit()
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.commit()
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    path = f"/api/channels/{task.channel_id}/components/{component.id}/answer"
    with TestClient(app) as client:
        assert client.post(path, json={"values": {}}).status_code == 401
        assert answer(client, task, component, other).status_code == 403
        assert client.post(f"/api/channels/{channel.id}/components/{component.id}/answer", headers=headers,
                           json={"values": {}}).status_code == 404
        assert answer(client, task, component, user, values={"scope": "invalid"}).status_code == 422
        assert client.post(f"/api/tasks/{task.id}/cancel", headers=headers).status_code == 200
        assert answer(client, task, component, user).status_code == 409
        assert client.get(f"/api/channels/{task.channel_id}/components/{component.id}", headers=headers).json()["request"]["status"] == "closed"
        assert client.post(f"/api/tasks/{task.id}/retry", headers=headers).status_code == 200
        assert run_attempt(tmp_path, task, ScriptedModel(responses=[])).status == "waiting"
        assert answer(client, task, component, user).status_code == 200
        with db() as session:
            session.add(DirectMessage(channel_id=task.channel_id, user_id=other.id, agent_id=task.agent_id))
            session.commit()
        assert answer(client, task, component, user).status_code == 404


def test_waiting_question_releases_worker_slot(tmp_path, job):
    import asyncio
    from app.tasks.worker import execute
    from app.runtime.harness import build_harness
    task, _, _ = job
    second = enqueue(agent_id=task.agent_id, channel_id=task.channel_id, room_id=task.room_id,
                     source_id=uuid4().hex, prompt="A second request")
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "queue.db")) as saver:
            await saver.setup()
            async def executor(item, checkpoint):
                model = ScriptedModel(responses=[ask()] if item.id == task.id else [AIMessage(content="Second request done")])
                return await execute(item, checkpoint, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw))
            await run_queue(saver, concurrency=1, executor=executor, stop_when_idle=True)
    asyncio.run(scenario())
    with db() as session:
        assert session.get(AgentTask, task.id).status == "waiting"
        assert session.get(AgentTask, second.id).status == "completed"


def test_catalogue_validation_and_all_input_types():
    assert len(CATALOG["components"]) == 55
    assert validate_answer(UI, ["scope"], {"scope": "small", "budget": "12.5", "work": ["docs"]})["budget"] == 12.5
    for bad in ({"scope": "invalid"}, {"scope": "small", "work": ["unknown"]},
                {"scope": "small", "budget": True}, {"scope": "small", "budget": "inf"},
                {"scope": "small", "other": "unknown"}, {"scope": " "}):
        with pytest.raises(ValueError):
            validate_answer(UI, ["scope"], bad)
    typed = {"_type": "Col", "children": [
        {"_type": "Input", "name": "notes", "label": "Notes", "multiline": True},
        {"_type": "Select", "name": "team", "label": "Team", "options": [{"label": "A", "value": "a"}]},
        {"_type": "DatePicker", "name": "date", "label": "Date", "min": "2026-10-18"},
        {"_type": "Checkbox", "name": "confirmed", "label": "Confirmed"},
        {"_type": "Slider", "name": "hours", "label": "Hours", "min": 1, "max": 8},
    ]}
    values = {"notes": "abc", "team": "a", "date": "2026-10-21", "confirmed": True, "hours": 4}
    assert validate_answer(typed, ["confirmed"], values) == values
    for changes in ({"date": "2026-02-30"}, {"date": "2026-10-17"}, {"confirmed": "true"}, {"hours": 9}):
        with pytest.raises(ValueError):
            validate_answer(typed, ["confirmed"], {**values, **changes})
    for bad in ({"_type": "iframe"}, {"_type": []}, {"_type": "Input", "name": "secret", "inputType": "password"},
                {"_type": "Text", "value": "Text", "_action": {"type": "send_email"}},
                {"_type": "Image", "src": "javascript:alert(1)", "alt": "bad"},
                {"_type": "Col", "children": [UI] * 210}):
        with pytest.raises(ValueError):
            validate_ui(bad, input_mode=True)


def test_present_ui_persists_catalogue_tree_and_is_replay_safe(tmp_path, job):
    task, _, user = job
    ui = {"_type": "Col", "children": [{"_type": "Fact", "label": "Ready", "value": "8"}]}
    call = AIMessage(content="", tool_calls=[{"name": "present_ui", "id": "present-1",
                                             "args": {"title": "Release readiness", "ui": ui}}])
    assert run_attempt(tmp_path, task, ScriptedModel(responses=[call, AIMessage(content="Eight are ready.")])).status == "completed"
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
        views = client.get(f"/api/channels/{task.channel_id}/views", headers=headers).json()
        assert views[0]["kind"] == "ui" and views[0]["props"]["ui"] == ui


def test_model_can_repair_invalid_ui_before_interrupting(tmp_path, job):
    task, _, _ = job
    invalid = ask(ui={"_type": "MadeUpWidget"})
    model = RecordingModel(responses=[invalid, ask("repaired")])
    assert run_attempt(tmp_path, task, model).status == "waiting"
    result = next(m for m in model.seen[1] if getattr(m, "name", "") == "ask_user")
    assert "Read ui_catalog" in result.content
    _, component = pending(task.id)
    assert component.tool_call_id == "repaired"


def test_input_response_rollback_and_shared_winner_are_atomic(tmp_path, job, monkeypatch):
    import app.tasks.inputs as inputs
    task, _, user = job
    assert run_attempt(tmp_path, task, ScriptedModel(responses=[ask()])).status == "waiting"
    request, component = pending(task.id)
    original_record = inputs.record_human
    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated transaction failure")
    monkeypatch.setattr(inputs, "record_human", fail)
    with pytest.raises(RuntimeError), db() as session:
        inputs.answer_input(session, task.channel_id, component.id, user, {"scope": "small"}, "draft only")
    with db() as session:
        assert session.get(ComponentResponse, component.id) is None
        assert session.get(TaskInput, request.id).status == "pending"
        assert session.get(AgentTask, task.id).status == "waiting"
        assert not session.exec(select(Message).where(Message.band_message_id == f"input-answer:{request.id}")).all()
        other = User(username=uuid4().hex, display_name="Teammate", password_hash="unused")
        session.add(other)
        session.flush()
        session.add(ChannelMember(channel_id=task.channel_id, member_type="human", member_id=other.id))
        session.commit()
    monkeypatch.setattr(inputs, "record_human", original_record)
    with TestClient(app) as client:
        assert answer(client, task, component, user).status_code == 200
        assert answer(client, task, component, other).status_code == 409
