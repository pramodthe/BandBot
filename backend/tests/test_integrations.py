"""MCP integration config, tool wrapping, and approval gating against a real local stdio server."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from sqlmodel import select

import app.runtime.tools.mcp as integrations
from app.runtime.tools.mcp import integration_tools, load_servers
from app.persistence.models import TaskApproval, db
from app.tasks import decide_approval
from test_tasks import RecordingModel, ScriptedModel, job, run_attempt  # noqa: F401  (job is a fixture)

SERVER = Path(__file__).with_name("mcp_test_server.py")


@pytest.fixture
def tickets(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    log.touch()
    monkeypatch.setenv("MCP_TEST_PYTHON", sys.executable)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": {"tickets": {
        "transport": "stdio", "command": "${MCP_TEST_PYTHON}", "args": [str(SERVER)],
        "env": {"MCP_TEST_LOG": str(log)}, "approval": "writes"}}}), encoding="utf-8")
    monkeypatch.setattr(integrations, "settings", replace(integrations.settings, mcp_config_path=str(config)))
    return log


def test_config_expands_env_skips_missing_and_filters_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret-value")
    monkeypatch.delenv("UNSET_TOKEN", raising=False)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": {
        "ok": {"transport": "streamable_http", "url": "https://example.com/mcp",
               "headers": {"Authorization": "Bearer ${TEST_TOKEN}"}, "agents": ["Coder"]},
        "missing": {"transport": "streamable_http", "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer ${UNSET_TOKEN}"}},
        "odd": {"transport": "stdio", "command": "x", "approval": "sometimes"},
        "bad name!": {"transport": "stdio", "command": "x"},
    }}), encoding="utf-8")
    servers = load_servers(config)
    assert set(servers) == {"ok", "odd"}
    assert servers["ok"]["connection"]["headers"]["Authorization"] == "Bearer secret-value"
    assert servers["ok"]["agents"] == ["Coder"] and "agents" not in servers["ok"]["connection"]
    assert servers["odd"]["approval"] == "always"
    assert asyncio.run(integration_tools("Master", servers={"ok": servers["ok"]})) == []
    assert load_servers(tmp_path / "absent.json") == {}


def test_tools_are_prefixed_and_only_writes_need_approval(tickets):
    tools = {tool.name: tool for tool in asyncio.run(integration_tools("Master"))}
    assert set(tools) == {"tickets__find_ticket", "tickets__create_ticket"}
    assert "pauses for human approval" in tools["tickets__create_ticket"].description
    assert "approval" not in tools["tickets__find_ticket"].description
    result = asyncio.run(tools["tickets__find_ticket"].ainvoke({"title": "Bug"}))
    assert "No ticket named Bug" in str(result)


def test_unreachable_server_is_skipped(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": {"broken": {
        "transport": "stdio", "command": sys.executable, "args": ["-c", "import sys; sys.exit(1)"]}}}), encoding="utf-8")
    monkeypatch.setattr(integrations, "settings", replace(integrations.settings, mcp_config_path=str(config)))
    assert asyncio.run(integration_tools("Master")) == []


def test_write_tool_pauses_for_approval_and_runs_once_after(tmp_path, job, tickets):
    task, _, _ = job
    call = AIMessage(content="", tool_calls=[{"name": "tickets__create_ticket", "id": "mcp-write",
                                             "args": {"title": "Fix login"}}])
    assert run_attempt(tmp_path, task, ScriptedModel(responses=[call])).status == "waiting"
    assert tickets.read_text() == ""  # Nothing was written before approval.
    with db() as session:
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
    assert approval.action == "Use the tickets integration: create_ticket" and "Fix login" in approval.details
    decide_approval(approval.id, approve=True, decided_by="Tester")
    model = RecordingModel(responses=[AIMessage(content="Ticket created.")])
    done = run_attempt(tmp_path, task, model)
    assert done.status == "completed" and done.result == "Ticket created."
    assert tickets.read_text().splitlines() == ["Fix login"]
    assert any("Created ticket Fix login" in str(m.content) for m in model.seen[0] if m.type == "tool")


def test_denied_write_never_reaches_the_server(tmp_path, job, tickets):
    task, _, _ = job
    call = AIMessage(content="", tool_calls=[{"name": "tickets__create_ticket", "id": "mcp-deny",
                                             "args": {"title": "Delete prod"}}])
    run_attempt(tmp_path, task, ScriptedModel(responses=[call]))
    with db() as session:
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
    decide_approval(approval.id, approve=False, decided_by="Tester")
    model = RecordingModel(responses=[AIMessage(content="I did not create it.")])
    assert run_attempt(tmp_path, task, model).status == "completed"
    assert tickets.read_text() == ""
    assert any("Tester denied this tickets action" in str(m.content) for m in model.seen[0] if m.type == "tool")
