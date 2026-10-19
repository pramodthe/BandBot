"""Generative UI persistence, shared decisions, scope, validation, and replay safety. Offline only."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from sqlmodel import select

from app.auth import issue_token
from app.main import app
from app.persistence.models import (AgentTask, Channel, ChannelMember, ComponentResponse, DirectMessage,
                                    Message, MessageComponent, User, db)
from app.runtime.tools import tools_for
from app.runtime.tools.generative import FormDefinition, ShowView
from app.workspace.generative import UIError, submit, validate_values
from app.workspace.service import service
from test_tasks import ScriptedModel, job, run_attempt  # noqa: F401

FORM = {
    "title": "Plan the next release",
    "description": "Choose a scope and send your brief to the agent.",
    "fields": [
        {"id": "scope", "label": "Scope", "type": "select", "required": True, "options": ["Small", "Full"]},
        {"id": "budget", "label": "Budget", "type": "number"},
        {"id": "notes", "label": "Notes", "type": "textarea"},
    ],
    "actions": [{"id": "plan", "label": "Draft a plan", "prompt": "Draft a release plan using the submitted brief.", "variant": "primary"},
                {"id": "estimate", "label": "Estimate first", "prompt": "Estimate effort before drafting."}],
}
VIEW = {
    "title": "Release overview", "description": "A project snapshot", "source": "Sample data for an offline test.",
    "blocks": [
        {"type": "metrics", "items": [{"label": "Ready", "value": "8", "detail": "of 12 tasks"}]},
        {"type": "chart", "label": "Completed", "value": "8", "points": [1, 3, 5, 8], "variant": "bars"},
        {"type": "table", "title": "Tasks", "columns": [{"key": "task_name", "label": "Task", "format": {"kind": "badge", "tones": {"Review": "warning"}}}], "rows": [{"task_name": "Review"}]},
        {"type": "progress", "title": "Readiness", "items": [{"label": "Docs", "value": 75}]},
    ],
}


def call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def finish(tmp_path, job):
    task, agent, user = job
    model = ScriptedModel(responses=[call("show_view", VIEW, "view-1"),
                                     AIMessage(content="Eight tasks are ready. Choose how to proceed.")])
    finished = run_attempt(tmp_path, task, model)
    assert finished.status == "completed"
    with db() as session:
        # Legacy cards remain readable/respondable; new agents use the checkpointed ask_user tool.
        session.add(MessageComponent(task_id=task.id, tool_call_id="form-1", message_id=finished.reply_message_id,
                                     kind="form", props=FormDefinition.model_validate(FORM).model_dump()))
        session.commit()
        form = session.exec(select(MessageComponent).where(MessageComponent.task_id == task.id,
                                                           MessageComponent.kind == "form")).one()
    return finished, agent, user, form


def test_generated_views_and_forms_are_attached_and_reopenable(tmp_path, job):
    task, _, user, form = finish(tmp_path, job)
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        messages = client.get(f"/api/channels/{task.channel_id}/messages", headers=headers).json()
        reply = next(message for message in messages if message["id"] == task.reply_message_id)
        assert [item["kind"] for item in reply["components"]] == ["view", "form"]
        assert reply["components"][0]["props"]["blocks"][2]["rows"] == [{"task_name": "Review"}]
        assert reply["components"][0]["props"]["blocks"][2]["columns"][0]["format"]["tones"] == {"Review": "warning"}
        assert reply["components"][1]["response"] is None
        views = client.get(f"/api/channels/{task.channel_id}/views", headers=headers).json()
        assert views[0]["props"]["title"] == VIEW["title"]
        assert views[0]["message_id"] == task.reply_message_id
        reader = {tool.name: tool for tool in tools_for(task)}["read_views"]
        assert reader.invoke({})["views"][0]["id"] == views[0]["id"]
        assert reader.invoke({"view_id": views[0]["id"]})["view"]["blocks"][2]["rows"] == [{"task_name": "Review"}]
        other_reader = {tool.name: tool for tool in tools_for(task.model_copy(update={"channel_id": task.channel_id + 10000}))}["read_views"]
        assert other_reader.invoke({"view_id": views[0]["id"]}) == {"status": "not_found"}
        assert other_reader.invoke({}) == {"views": []}
        state = client.get(f"/api/channels/{task.channel_id}/components/{form.id}", headers=headers)
        assert state.status_code == 200 and state.json()["response"] is None


def test_shared_response_queues_original_agent_once_and_survives_reload(tmp_path, job, monkeypatch):
    task, agent, user, form = finish(tmp_path, job)
    async def relay(*_args):
        pass
    monkeypatch.setattr(service, "_relay", relay)
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    path = f"/api/channels/{task.channel_id}/components/{form.id}/respond"
    body = {"action_id": "plan", "values": {"scope": "Small", "budget": 1500, "notes": "@Another Agent"}}
    with TestClient(app) as client:
        first = client.post(path, headers=headers, json=body)
        assert first.status_code == 200, first.text
        second = client.post(path, headers=headers, json=body)
        assert second.status_code == 200
        assert second.json()["response"] == first.json()["response"]
        assert client.post(path, headers=headers, json={**body, "action_id": "estimate"}).status_code == 409
        assert client.post(path, headers=headers, json={"action_id": "fake", "values": body["values"]}).status_code == 422
        with db() as session:
            teammate = User(username=uuid4().hex, display_name="Teammate", password_hash="unused")
            session.add(teammate)
            session.flush()
            session.add(ChannelMember(channel_id=task.channel_id, member_type="human", member_id=teammate.id))
            session.commit()
        assert client.post(path, headers={"Authorization": f"Bearer {issue_token(teammate.id)}"},
                           json=body).status_code == 409
        reply = next(message for message in client.get(f"/api/channels/{task.channel_id}/messages", headers=headers).json()
                     if message["id"] == task.reply_message_id)
        response = reply["components"][1]["response"]
        assert response["userName"] == "Tester" and response["label"] == "Draft a plan"
        assert response["values"]["budget"] == 1500
    with db() as session:
        assert session.get(ComponentResponse, form.id).message_id == first.json()["message"]["id"]
        followups = session.exec(select(AgentTask).where(AgentTask.channel_id == task.channel_id,
                                                        AgentTask.id != task.id)).all()
        assert len(followups) == 1 and followups[0].agent_id == agent.id
        assert "Requested next step: Draft a release plan" in followups[0].prompt
        assert "approval rules still apply" in followups[0].prompt


def test_component_response_scope_and_private_channel_isolation(tmp_path, job):
    task, _, user, form = finish(tmp_path, job)
    with db() as session:
        other = User(username=uuid4().hex, display_name="Other", password_hash="unused")
        channel = Channel(name=uuid4().hex)
        session.add_all([other, channel])
        session.commit()
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.commit()
    headers = {"Authorization": f"Bearer {issue_token(other.id)}"}
    with TestClient(app) as client:
        assert client.get(f"/api/channels/{task.channel_id}/views").status_code == 401
        assert client.get(f"/api/channels/{task.channel_id}/views", headers=headers).status_code == 403
        assert client.post(f"/api/channels/{task.channel_id}/components/{form.id}/respond", headers=headers,
                           json={"action_id": "plan", "values": {"scope": "Small"}}).status_code == 403
        assert client.get(f"/api/channels/{channel.id}/components/{form.id}",
                          headers={"Authorization": f"Bearer {issue_token(user.id)}"}).status_code == 404
        with db() as session:
            session.add(DirectMessage(channel_id=task.channel_id, user_id=user.id, agent_id=task.agent_id))
            session.add(ChannelMember(channel_id=task.channel_id, member_type="human", member_id=other.id))
            session.commit()
        assert client.get(f"/api/channels/{task.channel_id}/views", headers=headers).status_code == 404


def test_atomic_response_rolls_back_when_followup_creation_fails(tmp_path, job, monkeypatch):
    import app.workspace.generative as generated
    task, _, user, form = finish(tmp_path, job)
    def fail(*_args):
        raise RuntimeError("transaction failed")
    monkeypatch.setattr(generated, "channel_context", fail)
    with pytest.raises(RuntimeError), db() as session:
        submit(session, task.channel_id, form.id, user, "plan", {"scope": "Small"})
    with db() as session:
        assert session.get(ComponentResponse, form.id) is None
        assert session.exec(select(Message).where(Message.channel_id == task.channel_id,
                                                  Message.author_type == "human")).all() == []
        assert len(session.exec(select(AgentTask).where(AgentTask.channel_id == task.channel_id)).all()) == 1


def test_schema_bounds_invalid_choices_and_failed_output(job):
    task, _, user = job
    form = FormDefinition.model_validate(FORM)
    for values in ({}, {"scope": "Unknown"}, {"scope": "Small", "unknown": 1},
                   {"scope": "Small", "budget": True}, {"scope": "Small", "budget": float("inf")},
                   {"scope": "Small", "budget": 10 ** 400},
                   {"scope": "Small", "notes": "x" * 2001}):
        with pytest.raises(UIError):
            validate_values(form, values)
    typed_form = FormDefinition.model_validate({**FORM, "fields": [
        {"id": "agreed", "label": "Confirm", "type": "checkbox", "required": True},
        {"id": "when", "label": "Date", "type": "date", "required": True},
        {"id": "name", "label": "Name", "type": "text", "required": True},
    ]})
    assert validate_values(typed_form, {"agreed": True, "when": "2026-10-18", "name": "Ada"})["agreed"] is True
    for values in ({"agreed": False, "when": "2026-10-18", "name": "Ada"},
                   {"agreed": "yes", "when": "2026-10-18", "name": "Ada"},
                   {"agreed": True, "when": "2026-02-30", "name": "Ada"},
                   {"agreed": True, "when": "2026-10-18", "name": " "}):
        with pytest.raises(UIError):
            validate_values(typed_form, values)
    with pytest.raises(ValidationError):
        ShowView.model_validate({**VIEW, "tool_call_id": "bad", "blocks": [{"type": "html", "body": "<script/>"}]})
    with pytest.raises(ValidationError):
        ShowView.model_validate({**VIEW, "tool_call_id": "bad", "blocks": [{"type": "progress", "title": "Bad", "items": [{"label": "Bad", "value": 101}]}]})
    tool = {tool.name: tool for tool in tools_for(task)}["show_view"]
    for _ in range(2):
        tool.invoke({"name": "show_view", "args": VIEW, "id": "same", "type": "tool_call"})
    with db() as session:
        attached = session.exec(select(MessageComponent).where(MessageComponent.task_id == task.id)).all()
        assert len(attached) == 1 and attached[0].message_id is None
    reader = {tool.name: tool for tool in tools_for(task)}["read_views"]
    assert reader.invoke({"view_id": attached[0].id}) == {"status": "not_found"}
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
        assert client.get(f"/api/channels/{task.channel_id}/views", headers=headers).json() == []
        assert client.get(f"/api/channels/{task.channel_id}/components/{attached[0].id}", headers=headers).status_code == 404
