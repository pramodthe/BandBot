from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from types import SimpleNamespace

import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select

from app import agent_connections as connections
from app.api import agent_connections as api
from app.auth import decrypt_secret, encrypt_secret, issue_token
from app.main import app
from app.openai_provider import migrate_profiles
from app.persistence.models import (AgentConnection, AgentOAuthAttempt, AgentProfile, AgentRuntime, AgentTask,
    Channel, ChannelMember, TaskApproval, TaskModel, TaskUsage, User, WorkspaceRole, db, init_db, utcnow)
from app.runtime import connected
from app.runtime.connected import ConnectedRuntimeModel, isolated_environment
from app.runtime.connected_runner import CODEX_SETTINGS, decision_schema, request_content
from app.tasks import decide_approval, enqueue, update_task
from app.tasks.worker import execute, process


@pytest.fixture
def accounts():
    init_db()
    with db() as session:
        admin = User(username=uuid4().hex, display_name="Runtime admin", password_hash="unused")
        member = User(username=uuid4().hex, display_name="Runtime member", password_hash="unused")
        session.add_all([admin, member])
        session.flush()
        session.add(WorkspaceRole(user_id=admin.id, role="admin"))
        session.commit()
    return admin, member


def auth(user):
    return {"Authorization": f"Bearer {issue_token(user.id)}"}


def test_connections_belong_to_their_creator_and_never_return_credentials(accounts, monkeypatch):
    admin, member = accounts
    secret = "sk-offline-key-" + uuid4().hex

    async def validate(provider, key):
        assert provider == "claude" and key == secret
        return ["claude-test"]

    monkeypatch.setattr(api, "validate_key", validate)
    with TestClient(app) as client:
        assert client.get("/api/agent-connections").status_code == 401
        assert client.get("/api/agent-connections", headers=auth(member)).status_code == 200
        body = {"provider": "claude", "label": "Test account", "api_key": secret}
        member_connection = client.post("/api/agent-connections", headers=auth(member), json=body)
        assert member_connection.status_code == 200
        result = client.post("/api/agent-connections", headers=auth(admin), json=body)
        assert result.status_code == 200, result.text
        assert result.json()["models"] == ["claude-test"]
        listing = client.get("/api/agent-connections", headers=auth(admin))
        assert member_connection.json()["id"] not in {item["id"] for item in listing.json()["connections"]}
        assert secret not in listing.text and "secret_enc" not in listing.text and secret not in result.text
        with db() as session:
            row = session.get(AgentConnection, result.json()["id"])
            assert secret not in row.secret_enc
            assert json.loads(decrypt_secret(row.secret_enc))["api_key"] == secret
        assert client.delete(f"/api/agent-connections/{row.id}", headers=auth(member)).status_code == 404
        assert client.delete(f"/api/agent-connections/{row.id}", headers=auth(admin)).status_code == 200


def test_authentication_errors_are_sanitized_and_do_not_save_keys(accounts, monkeypatch):
    async def validate(*args):
        raise ValueError("Authorization: Bearer secret-provider-header")

    monkeypatch.setattr(api, "validate_key", validate)
    with TestClient(app) as client:
        result = client.post("/api/agent-connections", headers=auth(accounts[0]),
            json={"provider": "codex", "label": "Rejected connection", "api_key": "sk-offline-rejected"})
        assert result.status_code == 422
        assert "secret-provider-header" not in result.text
        with db() as session:
            assert not session.exec(select(AgentConnection).where(AgentConnection.label == "Rejected connection")).first()
        malformed = client.post("/api/agent-connections", headers=auth(accounts[0]),
            json={"provider": "codex", "label": "Invalid", "api_key": {"secret": "must-not-be-returned"}})
        assert malformed.status_code == 422 and "must-not-be-returned" not in malformed.text


@pytest.mark.parametrize("provider,model", [("codex", "gpt-test"), ("claude", "claude-test"),
                                         ("cursor", "composer-test"), ("openclaw", "openai/gpt-test")])
