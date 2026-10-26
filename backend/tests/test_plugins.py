"""Composio plugin session, allow-list, and admin connect flow. No live Composio calls."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlmodel import select

import app.runtime.tools.composio as composio
import app.runtime.tools.mcp as integrations
from app.auth import encrypt_secret
from app.main import app
from app.persistence.models import AgentProfile, ComposioSession, User, WorkspaceRole, db, init_db, utcnow
from app.runtime.tools.composio import call_refusal, set_plugin_access, trusted_url
from app.runtime.tools.mcp import integration_tools
from helpers import signup
SESSION = {
    "session_id": "sess_1",
    "mcp": {"type": "http", "url": "https://backend.composio.dev/mcp/sess_1"},
    "config": {"user_id": "bandslack_test"},
}


class _Args(BaseModel):
    tools: list[dict[str, Any]] | None = None


def _clear_session() -> None:
    init_db()
    with db() as session:
        for row in session.exec(select(ComposioSession)).all():
            session.delete(row)
        session.commit()


def _enable(monkeypatch):
    monkeypatch.setattr(composio, "provider_api_key", lambda: "ak_test")
    monkeypatch.setattr(composio, "selected_composio", lambda: None)


def test_mcp_urls_must_be_composio_https():
    assert trusted_url("https://backend.composio.dev/mcp/sess_1")
    assert trusted_url("https://mcp.composio.dev/session")
    assert not trusted_url("http://backend.composio.dev/mcp/sess_1")
    assert not trusted_url("https://evil.example/mcp")
    assert not trusted_url("https://composio.dev.evil.com/mcp")


def test_session_is_reused_and_untrusted_urls_are_rejected(monkeypatch):
    _clear_session()
    _enable(monkeypatch)
    calls: list[str] = []

    async def send(method, url, *, json_body=None, params=None):
        calls.append(f"{method} {url}")
        if method == "POST":
            return httpx.Response(200, json=SESSION)
        return httpx.Response(200, json=SESSION)

    monkeypatch.setattr(composio, "_send", send)
    first = asyncio.run(composio.ensure_session())
    second = asyncio.run(composio.ensure_session())
    assert first["session_id"] == second["session_id"] == "sess_1"
    assert calls == [
        "POST https://backend.composio.dev/api/v3.1/tool_router/session",
        "GET https://backend.composio.dev/api/v3.1/tool_router/session/sess_1",
    ]

    async def evil(method, url, *, json_body=None, params=None):
        return httpx.Response(200, json={
            "session_id": "sess_bad",
            "mcp": {"type": "http", "url": "https://evil.example/mcp"},
        })

    _clear_session()
    monkeypatch.setattr(composio, "_send", evil)
    with pytest.raises(composio.ComposioError, match="untrusted"):
        asyncio.run(composio.ensure_session())
    with db() as session:
        assert session.exec(select(ComposioSession)).first() is None


def test_gmail_only_agent_cannot_execute_slack():
    init_db()
    set_plugin_access(900001, ["gmail"])
    assert call_refusal(900001, "COMPOSIO_SEARCH_TOOLS", {}) is None
    assert call_refusal(900001, "COMPOSIO_MULTI_EXECUTE_TOOL", {
        "tools": [{"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}}],
    }) is None
    refused = call_refusal(900001, "COMPOSIO_MULTI_EXECUTE_TOOL", {
        "tools": [{"tool_slug": "SLACK_SEND_MESSAGE", "arguments": {}}],
    })
    assert refused is not None and "SLACK_SEND_MESSAGE" in refused and "not granted" in refused
    assert call_refusal(900001, "COMPOSIO_REMOTE_WORKBENCH", {"code": "print(1)"})
    assert call_refusal(900001, "COMPOSIO_MULTI_EXECUTE_TOOL", {"nope": True})
    set_plugin_access(900001, None)
    assert call_refusal(900001, "COMPOSIO_MULTI_EXECUTE_TOOL", {
        "tools": [{"tool_slug": "SLACK_SEND_MESSAGE", "arguments": {}}],
    }) is None


def test_composio_tools_keep_meta_tools_and_enforce_the_allow_list(monkeypatch):
    init_db()
    set_plugin_access(900002, ["gmail"])

    async def boom(**_arguments):
        raise AssertionError("the upstream tool ran")

    def named(name: str) -> StructuredTool:
        return StructuredTool(name=name, description=name, args_schema=_Args, coroutine=boom)

    class FakeClient:
        def __init__(self, _servers):
            pass

        async def get_tools(self, server_name):
            assert server_name == "composio"
            return [
                named("COMPOSIO_SEARCH_TOOLS"),
                named("COMPOSIO_GET_TOOL_SCHEMAS"),
                named("COMPOSIO_MULTI_EXECUTE_TOOL"),
                named("COMPOSIO_MANAGE_CONNECTIONS"),
                named("COMPOSIO_REMOTE_BASH_TOOL"),
                named("GMAIL_SEND_EMAIL"),
            ]

    monkeypatch.setattr(integrations, "MultiServerMCPClient", FakeClient)
    tools = {tool.name: tool for tool in asyncio.run(integration_tools("Master", servers={"composio": {
        "connection": {"transport": "streamable_http", "url": "https://backend.composio.dev/mcp"},
        "approval": "writes", "agents": None,
    }}, agent_id=900002))}
    assert set(tools) == {
        "composio__COMPOSIO_SEARCH_TOOLS",
        "composio__COMPOSIO_GET_TOOL_SCHEMAS",
        "composio__COMPOSIO_MULTI_EXECUTE_TOOL",
        "composio__COMPOSIO_REMOTE_BASH_TOOL",
    }
    assert "pauses for human approval" not in tools["composio__COMPOSIO_SEARCH_TOOLS"].description
    assert "pauses for human approval" in tools["composio__COMPOSIO_MULTI_EXECUTE_TOOL"].description
    refused = asyncio.run(tools["composio__COMPOSIO_MULTI_EXECUTE_TOOL"].ainvoke(
        {"tools": [{"tool_slug": "SLACK_SEND_MESSAGE", "arguments": {}}]}))
    assert "SLACK_SEND_MESSAGE" in str(refused)


def test_discovery_metadata_scopes_external_apps_without_breaking_calls():
    class SearchArgs(BaseModel):
        queries: list[dict[str, str]]

    calls = []

    async def discover(**arguments):
        calls.append(arguments)
        return ("Google Drive action found", None)

    upstream = StructuredTool(name="COMPOSIO_SEARCH_TOOLS",
        description="Use this tool whenever kicking off a task. Always call it first.",
        args_schema=SearchArgs, coroutine=discover, metadata={"readOnlyHint": True})
    wrapped = integrations._wrap("composio", upstream, False)
    # A third-party tool's global workflow instruction must not leak into the model-facing contract.
    assert "whenever kicking off a task" not in wrapped.description
    assert "search_documents" in wrapped.description and "external connected app" in wrapped.description
    assert wrapped.args_schema is SearchArgs and wrapped.metadata == upstream.metadata
    arguments = {"queries": [{"use_case": "Find a report in Google Drive", "known_fields": "name: report"}]}
    assert asyncio.run(wrapped.ainvoke(arguments)) == "Google Drive action found"
    assert calls == [arguments]
    other = integrations._wrap("other", upstream, False)
    assert upstream.description in other.description


def test_plugins_are_readable_by_members_and_connected_by_admins(monkeypatch):
    with TestClient(app) as client:
        alice = signup(client, "plugin-admin", "Ada")
        bob = signup(client, "plugin-member", "Bea")
        with db() as session:
            account = session.exec(select(User).where(User.username == "plugin-admin")).one()
            session.merge(WorkspaceRole(user_id=account.id, role="admin", updated_at=utcnow()))
            session.commit()
        admin = {"Authorization": f"Bearer {alice['token']}"}
        member = {"Authorization": f"Bearer {bob['token']}"}

        listed = client.get("/api/plugins?q=gmail", headers=member)
        assert listed.status_code == 200
        body = listed.json()
        assert body["configured"] is False
        assert any(plugin["slug"] == "gmail" and plugin["connected"] is False for plugin in body["plugins"])
        assert client.post("/api/plugins/gmail/connect", headers=member).status_code == 403
        assert client.delete("/api/plugins/gmail", headers=member).status_code == 403

        calls: list[str] = []

        async def send(method, url, *, json_body=None, params=None):
            calls.append(f"{method} {url}")
            if url.endswith("/link"):
                assert json_body == {"toolkit": "gmail"}
                return httpx.Response(200, json={"redirect_url": "https://connect.composio.dev/link/abc"})
            if url.endswith("/tool_router/session"):
                return httpx.Response(200, json=SESSION)
            if url.endswith("/connected_accounts"):
                return httpx.Response(200, json={"items": [{"id": "ca_1"}]})
            if method == "DELETE":
                return httpx.Response(204)
            if "/toolkits" in url:
                return httpx.Response(200, json={"items": [{
                    "slug": "gmail", "name": "Gmail",
                    "meta": {"description": "Read and send email", "logo": "http://insecure.example/logo.png"},
                }]})
            if "/tool_router/session/sess_1" in url:
                return httpx.Response(200, json=SESSION)
            raise AssertionError(f"{method} {url}")

        _enable(monkeypatch)
        monkeypatch.setattr(composio, "_send", send)
        _clear_session()
        blocked = client.post("/api/plugins/gmail/connect", headers=member)
        assert blocked.status_code == 403
        linked = client.post("/api/plugins/gmail/connect", headers=admin)
        assert linked.status_code == 200
        assert linked.json()["url"] == "https://connect.composio.dev/link/abc"
        assert client.post("/api/plugins/GMAIL/connect", headers=admin).status_code == 422
        removed = client.delete("/api/plugins/gmail", headers=admin)
        assert removed.status_code == 200 and removed.json()["connected"] is False
        assert any(call.startswith("DELETE ") for call in calls)
        catalog = client.get("/api/plugins", headers=member).json()
        assert catalog["configured"] is True
        gmail = next(plugin for plugin in catalog["plugins"] if plugin["slug"] == "gmail")
        assert gmail["logo"] is None


def test_agent_plugin_selection_round_trips():
    with TestClient(app) as client:
        alice = signup(client, "plugin-editor", "Eden")
        auth = {"Authorization": f"Bearer {alice['token']}"}
        with db() as session:
            profile = AgentProfile(name="Mailer", handle="", band_agent_id="agent-mailer",
                                   band_api_key_enc=encrypt_secret("k"))
            session.add(profile)
            session.commit()
            session.refresh(profile)
            agent_id = profile.id
        limited = client.patch(f"/api/agents/{agent_id}", json={"plugins": ["gmail", "gmail"]}, headers=auth)
        assert limited.status_code == 200, limited.text
        assert limited.json()["plugins"] == ["gmail"]
        assert client.patch(f"/api/agents/{agent_id}", json={"plugins": ["Gmail"]}, headers=auth).status_code == 422
        cleared = client.patch(f"/api/agents/{agent_id}", json={"plugins": None}, headers=auth)
        assert cleared.status_code == 200 and cleared.json()["plugins"] is None
        none = client.patch(f"/api/agents/{agent_id}", json={"plugins": []}, headers=auth)
        assert none.json()["plugins"] == []
