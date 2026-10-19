"""Sandbox source never executes on the server; follow-ups use ordinary scoped tasks."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from sqlmodel import select

from app.auth import issue_token
from app.main import app
from app.persistence.models import (AgentProfile, AgentSkill, AgentTask, Channel, ChannelMember,
                                    DirectMessage, Message, MessageComponent, SandboxInteraction, User, db)
from app.runtime.skills import skills_backend
from app.runtime.tools import tools_for
from app.runtime.tools.sandbox_ui import SandboxedUI, sandbox_ui_tools
from app.workspace.audit import summarize_args
from app.workspace.sandbox_ui import followup
from app.workspace.skills import enabled_skill_files, seed_library
from app.workspace.service import components_for
from test_tasks import ScriptedModel, job, run_attempt  # noqa: F401


SPEC = {"initialHeight": 360, "placeholderMessages": ["Preparing a calculator"], "css": "output{font-weight:bold}",
        "html": '<label>Amount<input id="amount" type="number" value="10"></label><output id="result">20</output>',
        "jsFunctions": "function calculate(){document.getElementById('result').textContent = '20';}",
        "jsExpressions": "calculate();", "title": "Example calculator", "description": "Illustrative values"}


def rendered(tmp_path, job):
    task, agent, user = job
    with db() as session:
        stored = session.get(AgentTask, task.id)
        stored.prompt = "Build an interactive calculator using generate_sandboxed_ui"
        session.add(stored)
        session.add(ChannelMember(channel_id=task.channel_id, member_type="agent", member_id=agent.id))
        session.commit()
        task.prompt = stored.prompt
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "generate_sandboxed_ui", "args": SPEC, "id": "sandbox-1"}]), AIMessage(content="Use the calculator below.")])
    completed = run_attempt(tmp_path, task, model)
    assert completed.status == "completed"
    with db() as session:
        component = session.exec(select(MessageComponent).where(MessageComponent.task_id == task.id)).one()
    return completed, agent, user, component


def test_sandbox_output_is_durable_replay_safe_and_reopens(tmp_path, job):
    completed, _, user, component = rendered(tmp_path, job)
    assert component.kind == "sandbox"
    assert {name: component.props[name] for name in SPEC} == SPEC
    assert component.props["agentIdentity"] == job[1].band_agent_id
    assert component.message_id == completed.reply_message_id
    tool = next(tool for tool in tools_for(completed) if tool.name == "generate_sandboxed_ui")
    tool.invoke({"name": tool.name, "type": "tool_call", "id": "sandbox-1", "args": SPEC})
    with db() as session:
        assert len(session.exec(select(MessageComponent).where(MessageComponent.task_id == completed.id)).all()) == 1
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        views = client.get(f"/api/channels/{completed.channel_id}/views", headers=auth).json()
        assert views[0]["kind"] == "sandbox" and views[0]["props"] == component.props
    reader = next(tool for tool in tools_for(completed) if tool.name == "read_views")
    assert reader.invoke({"view_id": component.id})["view"] == component.props


@pytest.mark.parametrize("changes", [
    {"html": "<html><body>Wrong</body></html>"}, {"html": '<meta http-equiv="refresh" content="0">'},
    {"html": '<iframe src="https://example.com"></iframe>'}, {"html": " "},
    {"initialHeight": 9999}, {"placeholderMessages": [""]}, {"css": "\x00"},
    {"html": "a" * 64000, "jsFunctions": "b" * 64000}, {"tools": ["send_email"]},
])
def test_invalid_or_unbounded_source_is_rejected(changes):
    with pytest.raises(ValidationError):
        SandboxedUI.model_validate({**SPEC, "tool_call_id": "test", **changes})


def test_confirmed_followup_is_atomic_replay_safe_and_only_targets_its_agent(tmp_path, job, monkeypatch):
    completed, agent, user, component = rendered(tmp_path, job)
    async def relay(*_args):
        pass
    from app.workspace.service import service
    monkeypatch.setattr(service, "_relay", relay)
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    path = f"/api/channels/{completed.channel_id}/components/{component.id}/followup"
    body = {"request_id": uuid4().hex, "text": "Explain the scenario with amount 10. @Other must not become a recipient."}
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 401
        first = client.post(path, json=body, headers=auth)
        second = client.post(path, json=body, headers=auth)
        assert first.status_code == second.status_code == 200
        assert first.json()["task_id"] == second.json()["task_id"]
        assert first.json()["message"]["id"] == second.json()["message"]["id"]
        assert client.post(path, json={**body, "text": "Changed"}, headers=auth).status_code == 409
        assert client.post(path, json={**body, "agent_id": 999}, headers=auth).status_code == 422
    with db() as session:
        task = session.get(AgentTask, first.json()["task_id"])
        message = session.get(Message, first.json()["message"]["id"])
        assert task.agent_id == agent.id and task.channel_id == completed.channel_id
        assert "not tool permissions or external-action approval" in task.prompt
        assert len(message.mentions) == 1 and message.mentions[0]["id"] == agent.id
        assert len(session.exec(select(SandboxInteraction).where(SandboxInteraction.component_id == component.id)).all()) == 1
    # A normal external-action approval still interrupts this follow-up's graph.
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "request_approval", "id": "external", "args": {"action": "Publish the result", "details": "External write"}}])])
    (tmp_path / "followup").mkdir()
    assert run_attempt(tmp_path / "followup", task, model).status == "waiting"


def test_followups_enforce_membership_component_channel_and_private_ownership(tmp_path, job):
    completed, _, user, component = rendered(tmp_path, job)
    with db() as session:
        outsider = User(username=uuid4().hex, display_name="Outsider", password_hash="unused")
        other = Channel(name=uuid4().hex)
        session.add_all([outsider, other]); session.commit()
        session.add(ChannelMember(channel_id=other.id, member_type="human", member_id=user.id)); session.commit()
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    body = {"request_id": uuid4().hex, "text": "Explain this"}
    with TestClient(app) as client:
        path = f"/api/channels/{completed.channel_id}/components/{component.id}/followup"
        assert client.post(path, json=body, headers={"Authorization": f"Bearer {issue_token(outsider.id)}"}).status_code == 403
        assert client.post(f"/api/channels/{other.id}/components/{component.id}/followup", json=body, headers=auth).status_code == 404
        with db() as session:
            session.add(DirectMessage(channel_id=completed.channel_id, user_id=outsider.id)); session.commit()
        assert client.post(path, json=body, headers=auth).status_code == 404


def test_followup_rolls_back_all_writes_if_audit_fails(tmp_path, job, monkeypatch):
    completed, _, user, component = rendered(tmp_path, job)
    def fail(*_args, **_kwargs):
        raise RuntimeError("Simulated transaction failure")
    monkeypatch.setattr("app.workspace.sandbox_ui.record", fail)
    with pytest.raises(RuntimeError), db() as session:
        followup(session, completed.channel_id, component.id, user, "request-rollback", "Explain this")
    with db() as session:
        assert session.exec(select(SandboxInteraction).where(SandboxInteraction.component_id == component.id)).first() is None
        assert not session.exec(select(Message).where(Message.channel_id == completed.channel_id, Message.author_type == "human")).all()


def test_followup_cannot_target_a_replacement_agent_with_a_reused_row_id(tmp_path, job):
    from app.workspace.generative import UIError
    completed, agent, user, component = rendered(tmp_path, job)
    with db() as session:
        replacement = session.get(AgentProfile, agent.id)
        replacement.band_agent_id = uuid4().hex
        session.add(replacement); session.commit()
    with db() as session, pytest.raises(UIError, match="no longer available"):
        followup(session, completed.channel_id, component.id, user, "replacement-test", "Explain this")


def test_ui_skills_are_opt_in_read_only_and_respect_admin_choices(job):
    _, agent, _ = job
    assert skills_backend(agent.id) is None
    names = ("master-playbook", "advanced-visualization", "svg-diagrams")
    with db() as session:
        seed_library(session)
        assert not session.exec(select(AgentSkill).where(AgentSkill.agent_id == agent.id)).all()
        for name in names:
            session.add(AgentSkill(agent_id=agent.id, skill_id=f"builtin-{name}", enabled=False))
        session.commit()
    assert skills_backend(agent.id) is None
    with db() as session:
        for name in names:
            link = session.get(AgentSkill, (agent.id, f"builtin-{name}"))
            link.enabled = True; session.add(link)
        session.commit()
    backend = skills_backend(agent.id)
    for name in names:
        assert not backend.read(f"/skills/{name}/SKILL.md").error
        assert not backend.read(f"/skills/{name}/LICENSE").error
        assert backend.write(f"/skills/{name}/SKILL.md", "bad").error
    with db() as session:
        session.delete(session.get(AgentSkill, (agent.id, "builtin-master-playbook")))
        link = session.get(AgentSkill, (agent.id, "builtin-advanced-visualization")); link.enabled = False; session.add(link)
        session.commit()
    skills_backend(agent.id)
    files = enabled_skill_files(agent.id)
    assert "/master-playbook/SKILL.md" not in files
    assert "/advanced-visualization/SKILL.md" not in files
    assert "/svg-diagrams/SKILL.md" in files


@pytest.mark.parametrize("prompt,available", [
    ("Explain binary search", False), ("What is Apple's share price?", False),
    ("Show a dashboard of sales with charts and tables", False), ("Calculate 17% of 2350", False),
    ("Explain how to use generate_sandboxed_ui", False), ("Write an HTML code example", False),
    ("Show an explanation without an interactive UI", False),
    ("I want an interactive calculator", True), ("Can you explain binary search interactively?", True),
    ("Build an interactive room flooring calculator", True),
    ("Create an interactive Three.js solar system", True),
    ("Use generate_sandboxed_ui to show binary search", True),
    ("Don't create an interactive UI. Explain binary search in words.", False),
])
def test_custom_tool_requires_an_explicit_request_and_native_elements_remain(job, prompt, available):
    task, _, _ = job
    task.prompt = prompt
    task.context = "Earlier: build an interactive calculator using generate_sandboxed_ui"
    names = {tool.name for tool in tools_for(task)}
    assert ("generate_sandboxed_ui" in names) is available
    assert {"present_ui", "ask_user", "ui_elements", "ui_catalog", "show_view"} <= names


def test_direct_custom_tool_cannot_bypass_request_policy(job):
    task, _, _ = job
    tool = sandbox_ui_tools(task)[0]
    result = tool.invoke({"name": tool.name, "type": "tool_call", "id": "unasked", "args": SPEC})
    assert "explicit request" in result.content
    with db() as session:
        assert not session.exec(select(MessageComponent).where(MessageComponent.task_id == task.id)).all()


def test_multiple_calls_revise_one_draft_and_older_replay_cannot_undo_it(tmp_path, job):
    task, _, _ = job
    task.prompt = "Build an interactive calculator"
    with db() as session:
        stored = session.get(AgentTask, task.id)
        stored.prompt = task.prompt
        session.add(stored); session.commit()
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "generate_sandboxed_ui", "args": SPEC, "id": "first"}]),
        AIMessage(content="", tool_calls=[{"name": "generate_sandboxed_ui", "args": {**SPEC, "title": "Revised calculator"}, "id": "second"}]),
        AIMessage(content="The calculator is ready."),
    ])
    completed = run_attempt(tmp_path, task, model)
    assert completed.status == "completed"
    tool = sandbox_ui_tools(task)[0]
    tool.invoke({"name": tool.name, "type": "tool_call", "id": "first", "args": SPEC})
    result = tool.invoke({"name": tool.name, "type": "tool_call", "id": "too-late", "args": SPEC})
    assert "finished" in result.content
    with db() as session:
        [component] = session.exec(select(MessageComponent).where(MessageComponent.task_id == task.id)).all()
        assert component.props["title"] == "Revised calculator"
        assert component.message_id == completed.reply_message_id


def test_legacy_duplicate_versions_are_hidden_without_removing_native_cards(tmp_path, job):
    completed, _, user, original = rendered(tmp_path, job)
    with db() as session:
        latest = MessageComponent(task_id=completed.id, tool_call_id="legacy-second", kind="sandbox",
                                  message_id=completed.reply_message_id, props={**original.props, "title": "Final version"})
        native = MessageComponent(task_id=completed.id, tool_call_id="native-card", kind="ui",
                                  message_id=completed.reply_message_id, props={"title": "Native report"})
        session.add_all([latest, native]); session.commit()
        visible = components_for(session, [completed.reply_message_id])[completed.reply_message_id]
        assert [row["id"] for row in visible] == [latest.id, native.id]
        assert session.get(MessageComponent, original.id) is not None
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as http:
        ids = [row["id"] for row in http.get(f"/api/channels/{completed.channel_id}/views", headers=auth).json()]
        assert latest.id in ids and original.id not in ids and native.id in ids
    reader = next(tool for tool in tools_for(completed) if tool.name == "read_views")
    assert original.id not in [row["id"] for row in reader.invoke({})["views"]]
    assert reader.invoke({"view_id": original.id})["view"] == original.props


def test_sandbox_source_is_not_copied_into_audit_arguments():
    assert summarize_args({"html": "private data", "jsFunctions": "secret code", "title": "Calculator"}) == {
        "html": "[interface source omitted]", "jsFunctions": "[interface source omitted]", "title": "Calculator"}
