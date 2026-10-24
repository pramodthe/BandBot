"""Reconnect snapshots and idempotent human sends."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlmodel import select

from app.auth import encrypt_secret
from app.main import app
from app.persistence.models import AgentProfile, AgentTask, ChannelMember, Message, User, db
from app.workspace.service import service


def _signup(client: TestClient, username: str, name: str) -> dict:
    response = client.post("/api/auth/signup", json={
        "username": username, "display_name": name, "password": "secret123", "band_api_key": f"band_a_signup-{username}",
    })
    assert response.status_code == 200, response.text
    return response.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_retry_returns_the_original_message_and_one_task():
    with TestClient(app) as client:
        alice = _signup(client, "retry-alice", "Retry Alice")
        auth = _auth(alice["token"])
        channel = client.post("/api/channels", json={"name": "retry-room"}, headers=auth).json()
        with db() as session:
            agent = AgentProfile(name="QuillRetry", handle="quill-retry", band_agent_id="agent-quill-retry",
                                 band_api_key_enc=encrypt_secret("k"))
            session.add(agent)
            session.commit()
            session.refresh(agent)
            session.add(ChannelMember(channel_id=channel["id"], member_type="agent", member_id=agent.id))
            session.commit()
        body = {"content": "@QuillRetry please look", "client_id": "retry-key-01"}

        first = client.post(f"/api/channels/{channel['id']}/messages", json=body, headers=auth)
        assert first.status_code == 200, first.text
        again = client.post(f"/api/channels/{channel['id']}/messages", json=body, headers=auth)
        assert again.status_code == 200, again.text
        assert again.json()["id"] == first.json()["id"]
        assert again.json()["client_id"] == "retry-key-01"
        assert again.json()["content"] == "@QuillRetry please look"

        conflict = client.post(f"/api/channels/{channel['id']}/messages",
                               json={"content": "@QuillRetry a different request", "client_id": "retry-key-01"}, headers=auth)
        assert conflict.status_code == 409
        assert client.post(f"/api/channels/{channel['id']}/messages",
                           json={"content": "no key"}, headers=auth).status_code == 200
        assert client.post(f"/api/channels/{channel['id']}/messages",
                           json={"content": "short", "client_id": "abc"}, headers=auth).status_code == 422

        with db() as session:
            humans = session.exec(select(Message).where(Message.channel_id == channel["id"],
                                                        Message.author_id == alice["user"]["id"]).order_by(Message.id)).all()
            agent = session.exec(select(AgentProfile).where(AgentProfile.band_agent_id == "agent-quill-retry")).one()
            tasks = session.exec(select(AgentTask).where(AgentTask.agent_id == agent.id)).all()
        assert [m.content for m in humans] == ["@QuillRetry please look", "no key"]
        assert len(tasks) == 1
        assert tasks[0].source_key.endswith(f"app-message-{first.json()['id']}")


def test_same_client_id_cannot_read_someone_elses_message_or_skip_membership():
    with TestClient(app) as client:
        alice = _signup(client, "scope-alice", "Scope Alice")
        bob = _signup(client, "scope-bob", "Scope Bob")
        auth_a, auth_b = _auth(alice["token"]), _auth(bob["token"])
        client.post("/api/people/requests", json={"handle": "scope-bob"}, headers=auth_a)
        request_id = client.get("/api/people", headers=auth_b).json()["requests"][0]["id"]
        assert client.post(f"/api/people/requests/{request_id}/decision", json={"accept": True}, headers=auth_b).status_code == 200
        channel = client.post("/api/channels", json={"name": "Launch"}, headers=auth_a).json()
        assert client.post(f"/api/channels/{channel['id']}/invitations", json={"user_id": bob["user"]["id"]}, headers=auth_a).status_code == 200
        before = client.get("/api/bootstrap", headers=auth_b).json()
        assert channel["id"] not in {item["id"] for item in before["channels"]}
        assert before["invitations"][0]["channel_name"] == "launch"
        assert before["cursor"] >= 0

        invitation = before["invitations"][0]["id"]
        assert client.post(f"/api/people/invitations/{invitation}/decision", json={"accept": True}, headers=auth_b).status_code == 200
        joined = client.get("/api/bootstrap", headers=auth_b).json()
        assert channel["id"] in {item["id"] for item in joined["channels"]}
        assert joined["invitations"] == []
        visible = next(item for item in joined["channels"] if item["id"] == channel["id"])
        assert bob["user"]["id"] in visible["human_ids"]

        posted = client.post(f"/api/channels/{channel['id']}/messages",
                             json={"content": "alice-only-phrase", "client_id": "shared-id-01"}, headers=auth_a)
        assert posted.status_code == 200, posted.text
        other = client.post(f"/api/channels/{channel['id']}/messages",
                            json={"content": "bob says hello", "client_id": "shared-id-01"}, headers=auth_b)
        assert other.status_code == 200, other.text
        assert other.json()["id"] != posted.json()["id"]
        assert other.json()["content"] == "bob says hello"
        assert "alice-only-phrase" not in other.text

        private = client.post(f"/api/channels/{channel['id']}/messages",
                              json={"content": "private-phrase", "client_id": "private-id-1"}, headers=auth_b)
        assert private.status_code == 200, private.text
        first_id = private.json()["id"]
        caught_up = client.get(f"/api/channels/{channel['id']}/messages?after={first_id - 1}", headers=auth_b).json()
        assert [item["id"] for item in caught_up] == [first_id]
        assert client.get(f"/api/channels/{channel['id']}/messages?after={first_id}&before={first_id + 10}",
                          headers=auth_b).status_code == 422

        assert client.post(f"/api/channels/{channel['id']}/leave", headers=auth_b).status_code == 200
        left = client.get("/api/bootstrap", headers=auth_b).json()
        assert channel["id"] not in {item["id"] for item in left["channels"]}
        leaked = client.post(f"/api/channels/{channel['id']}/messages",
                             json={"content": "private-phrase", "client_id": "private-id-1"}, headers=auth_b)
        assert leaked.status_code == 404
        assert "private-phrase" not in leaked.text
        assert client.get(f"/api/channels/{channel['id']}/messages?after={first_id - 1}", headers=auth_b).status_code == 404
        with db() as session:
            assert session.get(Message, first_id).content == "private-phrase"
            assert len(session.exec(select(Message).where(Message.channel_id == channel["id"],
                                                          Message.author_id == bob["user"]["id"])).all()) == 2


def test_message_events_carry_a_cursor_the_snapshot_has_passed():
    with TestClient(app) as client:
        alice = _signup(client, "cursor-alice", "Cursor Alice")
        auth = _auth(alice["token"])
        channel = client.post("/api/channels", json={"name": "cursor-room"}, headers=auth).json()
        with client.websocket_connect(f"/ws?token={alice['token']}") as ws:
            posted = client.post(f"/api/channels/{channel['id']}/messages",
                                 json={"content": "hello again", "client_id": "cursor-id-01"}, headers=auth)
            assert posted.status_code == 200, posted.text
            event = ws.receive_json()
            while event["type"] != "message":
                event = ws.receive_json()
            assert event["message"]["id"] == posted.json()["id"]
            assert isinstance(event["cursor"], int) and event["cursor"] > 0
        snapshot = client.get("/api/bootstrap", headers=auth).json()
        assert snapshot["cursor"] >= event["cursor"]


def test_concurrent_retries_create_one_message():
    with TestClient(app) as client:
        alice = _signup(client, "race-alice", "Race Alice")
        channel = client.post("/api/channels", json={"name": "race-room"}, headers=_auth(alice["token"])).json()
        with db() as session:
            user = session.get(User, alice["user"]["id"])

        async def burst():
            return await asyncio.gather(
                service.post_human_message(user, channel["id"], "once only", None, "concurrent-id"),
                service.post_human_message(user, channel["id"], "once only", None, "concurrent-id"),
            )

        first, second = asyncio.run(burst())
        assert first["id"] == second["id"]
        assert first["content"] == "once only"
        with db() as session:
            rows = session.exec(select(Message).where(Message.channel_id == channel["id"],
                                                      Message.author_id == alice["user"]["id"]).order_by(Message.id)).all()
        assert [row.content for row in rows] == ["once only"]