def test_connected_tasks_pin_their_provider_and_survive_legacy_migration(accounts, provider, model):
    row = connections.save_connection(provider, "Offline", {"api_key": "offline"}, accounts[0].id)
    with db() as session:
        agent = AgentProfile(name="Connected " + uuid4().hex, model=model, band_agent_id=uuid4().hex, band_api_key_enc="unused")
        channel = Channel(name=uuid4().hex)
        session.add_all([agent, channel])
        session.flush()
        session.add(AgentRuntime(agent_id=agent.id, connection_id=row.id, provider=provider))
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=accounts[0].id))
        session.commit()
    task = enqueue(agent_id=agent.id, channel_id=channel.id, room_id="", source_id=uuid4().hex, prompt="Hello")
    migrate_profiles()
    with db() as session:
        pinned = session.get(TaskModel, task.id)
        assert (pinned.provider, pinned.model) == (provider, model)
        assert session.get(AgentProfile, agent.id).model == model
    with TestClient(app) as client:
        assert client.delete(f"/api/agent-connections/{row.id}", headers=auth(accounts[0])).status_code == 409
        update_task(task.id, status="failed")
        retried = client.post(f"/api/tasks/{task.id}/retry", headers=auth(accounts[0]))
        assert retried.status_code == 200 and retried.json()["id"] == task.id
        assert retried.json()["status"] == "queued"
        with db() as session:
            assert session.get(TaskModel, task.id).provider == provider


def test_connected_agent_creation_rejects_wrong_connection_before_band_registration(accounts, monkeypatch):
    row = connections.save_connection("claude", "Offline", {"api_key": "offline"}, accounts[0].id)
    import app.api.http as http
    monkeypatch.setattr(http, "runtime_available", lambda provider: True)
    with TestClient(app) as client:
        body = {"name": "Codex test", "runtime": "codex", "model": "gpt-test", "connection_id": row.id}
        assert client.post("/api/agents", json=body, headers=auth(accounts[1])).status_code == 403
        assert client.post("/api/agents", json=body, headers=auth(accounts[0])).status_code == 422


@pytest.mark.parametrize("provider", ["cursor", "openclaw"])
def test_new_connections_encrypt_secrets_and_allow_members_own_accounts(accounts, monkeypatch, provider):
    secret = "offline-provider-secret"

    async def validate(*args):
        return ["openai/gpt-test"] if provider == "openclaw" else ["composer-test"]

    monkeypatch.setattr(api, "validate_key", validate)
    monkeypatch.setattr(api, "validate_gateway", validate)
    body = {"provider": provider, "label": "New connection", "api_key": secret}
    if provider == "openclaw":
        body.update(gateway_url="https://gateway.example.test", gateway_agent_id="band")
    with TestClient(app) as client:
        assert client.post("/api/agent-connections", json=body, headers=auth(accounts[1])).status_code == 200
        result = client.post("/api/agent-connections", json=body, headers=auth(accounts[0]))
        assert result.status_code == 200, result.text
        assert secret not in result.text
        with db() as session:
            saved = session.get(AgentConnection, result.json()["id"])
            stored = json.loads(decrypt_secret(saved.secret_enc))
            assert stored["api_key"] == secret and secret not in saved.secret_enc
            if provider == "openclaw":
                assert saved.auth_method == "gateway_token" and stored["agent_id"] == "band"
        assert secret not in client.get("/api/agent-connections", headers=auth(accounts[0])).text


def test_openclaw_connection_checks_configuration_without_disclosing_errors(accounts, monkeypatch):
    async def refused(*args):
        raise ValueError("secret-gateway-token internal provider error")

    monkeypatch.setattr(api, "validate_gateway", refused)
    with TestClient(app) as client:
        body = {"provider": "openclaw", "label": "Rejected gateway", "api_key": "offline-secret"}
        assert client.post("/api/agent-connections", json=body, headers=auth(accounts[0])).status_code == 422
        body.update(gateway_url="https://not-approved.test", gateway_agent_id="band")
        result = client.post("/api/agent-connections", json=body, headers=auth(accounts[0]))
        assert result.status_code == 422 and "secret-gateway-token" not in result.text
        with db() as session:
            assert session.exec(select(AgentConnection).where(AgentConnection.label == body["label"])).first() is None


