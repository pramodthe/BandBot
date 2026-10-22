from __future__ import annotations

import json
import sys
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import codex_login
from app.auth import decrypt_secret, issue_token
from app.main import app
from app.persistence.models import AgentConnection, AgentOAuthAttempt, User, WorkspaceRole, db, init_db


@pytest.fixture
def owner():
    init_db()
    with db() as session:
        owner = User(username=uuid4().hex, display_name="Codex owner", password_hash="unused")
        session.add(owner)
        session.flush()
        session.add(WorkspaceRole(user_id=owner.id, role="admin"))
        session.commit()
    return owner


@pytest.fixture
def cli(tmp_path, monkeypatch):
    script = tmp_path / "codex_login_fixture.py"
    script.write_text('''import json,os,sys,time
from pathlib import Path
def send(data):
    print(json.dumps(data),flush=True)
for line in sys.stdin:
    req=json.loads(line); method=req.get("method"); response={}
    if method=="initialize": response={"userAgent":"offline"}
    elif method=="initialized": continue
    elif method=="account/login/start":
        assert req["params"]=={"type":"chatgpt"}
        assert not os.environ.get("OPENAI_API_KEY")
        assert not os.environ.get("APP_SECRET")
        assert not (Path(os.environ["CODEX_HOME"])/"auth.json").exists()
        response={"type":"chatgpt","loginId":"offline-login","authUrl":"https://auth.openai.com/oauth/authorize?state=offline"}
    elif method=="account/read": response={"account":{"type":"chatgpt","email":"offline@example.test","planType":"plus"}}
    elif method=="model/list": response={"data":[{"model":"gpt-test-codex","isDefault":True}],"nextCursor":None}
    elif method=="thread/start" or method=="turn/start": raise RuntimeError("No inference during login")
    send({"id":req["id"],"result":response})
    if method=="account/login/start" and "--pending" not in sys.argv:
        (Path(os.environ["CODEX_HOME"])/"auth.json").write_text(json.dumps({"auth_mode":"chatgpt","tokens":{"access_token":"offline-access","refresh_token":"offline-refresh"}}))
        send({"method":"account/login/completed","params":{"loginId":"offline-login","success":True,"error":None}})
''', encoding="utf-8")
    monkeypatch.setattr(codex_login, "codex_command", lambda _: [sys.executable, str(script)])
    import app.api.agent_connections as api
    monkeypatch.setattr(api, "runtime_available", lambda _: True)
    return script


def test_native_codex_login_completes_and_saves_encrypted_session(owner, cli):
    headers = {"Authorization": "Bearer " + issue_token(owner.id)}
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/api/agent-connections/codex/login", headers=headers)
        assert response.status_code == 200, response.text
        attempt_id = response.json()["attempt_id"]
        assert response.json()["url"].startswith("https://auth.openai.com/")
        for _ in range(100):
            status = client.get(f"/api/agent-connections/codex/login/{attempt_id}", headers=headers).json()
            if status["status"] != "pending":
                break
            time.sleep(0.02)
        assert status["status"] == "connected", status
        with db() as session:
            saved = session.get(AgentConnection, status["connection_id"])
            assert saved.auth_method == "codex_chatgpt" and saved.models == ["gpt-test-codex"]
            assert "offline-refresh" not in saved.secret_enc
            assert json.loads(decrypt_secret(saved.secret_enc))["auth_json"]["tokens"]["refresh_token"] == "offline-refresh"
        public = client.get("/api/agent-connections", headers=headers)
        assert "offline-refresh" not in public.text and "offline-access" not in public.text
        # The polling effect cleans up after success; this must not undo the session.
        cleanup = client.delete(f"/api/agent-connections/codex/login/{attempt_id}", headers=headers)
        assert cleanup.json() == {"cancelled": False}
        assert client.get(f"/api/agent-connections/codex/login/{attempt_id}", headers=headers).json()["status"] == "connected"


def test_native_login_owner_cancel_and_host_restrictions(owner, cli, monkeypatch):
    monkeypatch.setattr(codex_login, "codex_command", lambda _: [sys.executable, str(cli), "--pending"])
    headers = {"Authorization": "Bearer " + issue_token(owner.id)}
    with TestClient(app, base_url="https://hosted.example") as client:
        assert client.post("/api/agent-connections/codex/login", headers=headers).status_code == 409
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post("/api/agent-connections/codex/login").status_code == 401
        response = client.post("/api/agent-connections/codex/login", headers=headers)
        assert response.status_code == 200
        attempt_id = response.json()["attempt_id"]
        assert client.post("/api/agent-connections/codex/login", headers=headers).status_code == 409
        assert client.delete(f"/api/agent-connections/codex/login/{attempt_id}", headers=headers).status_code == 200
        with db() as session:
            assert session.get(AgentOAuthAttempt, attempt_id).status == "cancelled"


@pytest.mark.parametrize("url", ["https://evil.test/", "http://auth.openai.com/", "https://auth.openai.com.evil.test/", "https://user@auth.openai.com/"])
def test_native_login_rejects_untrusted_auth_url(url):
    with pytest.raises(ValueError):
        codex_login.login_url(url)
