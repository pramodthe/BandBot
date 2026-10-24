from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.auth import encrypt_secret, hash_password
from app.workspace.events import EventBus
from app.main import app
from sqlmodel import select

from app.persistence.models import (AgentProfile, AgentTask, BandAgentOwner, Channel, ChannelMember,
                                    DirectMessage, User, db)


def _signup(client: TestClient, username: str, name: str) -> dict:
    response = client.post(
        "/api/auth/signup", json={"username": username, "display_name": name, "password": "secret123", "band_api_key": f"band_a_signup-{username}"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_humans_chat_with_their_saved_agent_api_connection_without_human_api():
    with TestClient(app) as client:
        alice = _signup(client, "alice", "Alice")
        bob = _signup(client, "bob", "Bob")
        auth_a = {"Authorization": f"Bearer {alice['token']}"}
        auth_b = {"Authorization": f"Bearer {bob['token']}"}

        boot = client.get("/api/bootstrap", headers=auth_a).json()
        assert boot["band"]["configured"] and boot["band"]["online"]
        assert boot["band"]["api"] == "agent"
        channel = next(c for c in boot["channels"] if c["name"] == "general")
        assert {alice["user"]["id"], bob["user"]["id"]} <= set(channel["human_ids"])

        response = client.post("/api/channels", json={"name": "Launch Plan"}, headers=auth_a)
        assert response.status_code == 200
        assert response.json()["private"] and response.json()["band_room_id"]

        with client.websocket_connect(f"/ws?token={bob['token']}") as ws:
            posted = client.post(
                f"/api/channels/{channel['id']}/messages",
                json={"content": "hey @Bob, ready?"},
                headers=auth_a,
            ).json()
            assert posted["mentions"] == [{"type": "human", "id": bob["user"]["id"], "name": "Bob"}]
            event = ws.receive_json()
            while event["type"] != "message":
                event = ws.receive_json()
            assert event["message"]["content"] == "hey @Bob, ready?"

        messages = client.get(f"/api/channels/{channel['id']}/messages", headers=auth_b).json()
        assert [m["author_name"] for m in messages] == ["Alice"]

        response = client.post("/api/agents", json={"name": "Writer", "role": "writer"}, headers=auth_a)
        assert response.status_code == 200 and response.json()["band_auth"] == "platform"
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


def test_login_does_not_ask_for_a_band_key_or_list_server_agents(monkeypatch):
    monkeypatch.setattr("app.config.uses_workspace_band_key", lambda: True)
    with TestClient(app) as client:
        with db() as session:
            user = User(username="legacy", display_name="Legacy", password_hash=hash_password("secret123"))
            session.add(user)
            session.commit()
            session.refresh(user)
            general = session.exec(select(Channel).where(Channel.name == "general")).one()
            session.add(ChannelMember(channel_id=general.id, member_type="human", member_id=user.id))
            session.commit()
            general_id = general.id
        signed = client.post("/api/auth/login", json={"username": "legacy", "password": "secret123"})
        assert signed.status_code == 200, signed.text
        assert "band_u_" not in signed.text
        auth = {"Authorization": f"Bearer {signed.json()['token']}"}
        boot = client.get("/api/bootstrap", headers=auth)
        assert boot.status_code == 200 and "band_u_" not in boot.text
        peers = client.get(f"/api/channels/{general_id}/peers", headers=auth)
        assert peers.status_code == 403 and "band_u_" not in peers.text
        invited = client.post(f"/api/channels/{general_id}/outside-agents", headers=auth,
                              json={"band_id": "00000000-0000-0000-0000-000000000000"})
        assert invited.status_code == 403 and "invite" in invited.json()["detail"]
        assert "band_u_" not in invited.text
        created = client.post("/api/agents", json={"name": "Writer", "role": "writer"}, headers=auth)
        assert created.status_code == 403 and "admin" in created.json()["detail"]
        assert "band_u_" not in created.text and "own Band" not in created.text
        with db() as session:
            from app.persistence.models import WorkspaceRole
            session.add(WorkspaceRole(user_id=user.id, role="admin"))
            session.commit()
        allowed = client.post("/api/agents", json={"name": "Writer", "role": "writer"}, headers=auth)
        assert allowed.status_code == 503 and "band_u_" not in allowed.text


def test_agent_model_update_rejects_a_missing_provider_revision():
    with TestClient(app) as client:
        signed = _signup(client, "modeler", "Modeler")
        headers = {"Authorization": f"Bearer {signed['token']}"}
        user_id = signed["user"]["id"]
        with db() as session:
            profile = AgentProfile(name="Quill", role="writer", band_agent_id="agent-quill-missing-revision",
                                   band_api_key_enc=encrypt_secret("k"), model="old-model", created_by=user_id)
            session.add(profile)
            session.commit()
            session.refresh(profile)
            session.add(BandAgentOwner(agent_id=profile.id, user_id=user_id, band_user_id="local"))
            from app.providers.models import ServiceSelection
            session.add(ServiceSelection(scope=f"agent:{profile.id}", bindings={
                "llm": {"connection_id": "missing-connection", "revision": 1, "provider": "openai", "model": "old-model"},
            }))
            session.commit()
            agent_id = profile.id
        updated = client.patch(f"/api/agents/{agent_id}", json={"model": "new-model"}, headers=headers)
        assert updated.status_code == 422, updated.text
        assert "allowed provider model" in updated.json()["detail"]
        with db() as session:
            assert session.get(AgentProfile, agent_id).model == "old-model"


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