def test_oauth_pkce_owner_expiry_and_single_use_state(accounts, monkeypatch):
    monkeypatch.setattr(api, "settings", replace(api.settings, codex_chatgpt_sign_in=True))
    with TestClient(app, base_url="http://localhost") as client:
        first = client.post("/api/agent-connections/codex/sign-in", headers=auth(accounts[0])).json()
        query = parse_qs(urlsplit(first["url"]).query)
        assert query["client_id"] == ["dynamic_agent_client"]
        assert query["code_challenge_method"] == ["S256"]
        assert "chatgpt.tokens.use.direct" in query["scope"][0]
        with db() as session:
            attempt = session.get(AgentOAuthAttempt, first["attempt_id"])
            assert attempt.state_hash == hashlib.sha256(query["state"][0].encode()).hexdigest()
            assert decrypt_secret(attempt.verifier_enc) not in first["url"]
        assert client.get(f"/api/agent-connections/codex/sign-in/{attempt.id}", headers=auth(accounts[1])).status_code == 404
        second = client.post("/api/agent-connections/codex/sign-in", headers=auth(accounts[0])).json()
        assert client.get("/auth/callback", params={"state": query["state"][0], "error": "access_denied"}).status_code == 400
        state = parse_qs(urlsplit(second["url"]).query)["state"][0]
        assert client.get("/auth/callback", params={"state": state, "error": "access_denied"}).status_code == 200
        assert client.get("/auth/callback", params={"state": state}).status_code == 400
        assert client.get(f"/api/agent-connections/codex/sign-in/{second['attempt_id']}", headers=auth(accounts[0])).json()["status"] == "failed"
        third = client.post("/api/agent-connections/codex/sign-in", headers=auth(accounts[0])).json()
        with db() as session:
            expired = session.get(AgentOAuthAttempt, third["attempt_id"])
            expired.created_at = utcnow() - timedelta(minutes=11)
            session.add(expired)
            session.commit()
        assert client.get("/auth/callback", params={"state": parse_qs(urlsplit(third["url"]).query)["state"][0]}).status_code == 400


def test_hosted_oauth_flow_is_blocked(accounts, monkeypatch):
    monkeypatch.setattr(api, "settings", replace(api.settings, codex_chatgpt_sign_in=True))
    with TestClient(app, base_url="https://hosted.example") as client:
        assert client.post("/api/agent-connections/codex/sign-in", headers=auth(accounts[0])).status_code == 409


def test_oauth_callback_access_logs_omit_authorization_codes():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/auth/callback?code=secret-code&state=secret-state", "1.1", 200), None)
    assert api.OAuthAccessFilter().filter(record)
    assert "secret-code" not in record.getMessage() and "secret-state" not in record.getMessage()


@pytest.mark.parametrize("invalid", [None, "nonce", "scope", "audience"])
def test_oauth_verifies_signed_identity_and_plan_permission_before_saving(accounts, monkeypatch, invalid):
    monkeypatch.setattr(api, "settings", replace(api.settings, codex_chatgpt_sign_in=True))
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {"kid": "offline"}
    with TestClient(app, base_url="http://localhost") as client:
        start = client.post("/api/agent-connections/codex/sign-in", headers=auth(accounts[0])).json()
        state = parse_qs(urlsplit(start["url"]).query)["state"][0]
        with db() as session:
            attempt = session.get(AgentOAuthAttempt, start["attempt_id"])
        claims = {"iss": "https://auth.openai.com", "aud": "wrong" if invalid == "audience" else "oaiapp_offline",
                  "sub": "offline-subject", "iat": int(utcnow().timestamp()), "exp": int(utcnow().timestamp()) + 300,
                  "nonce": "wrong" if invalid == "nonce" else attempt.nonce, "email": "offline@example.test"}
        identity = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "offline"})
        tokens = {"id_token": identity, "access_token": "offline-access", "refresh_token": "offline-refresh",
                  "expires_in": 3600, "scope": "openid" if invalid == "scope" else "openid chatgpt.tokens.use.direct"}
        calls = []

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, data):
                calls.append(data)
                assert data["code_verifier"] == decrypt_secret(attempt.verifier_enc)
                assert data["redirect_uri"] == attempt.redirect_uri
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: tokens)

            async def get(self, url):
                assert url == "https://auth.openai.com/.well-known/jwks.json"
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"keys": [jwk]})

        monkeypatch.setattr(api.httpx, "AsyncClient", FakeClient)
        response = client.get("/auth/callback", params={"state": state, "code": "offline-code", "client_id": "oaiapp_offline"})
        assert response.status_code == 200
        assert "offline-access" not in response.text and "offline-refresh" not in response.text
        status = client.get(f"/api/agent-connections/codex/sign-in/{attempt.id}", headers=auth(accounts[0])).json()
        assert status["status"] == ("connected" if invalid is None else "failed")
        assert len(calls) == 1
        with db() as session:
            row = session.get(AgentOAuthAttempt, attempt.id)
            assert row.verifier_enc == ""
            if invalid is None:
                connection = session.get(AgentConnection, row.connection_id)
                assert connection.auth_method == "chatgpt"
                assert json.loads(decrypt_secret(connection.secret_enc))["client_id"] == "oaiapp_offline"
            else:
                assert row.connection_id is None
        assert client.get("/auth/callback", params={"state": state, "code": "offline-code", "client_id": "oaiapp_offline"}).status_code == 400


