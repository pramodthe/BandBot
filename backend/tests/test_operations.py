"""Sessions, recovery, people controls, worker health, and provider status."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from sqlmodel import select
from starlette.websockets import WebSocketDisconnect

from app.auth import issue_token, require_production_secret, token_payload, user_id_from_token
from app.auth_throttle import AttemptLimiter, GENERIC_LOCKOUT
from app.config import settings
from app.main import app
from app.persistence.models import (AgentConnection, AgentProfile, AgentRuntime, AgentTask, AppContact,
                                    AppContactRequest, AuthSession, ChannelInvitation, Message, UserBandAccount,
                                    db, utcnow)
from app.recovery import clear_outbox, outbox
from app.workspace.health import beat, mark_api_started, remember_provider_state, service_status, worker_state
from app.auth import encrypt_secret


def _signup(client: TestClient, username: str) -> dict:
    response = client.post("/api/auth/signup", json={
        "username": username, "display_name": username.title(), "password": "secret123",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    body["headers"] = {"Authorization": f"Bearer {body['token']}"}
    return body


def _assign(name: str, value: str) -> None:
    object.__setattr__(settings, name, value)


def test_production_rejects_an_unsafe_secret_without_echoing_it():
    previous = (settings.band_env, settings.recovery_delivery, settings.app_secret)
    try:
        _assign("band_env", "production")
        _assign("recovery_delivery", "none")
        _assign("app_secret", "dev-secret-change-me")
        with pytest.raises(SystemExit) as raised:
            require_production_secret()
        message = str(raised.value)
        assert "APP_SECRET" in message
        assert "dev-secret-change-me" not in message
        _assign("app_secret", "x" * 40)
        _assign("recovery_delivery", "memory")
        with pytest.raises(SystemExit) as raised:
            require_production_secret()
        assert "memory" in str(raised.value)
        assert "x" * 40 not in str(raised.value)
        _assign("band_env", "test")
        require_production_secret()
    finally:
        _assign("band_env", previous[0])
        _assign("recovery_delivery", previous[1])
        _assign("app_secret", previous[2])


def test_signup_and_login_attempts_are_limited(monkeypatch):
    import app.api.http as http
    from app.auth import hash_password
    from app.persistence.models import User

    monkeypatch.setattr(http, "signup_limiter", AttemptLimiter(2, 600))
    monkeypatch.setattr(http, "login_limiter", AttemptLimiter(2, 600))
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post("/api/auth/signup", json={"username": f"lim{uuid4().hex[:6]}", "display_name": "Lim", "password": "secret123"}).status_code == 200
        locked = client.post("/api/auth/signup", json={"username": f"lim{uuid4().hex[:6]}", "display_name": "Lim", "password": "secret123"})
        assert locked.status_code == 429
        assert locked.json()["detail"] == GENERIC_LOCKOUT
        name = f"log{uuid4().hex[:6]}"
        with db() as session:
            session.add(User(username=name, display_name="Log", password_hash=hash_password("secret123")))
            session.commit()
        first = client.post("/api/auth/login", json={"username": name, "password": "wrong"})
        assert first.status_code == 401
        assert first.json()["detail"] == "Wrong username or password"
        second = client.post("/api/auth/login", json={"username": name, "password": "wrong"})
        assert second.status_code == 429
        assert second.json()["detail"] == GENERIC_LOCKOUT
        third = client.post("/api/auth/login", json={"username": name, "password": "secret123"})
        assert third.status_code == 429
        assert name not in third.text


def test_revoked_expired_and_legacy_tokens_are_rejected():
    with TestClient(app) as client:
        user = _signup(client, f"sess{uuid4().hex[:8]}")
        other = issue_token(user["user"]["id"])
        other_headers = {"Authorization": f"Bearer {other}"}
        assert client.get("/api/bootstrap", headers=other_headers).status_code == 200
        assert client.post("/api/auth/logout", headers=user["headers"]).status_code == 200
        assert client.get("/api/bootstrap", headers=user["headers"]).status_code == 401
        assert client.post("/api/auth/logout-all", headers=other_headers).status_code == 200
        assert client.get("/api/bootstrap", headers=other_headers).status_code == 401
        unsigned = URLSafeTimedSerializer(settings.app_secret, salt="band-slack-session").dumps({"uid": user["user"]["id"]})
        assert client.get("/api/bootstrap", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401
        assert user_id_from_token(unsigned) == user["user"]["id"]
        fresh = issue_token(user["user"]["id"])
        with db() as session:
            row = session.get(AuthSession, token_payload(fresh)["sid"])
            row.expires_at = utcnow() - timedelta(seconds=30)
            session.add(row)
            session.commit()
        assert client.get("/api/bootstrap", headers={"Authorization": f"Bearer {fresh}"}).status_code == 401


def test_logout_closes_the_websocket():
    import time
    with TestClient(app) as client:
        user = _signup(client, f"sock{uuid4().hex[:8]}")
        with client.websocket_connect(f"/ws?token={user['token']}") as socket:
            assert client.post("/api/auth/logout", headers=user["headers"]).status_code == 200
            code = None
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and code is None:
                try:
                    socket.receive_text()
                except WebSocketDisconnect as exc:
                    code = exc.code
            assert code == 4401


def test_recovery_expiry_treats_naive_timestamps_as_utc():
    from app.recovery import _current

    assert _current(datetime(2099, 1, 1))
    assert not _current(datetime(2000, 1, 1))
    assert _current(datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert not _current(None)


def test_recovery_hides_missing_accounts_and_rejects_replayed_codes():
    clear_outbox()
    with TestClient(app) as client:
        missing = client.post("/api/auth/recovery", json={"username": f"nobody{uuid4().hex[:6]}"})
        user = _signup(client, f"rec{uuid4().hex[:8]}")
        unverified = client.post("/api/auth/recovery", json={"username": user["user"]["username"]})
        assert missing.status_code == unverified.status_code == 200
        assert missing.json()["message"] == unverified.json()["message"]
        assert outbox() == []
        assert client.post("/api/account/recovery-email", json={"email": "not-an-email"}, headers=user["headers"]).status_code == 422
        assert client.post("/api/account/recovery-email", json={"email": "rec@example.com"}, headers=user["headers"]).status_code == 200
        code = outbox()[-1]["text"].split("Verification code: ", 1)[1].strip()
        assert "rec@example.com" == outbox()[-1]["to"]
        with db() as session:
            from app.persistence.models import RecoveryAddress
            row = session.get(RecoveryAddress, user["user"]["id"])
            row.verify_expires_at = utcnow() - timedelta(minutes=1)
            session.add(row)
            session.commit()
        assert client.post("/api/auth/recovery/verify", json={"token": code}).status_code == 400
        assert client.post("/api/account/recovery-email", json={"email": "rec@example.com"}, headers=user["headers"]).status_code == 200
        code = outbox()[-1]["text"].split("Verification code: ", 1)[1].strip()
        assert client.post("/api/auth/recovery/verify", json={"token": code}).status_code == 200
        assert client.post("/api/auth/recovery/verify", json={"token": code}).status_code == 400
        before = len(outbox())
        sent = client.post("/api/auth/recovery", json={"username": user["user"]["username"]})
        assert sent.json()["message"] == missing.json()["message"]
        reset = outbox()[before]["text"].split("Reset code: ", 1)[1].strip()
        assert client.post("/api/auth/recovery/reset", json={"token": reset, "password": "new-secret"}).status_code == 200
        assert client.post("/api/auth/recovery/reset", json={"token": reset, "password": "new-secret"}).status_code == 400
        assert client.get("/api/bootstrap", headers=user["headers"]).status_code == 401
        assert client.post("/api/auth/login", json={"username": user["user"]["username"], "password": "secret123"}).status_code == 401
        again = client.post("/api/auth/login", json={"username": user["user"]["username"], "password": "new-secret"})
        assert again.status_code == 200
        with db() as session:
            account = session.get(UserBandAccount, user["user"]["id"]) or UserBandAccount(user_id=user["user"]["id"])
            account.secret_enc = encrypt_secret("band_u_kept")
            account.band_user_id = f"band-{user['user']['id']}"
            session.add(account)
            session.commit()
        relogin = client.post("/api/auth/login", json={"username": user["user"]["username"], "password": "new-secret"})
        with db() as session:
            stored = session.get(UserBandAccount, user["user"]["id"])
            from app.auth import decrypt_secret
            assert decrypt_secret(stored.secret_enc) == "band_u_kept"
        assert relogin.status_code == 200


def _pair(client: TestClient):
    left = _signup(client, f"a{uuid4().hex[:8]}")
    right = _signup(client, f"b{uuid4().hex[:8]}")
    outsider = _signup(client, f"c{uuid4().hex[:8]}")
    return left, right, outsider


def test_people_events_stay_with_the_participants_and_final_states_stick(monkeypatch):
    from app.workspace.events import bus
    recorded = []
    original = bus.publish

    async def spy(event, *, users=None):
        recorded.append((event.get("type"), None if users is None else set(users), set(event) - {"type", "cursor"}))
        await original(event, users=users)

    monkeypatch.setattr(bus, "publish", spy)
    with TestClient(app) as client:
        alice, bob, cara = _pair(client)
        with client.websocket_connect(f"/ws?token={bob['token']}") as bob_ws:
            requested = client.post("/api/people/requests", json={"handle": bob["user"]["username"]}, headers=alice["headers"])
            assert requested.status_code == 200, requested.text
            event = bob_ws.receive_json()
            while event["type"] != "people_changed":
                event = bob_ws.receive_json()
            assert "channel_name" not in event and "message" not in event
            delivered = [item for item in recorded if item[0] == "people_changed"]
            assert delivered
            assert delivered[-1][1] == {alice["user"]["id"], bob["user"]["id"]}
            assert cara["user"]["id"] not in delivered[-1][1]
            assert delivered[-1][2] == set()
        cancelled = client.post(f"/api/people/requests/{requested.json()['id']}/cancel", headers=alice["headers"])
        assert cancelled.status_code == 200
        decided = client.post(f"/api/people/requests/{requested.json()['id']}/decision", json={"accept": True}, headers=bob["headers"])
        assert decided.status_code == 200
        with db() as session:
            row = session.get(AppContactRequest, requested.json()["id"])
            assert row.status == "cancelled"
            assert session.get(AppContact, tuple(sorted([alice["user"]["id"], bob["user"]["id"]]))) is None
        again = client.post("/api/people/requests", json={"handle": bob["user"]["username"]}, headers=alice["headers"])
        assert client.post(f"/api/people/requests/{again.json()['id']}/decision", json={"accept": True}, headers=bob["headers"]).status_code == 200
        channel = client.post("/api/channels", json={"name": f"room-{uuid4().hex[:6]}"}, headers=alice["headers"]).json()
        invited = client.post(f"/api/channels/{channel['id']}/invitations", json={"user_id": bob["user"]["id"]}, headers=alice["headers"])
        assert invited.status_code == 200
        invitation_id = client.get("/api/people", headers=alice["headers"]).json()["sent_invitations"][0]["id"]
        assert client.post(f"/api/people/invitations/{invitation_id}/withdraw", headers=alice["headers"]).status_code == 200
        assert client.post(f"/api/people/invitations/{invitation_id}/decision", json={"accept": True}, headers=bob["headers"]).status_code == 200
        with db() as session:
            assert session.get(ChannelInvitation, invitation_id).status == "withdrawn"
        assert client.get(f"/api/channels/{channel['id']}/messages", headers=bob["headers"]).status_code == 404
        assert client.post(f"/api/channels/{channel['id']}/invitations", json={"user_id": bob["user"]["id"]}, headers=alice["headers"]).status_code == 200
        invitation_id = client.get("/api/people/invitations", headers=bob["headers"]).json()[0]["id"]
        assert client.post(f"/api/people/invitations/{invitation_id}/decision", json={"accept": True}, headers=bob["headers"]).status_code == 200
        dm = client.post(f"/api/people/{bob['user']['id']}/dm", headers=alice["headers"])
        assert dm.status_code == 200, dm.text
        sent = client.post(f"/api/channels/{dm.json()['id']}/messages", json={"content": "hello there"}, headers=alice["headers"])
        assert sent.status_code == 200, sent.text
        shared = client.post(f"/api/channels/{channel['id']}/messages", json={"content": "still here"}, headers=bob["headers"])
        assert shared.status_code == 200, shared.text
        assert client.post(f"/api/people/{bob['user']['id']}/block", headers=alice["headers"]).status_code == 200
        blocked = client.post(f"/api/channels/{dm.json()['id']}/messages", json={"content": "again"}, headers=bob["headers"])
        assert blocked.status_code == 403
        history = client.get(f"/api/channels/{dm.json()['id']}/messages", headers=bob["headers"])
        assert any(item["content"] == "hello there" for item in history.json())
        assert client.post(f"/api/channels/{channel['id']}/messages", json={"content": "shared stays"}, headers=bob["headers"]).status_code == 200
        assert client.post("/api/people/requests", json={"handle": alice["user"]["username"]}, headers=bob["headers"]).status_code == 403
        assert client.delete(f"/api/people/{bob['user']['id']}", headers=alice["headers"]).status_code == 200
        assert client.get(f"/api/channels/{channel['id']}/messages", headers=bob["headers"]).json()
        assert client.post(f"/api/people/{bob['user']['id']}/dm", headers=alice["headers"]).status_code == 403


def test_worker_heartbeat_and_provider_status_hide_secrets():
    mark_api_started()
    beat("idle")
    assert worker_state(stale_seconds=20, grace_seconds=0) == "ready"
    beat("busy")
    assert worker_state(stale_seconds=20, grace_seconds=0) == "busy"
    from app.persistence.models import WorkerHeartbeat
    with db() as session:
        row = session.get(WorkerHeartbeat, 1)
        row.beat_at = utcnow() - timedelta(seconds=100)
        session.add(row)
        session.commit()
    import app.workspace.health as health
    health._api_started_at = utcnow()
    assert worker_state(stale_seconds=20, grace_seconds=30) == "starting"
    health._api_started_at = utcnow() - timedelta(seconds=100)
    assert worker_state(stale_seconds=20, grace_seconds=15) == "unavailable"
    with TestClient(app) as client:
        owner = _signup(client, f"own{uuid4().hex[:8]}")
        other = _signup(client, f"oth{uuid4().hex[:8]}")
        public = client.get("/api/health").json()
        encoded = json.dumps(public)
        assert public == {"ok": True, "band": {"configured": public["band"]["configured"], "online": public["band"]["online"]}}
        assert "API_KEY" not in encoded and "sk-" not in encoded
        connection_id = uuid4().hex
        secret = "sk-test-secret-value"
        with db() as session:
            profile = AgentProfile(name=f"Helper {uuid4().hex[:4]}", handle=uuid4().hex[:8], role="custom",
                                    model="gpt-4o-mini", band_agent_id=uuid4().hex, band_api_key_enc=encrypt_secret("e2e"),
                                    created_by=owner["user"]["id"])
            session.add(profile)
            session.flush()
            session.add(AgentConnection(id=connection_id, provider="cursor", label="Cursor", secret_enc=encrypt_secret(secret),
                                        created_by=owner["user"]["id"]))
            session.add(AgentRuntime(agent_id=profile.id, provider="cursor", connection_id=connection_id))
            session.commit()
            agent_id = profile.id
        remember_provider_state(connection_id, "sign_in_required")
        owned = client.get("/api/status", headers=owner["headers"]).json()
        stranger = client.get("/api/status", headers=other["headers"]).json()
        mine = next(item for item in owned["providers"] if item["agent_id"] == agent_id)
        theirs = next(item for item in stranger["providers"] if item["agent_id"] == agent_id)
        assert mine["action"] == "reconnect"
        assert "action" not in theirs
        assert theirs["state"] == "sign_in_required"
        blob = json.dumps({"owned": owned, "stranger": stranger, "public": public})
        assert secret not in blob
        assert "Bearer" not in blob
        assert owned["worker"] in {"ready", "busy", "starting", "unavailable"}
        assert owned["human_chat"] == "ok"
        viewer = next(user for user in (owner, other))
        direct = service_status(type("User", (), {"id": viewer["user"]["id"]})())
        assert secret not in json.dumps(direct)


def test_configured_database_keeps_one_human_message():
    name = f"db{uuid4().hex[:8]}"
    client_id = uuid4().hex
    with TestClient(app) as client:
        user = _signup(client, name)
        channel = client.post("/api/channels", json={"name": f"notes-{name}"}, headers=user["headers"])
        assert channel.status_code == 200, channel.text
        body = {"content": "kept once", "client_id": client_id}
        first = client.post(f"/api/channels/{channel.json()['id']}/messages", json=body, headers=user["headers"])
        second = client.post(f"/api/channels/{channel.json()['id']}/messages", json=body, headers=user["headers"])
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        with db() as session:
            rows = session.exec(select(Message).where(Message.channel_id == channel.json()["id"], Message.content == "kept once")).all()
            tasks = session.exec(select(AgentTask).where(AgentTask.channel_id == channel.json()["id"])).all()
        assert len(rows) == 1
        assert tasks == []
