from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import select

import app.runtime.harness as harness
from app.auth import issue_token, encrypt_secret
from app.main import app
from app.persistence.models import AgentPersona, AgentProfile, AgentTask, Channel, ChannelMember, User, UserBandConnection, WorkspaceRole, db, init_db
from app.runtime.tools.composio import plugin_slugs
from app.workspace.service import service
from helpers import FakeOrg
from app.band.platform import BandIdentity


def test_create_edit_and_delete_agent_identity_without_preset_or_implicit_membership(monkeypatch):
    init_db()
    key = uuid4().hex[:8]
    with db() as session:
        user = User(username=f"persona-{key}", display_name="Creator", password_hash="unused")
        channel = Channel(name=f"persona-{key}", band_room_id=f"persona-{key}")
        session.add_all([user, channel])
        session.commit()
        session.merge(WorkspaceRole(user_id=user.id, role="admin"))
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.commit()
    connection = UserBandConnection(user_id=user.id, band_agent_id=f"persona-{key}", secret_enc=encrypt_secret(f"band_a_persona-{key}"))
    with db() as session:
        session.add(connection)
        session.commit()
    org = FakeOrg(user_id=user.id, identity=connection.band_agent_id)
    monkeypatch.setattr(service, "_platform_orgs", {user.id: (connection.secret_enc, org)})

    async def identity(api_key):
        name = api_key.removeprefix("key-")
        return BandIdentity(id=f"agent-{name}", name=name, handle=f"pat/{name}")

    async def start(profile):
        service.agents.status[profile.id] = {"state": "online", "error": None}

    import app.workspace.service as service_module
    monkeypatch.setattr(service_module, "fetch_identity", identity)
    monkeypatch.setattr(service, "start_agent", start)
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    # No lifespan: this exercises real routes and persistence without connecting Band or models.
    client = TestClient(app)
    response = client.post("/api/agents", headers=auth, json={
        "name": f"Atlas-{key}", "specialty": "  Research and synthesis  ", "avatar": "sage", "plugins": [],
    })
    assert response.status_code == 200, response.text
    agent = response.json()
    agent_id = agent["id"]
    assert agent["band_auth"] == "platform"
    assert agent["role"] == "custom"
    assert agent["created_at"].endswith("+00:00")
    assert agent["specialty"] == agent["role_label"] == "Research and synthesis"
    assert agent["avatar"] == "sage" and plugin_slugs(agent_id) == []
    with db() as session:
        assert session.exec(select(ChannelMember).where(
            ChannelMember.member_type == "agent", ChannelMember.member_id == agent_id)).first() is None
    boot = client.get("/api/bootstrap", headers=auth).json()
    assert next(a for a in boot["agents"] if a["id"] == agent_id)["avatar"] == "sage"
    edited = client.patch(f"/api/agents/{agent_id}", headers=auth,
                          json={"specialty": "Product research", "avatar": "blue"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["role_label"] == "Product research"
    assert edited.json()["avatar"] == "blue"
    # Editing other settings keeps identity; explicit clearing restores the legacy role label/avatar.
    assert client.patch(f"/api/agents/{agent_id}", headers=auth, json={"instructions": "Be concise"}).json()["avatar"] == "blue"
    assert client.patch(f"/api/agents/{agent_id}", headers=auth, json={"avatar": "invalid"}).status_code == 422
    assert client.post("/api/agents", headers=auth, json={"name": "Invalid", "specialty": "x" * 161}).status_code == 422
    assert client.post("/api/agents", json={"name": "No auth"}).status_code == 401
    cleared = client.patch(f"/api/agents/{agent_id}", headers=auth, json={"specialty": "", "avatar": None}).json()
    assert cleared["specialty"] == "" and cleared["role_label"] == "Custom" and cleared["avatar"] is None
    opted_in = client.post("/api/agents", headers=auth, json={
        "name": f"Opted-{key}", "specialty": "Writing", "channel_id": channel.id, "avatar": "plum", "plugins": [],
    })
    assert opted_in.status_code == 200, opted_in.text
    with db() as session:
        assert session.get(ChannelMember, (channel.id, "agent", opted_in.json()["id"]))
    assert client.delete(f"/api/agents/{agent_id}", headers=auth).status_code == 200
    with db() as session:
        assert session.get(AgentPersona, agent_id) is None
        assert session.get(AgentProfile, agent_id) is None
    assert client.delete(f"/api/agents/{opted_in.json()['id']}", headers=auth).status_code == 200
    client.close()
    with db() as session:
        session.delete(session.get(UserBandConnection, user.id))
        session.delete(session.get(ChannelMember, (channel.id, "human", user.id)))
        session.delete(session.get(WorkspaceRole, user.id))
        session.delete(session.get(Channel, channel.id))
        session.delete(session.get(User, user.id))
        session.commit()


def test_specialty_guides_the_deep_agent_and_teammate_roster(monkeypatch):
    init_db()
    key = uuid4().hex
    with db() as session:
        agent = AgentProfile(name="Atlas", band_agent_id=f"atlas-{key}", band_api_key_enc="unused")
        peer = AgentProfile(name="Nova", band_agent_id=f"nova-{key}", band_api_key_enc="unused")
        session.add_all([agent, peer])
        session.flush()
        session.add_all([AgentPersona(agent_id=agent.id, specialty="Research and synthesis", avatar="teal"),
                         AgentPersona(agent_id=peer.id, specialty="Product strategy")])
        session.commit()
    monkeypatch.setattr(harness, "create_deep_agent", lambda **kwargs: kwargs)
    task = AgentTask(id=key, source_key=key, channel_id=0, agent_id=agent.id, room_id=key, prompt="Research")
    result = harness.build_harness(task, agent, None, model=object())
    assert "Your specialty: Research and synthesis." in result["system_prompt"]
    assert "Nova (Product strategy)" in result["system_prompt"]
    with db() as session:
        for profile in (agent, peer):
            session.delete(session.get(AgentPersona, profile.id))
            session.delete(session.get(AgentProfile, profile.id))
        session.commit()