def test_runtime_environment_excludes_host_credentials(monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "E2B_API_KEY", "BAND_USER_API_KEY", "APP_SECRET", "PYTHONPATH", "NODE_OPTIONS"):
        monkeypatch.setenv(name, "must-not-inherit")
    env = isolated_environment("isolated-directory", "only-this-connection")
    assert "must-not-inherit" not in env.values()
    assert env["CODEX_HOME"] == env["CLAUDE_CONFIG_DIR"] == "isolated-directory"
    assert CODEX_SETTINGS["features.shell_tool"] is False
    assert CODEX_SETTINGS["include_apply_patch_tool"] is False
    assert CODEX_SETTINGS["tools.disable_defaults"] is True


@pytest.mark.parametrize("fails", [False, True])
def test_native_codex_session_is_scoped_and_refresh_rotation_is_saved(accounts, tmp_path, monkeypatch, fails):
    row = connections.save_connection("codex", "Codex session", {"auth_json": {"tokens": {
        "access_token": "offline-access", "refresh_token": "old-refresh"}}}, accounts[0].id, method="codex_chatgpt")
    script = tmp_path / "connected_runner.py"
    script.write_text('''import json,os,sys
from pathlib import Path
payload=json.load(sys.stdin)
assert os.environ['BAND_RUNTIME_TOKEN']==''
assert 'OPENAI_API_KEY' not in os.environ
path=Path(os.environ['CODEX_HOME'])/'auth.json'
auth=json.loads(path.read_text())
assert auth['tokens']['refresh_token']=='old-refresh'
auth['tokens']['refresh_token']='rotated-refresh'
path.write_text(json.dumps(auth))
if payload.get('fail'): sys.exit(1)
print(json.dumps({'decision':{'content':'Ready','tool_calls':[]},'usage':{'input_tokens':1,'output_tokens':1}}))
''', encoding="utf-8")
    monkeypatch.setattr(connected, "__file__", str(tmp_path / "connected.py"))
    _, token = asyncio.run(connections.credential(row.id))
    call = connected.run_step("codex", "gpt-test", token,
        {"auth_method": "codex_chatgpt", "connection_id": row.id, "fail": fails})
    if fails:
        with pytest.raises(ValueError, match="runtime failed"):
            asyncio.run(call)
    else:
        assert asyncio.run(call)["decision"]["content"] == "Ready"
    with db() as session:
        saved = session.get(AgentConnection, row.id)
        assert "rotated-refresh" not in saved.secret_enc
        assert json.loads(decrypt_secret(saved.secret_enc))["auth_json"]["tokens"]["refresh_token"] == "rotated-refresh"


def test_runtime_child_receives_only_its_credential_and_cancels_cleanly(tmp_path, monkeypatch):
    runner = tmp_path / "connected_runner.py"
    runner.write_text('''import json, os, sys, time
from pathlib import Path
payload = json.load(sys.stdin)
if payload.get("marker"):
    Path(payload["marker"]).write_text(str(os.getpid()))
    time.sleep(60)
else:
    assert os.environ["BAND_RUNTIME_TOKEN"] == "offline-only"
    assert "APP_SECRET" not in os.environ and "OPENAI_API_KEY" not in os.environ
    print(json.dumps({"decision": {"content": "Isolated child verified", "tool_calls": []}, "usage": {"input_tokens": 1, "output_tokens": 1}}))
''', encoding="utf-8")
    monkeypatch.setattr(connected, "__file__", str(tmp_path / "connected.py"))
    monkeypatch.setenv("APP_SECRET", "must-not-inherit")
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(connected.asyncio, "create_subprocess_exec", capture)

    async def scenario():
        result = await connected.run_step("codex", "offline", "offline-only", {})
        assert result["decision"]["content"] == "Isolated child verified"
        marker = tmp_path / "started.txt"
        task = asyncio.create_task(connected.run_step("codex", "offline", "offline-only", {"marker": str(marker)}))
        async with asyncio.timeout(15):
            while not marker.exists():
                await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode == 0
        assert processes[1].returncode is not None
    asyncio.run(scenario())


