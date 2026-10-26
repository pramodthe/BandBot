"""Offline tests of private app publication, viewer permissions and real gateway routing."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from sqlmodel import select

import app.api.app_previews as api
import app.computer.app_proxy as proxy
from app.auth import encrypt_secret, issue_token
from app.computer import TaskComputer, sandbox_record, save_sandbox, take_control
from app.computer.app_previews import has_active_view, publish, validate_path, validate_port
from app.main import app
from app.persistence.models import (AgentTask, AppPreviewLease, ChannelMember, ChannelPolicy,
                                    ComputerConnection, DirectMessage, MessageComponent, User, db, utcnow)
from app.tasks.worker import complete
from app.tasks import update_task



@pytest.fixture
def preview_job(desktop_job, monkeypatch):
    task, agent, user, computer, client, sandbox = desktop_job
    async def connection(*, seconds):
        assert seconds == 900
        with db() as session:
            session.merge(ComputerConnection(sandbox_id=sandbox.id, host=f"6080-{sandbox.id}.e2b.app",
                                            access_token_enc=encrypt_secret("private-ingress-token")))
            session.commit()
    sandbox.prepare_app_connection = connection
    async def setup():
        await computer.ensure()
        return await publish(computer, port=3000, title="Task app", path="/dashboard?mode=edit", call_id="publish-1")
    detail = asyncio.run(setup())
    update_task(task.id, status="running")
    complete(task, "Built the app.")
    monkeypatch.setattr(api, "computer_factory", lambda current: TaskComputer(current, client_factory=lambda: client))
    return SimpleNamespace(task=task, agent=agent, user=user, computer=computer, client=client,
                           sandbox=sandbox, detail=detail, headers={"Authorization": f"Bearer {issue_token(user.id)}"})


def connect(client, job):
    result = client.post(f"/api/app-previews/{job.detail['id']}/sessions", headers=job.headers)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    return result.json()


def test_publication_replays_and_attaches_to_completed_reply(preview_job):
    job = preview_job
    again = asyncio.run(publish(job.computer, port=3000, title="Task app", path="/", call_id="publish-1"))
    assert again["id"] == job.detail["id"]
    with db() as session:
        rows = session.exec(select(MessageComponent).where(MessageComponent.task_id == job.task.id)).all()
        saved = session.get(AgentTask, job.task.id)
        assert len(rows) == 1 and rows[0].kind == "app" and rows[0].message_id == saved.reply_message_id
    with TestClient(app) as client:
        response = client.post(f"/api/tasks/{job.task.id}/app-previews", headers=job.headers,
                               json={"title": "Second app", "port": 8000, "path": "/"})
        assert response.status_code == 200
        messages = client.get(f"/api/channels/{job.task.channel_id}/messages", headers=job.headers).json()
        reply = next(row for row in messages if row["id"] == saved.reply_message_id)
        assert [item["kind"] for item in reply["components"]] == ["app", "app"]
        assert "private-ingress-token" not in str(reply)


@pytest.mark.parametrize("port", [80, 443, 2375, 2376, 5900, 6080, 6100, 9222, 9223, 65535, True, "3000"])
def test_cannot_register_infrastructure_or_non_integer_ports(port):
    with pytest.raises(ValueError):
        validate_port(port)


@pytest.mark.parametrize("path", ["https://example.com", "//example.com/", "/\\example.com", "/\r\nHost: evil", "relative"])
def test_cannot_register_external_destinations(path):
    with pytest.raises(ValueError):
        validate_path(path)


def test_sessions_are_private_expiring_and_owner_scoped(preview_job):
    job = preview_job
    with TestClient(app) as client:
        assert client.post(f"/api/app-previews/{job.detail['id']}/sessions").status_code == 401
        first, second = connect(client, job), connect(client, job)
        token = urlsplit(first["url"]).hostname.split(".")[0]
        assert len(token) == 32 and first["url"] != second["url"]
        assert urlsplit(first["url"]).path == "/dashboard"
        with db() as session:
            lease = session.get(AppPreviewLease, first["id"])
            assert lease.token_hash == hashlib.sha256(token.encode()).hexdigest()
            assert token not in str(lease.model_dump())
        other = make_user()
        headers = {"Authorization": f"Bearer {issue_token(other.id)}"}
        assert client.post(f"/api/app-preview-sessions/{first['id']}/renew", headers=headers).status_code == 404
        assert client.delete(f"/api/app-preview-sessions/{first['id']}", headers=headers).status_code == 404
        renewed = client.post(f"/api/app-preview-sessions/{first['id']}/renew", headers=job.headers)
        assert renewed.status_code == 200
        with db() as session:
            row = session.get(AppPreviewLease, first["id"])
            row.until = utcnow() - timedelta(seconds=1)
            session.add(row)
            session.commit()
        assert client.get(first["url"]).status_code == 410
        assert client.post(f"/api/app-preview-sessions/{first['id']}/renew", headers=job.headers).status_code == 410
        assert client.get("http://invalid.localhost:8000/api/channels").status_code == 404
        assert client.get("http://" + "f" * 32 + ".localhost:8000/api/channels").status_code == 410


def make_user():
    with db() as session:
        user = User(username=uuid4().hex, display_name="Another viewer", password_hash="unused")
        session.add(user)
        session.commit()
        return user


def test_membership_policy_dm_and_takeover_revocation(preview_job):
    job = preview_job
    other = make_user()
    headers = {"Authorization": f"Bearer {issue_token(other.id)}"}
    route = f"/api/app-previews/{job.detail['id']}"
    with TestClient(app) as client:
        lease = connect(client, job)
        assert client.get(route, headers=headers).status_code == 403
        with db() as session:
            session.add(ChannelMember(channel_id=job.task.channel_id, member_type="human", member_id=other.id))
            session.add(ChannelPolicy(channel_id=job.task.channel_id, computer_control="admins"))
            session.commit()
        assert client.get(route, headers=headers).status_code == 403
        assert client.get(lease["url"]).status_code == 403
        with db() as session:
            session.delete(session.get(ChannelPolicy, job.task.channel_id))
            session.commit()
        assert client.get(route, headers=headers).status_code == 200
        take_control(job.computer.key, user_id=other.id, user_name=other.display_name)
        assert client.get(route, headers=job.headers).status_code == 409
        assert client.get(lease["url"]).status_code == 409
        from app.computer import release_control
        release_control(job.computer.key, user_name=other.display_name)
        with db() as session:
            session.add(DirectMessage(channel_id=job.task.channel_id, user_id=job.user.id, agent_id=job.agent.id))
            session.commit()
        assert client.get(route, headers=headers).status_code == 404
        with db() as session:
            session.delete(session.get(ChannelMember, (job.task.channel_id, "human", job.user.id)))
            session.commit()
        assert client.get(lease["url"]).status_code == 403


class Bytes(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"asset or API response\x00"


def test_http_assets_api_cookies_redirects_and_credentials(preview_job, monkeypatch):
    job = preview_job
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(201, stream=Bytes(), headers={"Content-Type": "application/octet-stream",
            "Set-Cookie": "app=session; Domain=localhost; Path=/; HttpOnly", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
            "Location": "http://localhost:3000/login?next=/", "E2B-Traffic-Access-Token": "must-not-leak"})
    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)))
    with TestClient(app) as client:
        lease = connect(client, job)
        origin = lease["url"].split("/dashboard")[0]
        response = client.get(origin + "/assets/main.js?v=123", headers={**job.headers, "Referer": "http://localhost:8000/private",
                              "E2B-Traffic-Access-Token": "forged", "X-Forwarded-Host": "evil"}, follow_redirects=False)
        assert response.status_code == 201 and response.content == b"asset or API response\x00"
        assert str(requests[-1].url) == f"https://3000-{job.sandbox.id}.e2b.app/assets/main.js?v=123"
        assert requests[-1].headers["e2b-traffic-access-token"] == "private-ingress-token"
        assert "authorization" not in requests[-1].headers and "referer" not in requests[-1].headers
        assert requests[-1].headers["x-forwarded-host"] == f"3000-{job.sandbox.id}.e2b.app"
        assert "domain=" not in response.headers["set-cookie"].lower()
        assert "e2b-traffic-access-token" not in response.headers and "x-frame-options" not in response.headers
        assert response.headers["location"] == origin + "/login?next=/"
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" not in csp and "frame-ancestors http://testserver" in csp
        assert "default-src 'self'" in csp and "allow-same-origin" in csp
        response = client.post(origin + "/api/items", headers={"Origin": origin, "Authorization": "Bearer app-login"}, json={"value": 4})
        assert response.status_code == 201 and requests[-1].content == b'{"value":4}'
        assert requests[-1].headers["authorization"] == "Bearer app-login"
        assert client.post(origin + "/api/items", headers={"Origin": "https://other.example"}).status_code == 403
        assert len(requests) == 2


def test_pause_replacement_disconnect_and_worker_keepalive(preview_job):
    job = preview_job
    with TestClient(app) as client:
        first, second = connect(client, job), connect(client, job)
        assert has_active_view(job.computer.key)
        asyncio.run(job.computer.close())
        job.sandbox.stop.assert_not_awaited()
        assert client.delete(f"/api/app-preview-sessions/{first['id']}", headers=job.headers).status_code == 204
        job.sandbox.stop.assert_not_awaited()
        assert client.get(first["url"]).status_code == 410
        save_sandbox(job.computer.key, status="stopped")
        assert client.get(second["url"]).status_code == 409
        assert client.post(f"/api/app-preview-sessions/{second['id']}/renew", headers=job.headers).status_code == 409
        job.sandbox.start.assert_not_awaited()  # Renewing never overrides a human Stop.
        save_sandbox(job.computer.key, status="running", sandbox_id="replacement")
        assert client.post(f"/api/app-previews/{job.detail['id']}/sessions", headers=job.headers).status_code == 409
        assert client.get(second["url"]).status_code == 409
        save_sandbox(job.computer.key, status="running", sandbox_id=job.sandbox.id)
        assert client.delete(f"/api/app-preview-sessions/{second['id']}", headers=job.headers).status_code == 204
        job.sandbox.stop.assert_awaited_once()
        assert not has_active_view(job.computer.key) and sandbox_record(job.computer.key).status == "stopped"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_unreachable_server_returns_safe_actionable_error(preview_job, monkeypatch, status):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, stream=Bytes()))
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=transport))
    with TestClient(app) as client:
        lease = connect(client, preview_job)
        response = client.get(lease["url"])
        assert response.status_code == 502
        assert "app server is not responding" in response.text
        assert "band-app-preview-error" in response.text
        assert "asset or API response" not in response.text and "private-ingress-token" not in response.text


def test_websocket_echo_subprotocol_and_revoked_lease(preview_job, monkeypatch):
    job = preview_job
    sent = []
    @asynccontextmanager
    async def fake_connect(target, **kwargs):
        sent.append((target, kwargs))
        queue = asyncio.Queue()
        class Socket:
            subprotocol = "app-dev"
            async def send(self, value): await queue.put(value)
            def __aiter__(self): return self
            async def __anext__(self): return await queue.get()
        yield Socket()
    monkeypatch.setattr(proxy, "connect", fake_connect)
    with TestClient(app) as client:
        lease = connect(client, job)
        origin = lease["url"].split("/dashboard")[0]
        with client.websocket_connect(origin.replace("http:", "ws:") + "/hmr?token=app", subprotocols=["app-dev"], headers={"origin": origin}) as socket:
            assert socket.accepted_subprotocol == "app-dev"
            socket.send_text("hot update")
            assert socket.receive_text() == "hot update"
            socket.send_bytes(b"\x00binary")
            assert socket.receive_bytes() == b"\x00binary"
            assert sent[0][0] == f"wss://3000-{job.sandbox.id}.e2b.app/hmr?token=app"
            assert sent[0][1]["additional_headers"]["e2b-traffic-access-token"] == "private-ingress-token"
            with db() as session:
                session.delete(session.get(AppPreviewLease, lease["id"]))
                session.commit()
            socket.send_text("after revocation")
            with pytest.raises(WebSocketDisconnect):
                socket.receive_text()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(origin.replace("http:", "ws:") + "/hmr"):
                pass
