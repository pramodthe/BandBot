from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.auth import encrypt_secret, hash_password, issue_token
from app.workspace.events import EventBus
from app.main import app
from sqlmodel import select

from app.persistence.models import AgentProfile, AgentTask, Channel, ChannelMember, DirectMessage, User, db


def _signup(client: TestClient, username: str, name: str) -> dict:
    response = client.post(
        "/api/auth/signup", json={"username": username, "display_name": name, "password": "secret123",
                                  "band_api_key": f"band_u_{username}"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_humans_chat_while_band_is_not_configured():
    with TestClient(app) as client:
        alice = _signup(client, "alice", "Alice")
        bob = _signup(client, "bob", "Bob")
        auth_a = {"Authorization": f"Bearer {alice['token']}"}
        auth_b = {"Authorization": f"Bearer {bob['token']}"}

        boot = client.get("/api/bootstrap", headers=auth_a).json()
        assert boot["band"]["configured"] is True and boot["band"]["online"] is True
        assert boot["band"]["error"] is None
        assert "band_u_alice" not in client.get("/api/bootstrap", headers=auth_a).text
        with db() as session:
            general = session.exec(select(Channel).where(Channel.name == "general")).one()
            general_id = general.id
        assert all(c["name"] != "general" or alice["user"]["id"] in c["human_ids"] for c in boot["channels"])
        bob_boot = client.get("/api/bootstrap", headers=auth_b).json()
        assert all(c["name"] != "general" for c in bob_boot["channels"])
        assert client.get(f"/api/channels/{general_id}/messages", headers=auth_b).status_code == 404
        assert all(agent.get("owner_id") in (None, bob["user"]["id"]) for agent in bob_boot["agents"])

        response = client.post("/api/channels", json={"name": "Launch Plan"}, headers=auth_a)
        assert response.status_code == 200
        created = response.json()
        assert created["private"] and created["band_room_id"]

        assert client.post("/api/people/requests", json={"handle": "bob"}, headers=auth_a).status_code == 200
        request_id = client.get("/api/people", headers=auth_b).json()["requests"][0]["id"]
        assert client.post(f"/api/people/requests/{request_id}/decision", json={"accept": True}, headers=auth_b).status_code == 200
        dm = client.post(f"/api/people/{bob['user']['id']}/dm", headers=auth_a).json()
        with client.websocket_connect(f"/ws?token={bob['token']}") as ws:
            posted = client.post(
                f"/api/channels/{dm['id']}/messages",
                json={"content": "hey @Bob, ready?"},
                headers=auth_a,
            ).json()
            assert posted["mentions"] == [{"type": "human", "id": bob["user"]["id"], "name": "Bob"}]
            event = ws.receive_json()
            while event["type"] != "message":
                event = ws.receive_json()
            assert event["message"]["content"] == "hey @Bob, ready?"

        messages = client.get(f"/api/channels/{dm['id']}/messages", headers=auth_b).json()
        assert [m["author_name"] for m in messages] == ["Alice"]

        response = client.post("/api/agents", json={"name": "Writer", "role": "writer"}, headers=auth_a)
        assert response.status_code == 200, response.text
        assert response.json()["owner_id"] == alice["user"]["id"]
        assert "band_u_" not in response.text
        response = client.post("/api/agents", json={"name": "W", "role": "writer"}, headers=auth_a)
        assert response.status_code == 422

        assert client.get("/api/bootstrap").status_code == 401
        time.sleep(0.1)


def test_direct_messages_are_visible_only_to_their_owner():
    with TestClient(app) as client:
        dana = _signup(client, "dana", "Dana")
        eli = _signup(client, "eli", "Eli")
        auth_d = {"Authorization": f"Bearer {dana['token']}"}
        auth_e = {"Authorization": f"Bearer {eli['token']}"}
        with db() as session:
            agent = AgentProfile(name="Quill", handle="pat/quill", band_agent_id="agent-quill",
                                 band_api_key_enc=encrypt_secret("k"))
            channel = Channel(name="dm-test-dana", topic="Direct messages with Quill")
            session.add_all([agent, channel])
            session.flush()
            session.add_all([
                DirectMessage(channel_id=channel.id, user_id=dana["user"]["id"], agent_id=agent.id),
                ChannelMember(channel_id=channel.id, member_type="human", member_id=dana["user"]["id"]),
                ChannelMember(channel_id=channel.id, member_type="agent", member_id=agent.id),
                AgentTask(id="dm-task", source_key="dm-task", channel_id=channel.id, agent_id=agent.id,
                          room_id="", prompt="hi"),
            ])
            session.commit()
            channel_id, agent_id = channel.id, agent.id

        assert client.post(f"/api/agents/{agent_id}/dm", headers=auth_d).json()["id"] == channel_id
        assert client.post(f"/api/agents/{agent_id}/dm", headers=auth_e).status_code == 503

        mine = next(c for c in client.get("/api/bootstrap", headers=auth_d).json()["channels"] if c["id"] == channel_id)
        assert mine["is_dm"] and mine["dm_agent_id"] == agent_id
        assert client.get(f"/api/channels/{channel_id}/messages", headers=auth_d).status_code == 200
        assert client.post(f"/api/channels/{channel_id}/leave", headers=auth_d).status_code == 409
        assert client.delete(f"/api/channels/{channel_id}/agents/{agent_id}", headers=auth_d).status_code == 409
        assert client.get("/api/tasks/dm-task", headers=auth_d).status_code == 200

        assert channel_id not in {c["id"] for c in client.get("/api/bootstrap", headers=auth_e).json()["channels"]}
        assert client.get(f"/api/channels/{channel_id}/messages", headers=auth_e).status_code == 404
        assert client.post(f"/api/channels/{channel_id}/messages", json={"content": "hi"},
                           headers=auth_e).status_code == 404
        assert client.post(f"/api/channels/{channel_id}/join", headers=auth_e).status_code == 404
        assert client.get("/api/tasks/dm-task", headers=auth_e).status_code == 404


class _Socket:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_json(self, event: dict) -> None:
        self.events.append(event)


def test_login_uses_the_persons_band_key_not_the_server_key(monkeypatch):
    monkeypatch.setattr("app.config.uses_workspace_band_key", lambda: True)
    monkeypatch.setattr("app.api.http.uses_workspace_band_key", lambda: True)
    with TestClient(app) as client:
        with db() as session:
            user = User(username="legacy", display_name="Legacy", password_hash=hash_password("secret123"))
            session.add(user)
            session.commit()
            session.refresh(user)
            general = session.exec(select(Channel).where(Channel.name == "general")).one()
            session.add(ChannelMember(channel_id=general.id, member_type="human", member_id=user.id))
            session.commit()
            user_id, general_id = user.id, general.id
        headers = {"Authorization": f"Bearer {issue_token(user_id)}"}
        refused = client.post("/api/auth/login", json={"username": "legacy", "password": "secret123"})
        assert refused.status_code == 409 and "band_u_" not in refused.text
        created = client.post("/api/agents", json={"name": "Writer", "role": "writer"}, headers=headers)
        assert created.status_code == 409 and "band_u_" not in created.text
        peers = client.get(f"/api/channels/{general_id}/peers", headers=headers)
        assert peers.status_code == 409 and "band_u_" not in peers.text
        signed = client.post("/api/auth/login", json={
            "username": "legacy", "password": "secret123", "band_api_key": "band_u_legacy",
        })
        assert signed.status_code == 200, signed.text
        auth = {"Authorization": f"Bearer {signed.json()['token']}"}
        boot = client.get("/api/bootstrap", headers=auth)
        assert boot.status_code == 200 and boot.json()["band"]["configured"] is True
        assert "band_u_legacy" not in boot.text


def test_private_channel_events_reach_only_their_owner():
    bus = EventBus()
    bus.audience = lambda channel_id: 1 if channel_id == 7 else None
    owner, other = _Socket(), _Socket()

    async def scenario():
        await bus.connect(1, owner)
        await bus.connect(2, other)
        owner.events.clear()
        other.events.clear()
        await bus.publish({"type": "message", "message": {"channel_id": 7}})
        await bus.publish({"type": "typing", "channel_id": 7})
        await bus.publish({"type": "task_activity", "task": {"channel_id": 7}})
        await bus.publish({"type": "channel", "channel": {"id": 7}})
        await bus.publish({"type": "message", "message": {"channel_id": 3}})
    asyncio.run(scenario())

    assert len(owner.events) == 5
    assert [event["type"] for event in other.events] == ["message"]
    assert other.events[0]["message"]["channel_id"] == 3
    assert isinstance(other.events[0]["cursor"], int)