def test_images_are_transport_images_instead_of_base64_prompt_text():
    prompt, images = request_content({"messages": [{"type": "tool", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,offline-pixels"}}]}], "tools": []})
    assert "offline-pixels" not in prompt
    assert len(images) == 1
    assert decision_schema(["remember"])["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["remember"]


def test_runtime_rejects_unknown_tools_and_unmetered_responses(accounts, monkeypatch):
    row = connections.save_connection("codex", "Offline", {"api_key": "offline"}, accounts[0].id)
    model = ConnectedRuntimeModel(provider="codex", model_name="gpt-test", connection_id=row.id)

    async def unknown(*args):
        return {"decision": {"content": "", "tool_calls": [{"name": "host_shell", "arguments_json": "{}"}]}, "usage": {"input_tokens": 5}}

    monkeypatch.setattr(connected, "run_step", unknown)
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(model.ainvoke([HumanMessage(content="Hello")]))

    async def unmetered(*args):
        return {"decision": {"content": "Hello", "tool_calls": []}}

    monkeypatch.setattr(connected, "run_step", unmetered)
    with pytest.raises(ValueError, match="unmetered"):
        asyncio.run(model.ainvoke([HumanMessage(content="Hello")]))


@pytest.mark.parametrize("provider", ["codex", "claude", "cursor", "openclaw"])
def test_connected_runtime_uses_real_checkpoint_approval_and_usage_flow(accounts, monkeypatch, tmp_path, provider):
    row = connections.save_connection(provider, "Offline", {"api_key": "only-this-key"}, accounts[0].id)
    with db() as session:
        agent = AgentProfile(name="Runtime " + uuid4().hex, model="claude-test" if provider == "claude" else "gpt-test",
                             band_agent_id=uuid4().hex, band_api_key_enc=encrypt_secret("offline"))
        channel = Channel(name=uuid4().hex)
        session.add_all([agent, channel])
        session.flush()
        session.add_all([AgentRuntime(agent_id=agent.id, connection_id=row.id, provider=provider),
                         ChannelMember(channel_id=channel.id, member_type="human", member_id=accounts[0].id)])
        session.commit()
    task = enqueue(agent_id=agent.id, channel_id=channel.id, room_id="", source_id=uuid4().hex, prompt="Ask before posting")
    calls = []

    async def fake_step(actual_provider, model, token, payload):
        assert actual_provider == provider and token == "only-this-key"
        assert any(tool["name"] == "request_approval" for tool in payload["tools"])
        calls.append(payload)
        decision = ({"content": "", "tool_calls": [{"name": "request_approval", "arguments_json": json.dumps({
            "action": "Post the prepared update", "details": "Publish the reviewed text"})}]} if len(calls) == 1 else
                    {"content": "Approval received. Ready to proceed.", "tool_calls": []})
        return {"decision": decision, "usage": {"input_tokens": 20, "output_tokens": 10}}

    monkeypatch.setattr(connected, "run_step", fake_step)
    if provider == "openclaw":
        import app.openclaw_gateway as gateway

        async def gateway_step(secret, model, payload):
            return await fake_step("openclaw", model, secret["api_key"], payload)

        monkeypatch.setattr(gateway, "run_gateway_step", gateway_step)

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "runtime.db")) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            await process(task, saver)
            with db() as session:
                current = session.get(AgentTask, task.id)
                assert current.status == "waiting", current.error
                approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
            assert len(calls) == 1
            decide_approval(approval.id, approve=True, decided_by="Runtime admin")
            update_task(task.id, status="running")
            await process(task, saver)
            with db() as session:
                current = session.get(AgentTask, task.id)
                assert current.status == "completed", current.error
                assert "Approval received" in current.result
                assert len(session.exec(select(TaskUsage).where(TaskUsage.task_id == task.id)).all()) == 2
            # A completed graph replays its saved answer without another vendor call.
            assert await execute(task, saver) == "Approval received. Ready to proceed."
            assert len(calls) == 2
    asyncio.run(scenario())
