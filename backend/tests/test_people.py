from __future__ import annotations

import asyncio
from types import SimpleNamespace as Obj
from uuid import uuid4
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import app.api.http as http
import app.api.people as people
import app.workspace.service as service_module
from app.auth import decrypt_secret, encrypt_secret
from app.band.listener import BandIdentity, QueuedAgentAdapter
from app.band.platform import AgentPlatformOrg, Owner
from app.persistence.models import (AgentConnection, AgentProfile, AgentTask, BandAgentOwner, ChannelMember, User,
                                    Message, UserBandAccount, UserBandConnection, BandPlatformRoom, PlatformTeammate, db)
from app.tasks.queue import hand_off
from app.workspace.events import EventBus
from test_service import svc  # Reuse the isolated offline service/database fixture.


@pytest.fixture
def social(svc, monkeypatch):
    network = {"rooms": {}, "sends": [], "registers": []}

    class PersonalOrg(AgentPlatformOrg):
        async def connect(self):
            identity = self.identity
            self.owner = Owner(id=identity, name=identity.title(), handle=identity)
            self._rest = object()

        async def close(self):
            self._rest = None

        async def create_room(self, title):
            room = 'fake-room-' + self.identity + '-' + uuid4().hex
            network["rooms"][room] = {self.owner.id}
            return room

        async def add_participant(self, room, band_id):
            network["rooms"][room].add(band_id)

        async def remove_participant(self, room, band_id):
            network["rooms"][room].discard(band_id)

        async def _members(self, room):
            return network["rooms"][room]

        async def send(self, room, content, mentions):
            network["sends"].append((self.owner.id, room, content, mentions))
            return uuid4().hex

    async def noop(*args, **kwargs):
        pass

    async def identity(key):
        if key.startswith("band_a_"):
            return BandIdentity(id=key.removeprefix("band_a_"), name="Remote Writer", handle="remote/writer")
        owner, name = key.removeprefix("key-").split(":")
        return BandIdentity(id=f"{owner}/{name}", name=name, handle=f"{owner}/{name.lower()}")

    monkeypatch.setattr(service_module, "AgentPlatformOrg", PersonalOrg)
    monkeypatch.setattr(service_module, "fetch_identity", identity)
    monkeypatch.setattr(http, "service", svc)
    monkeypatch.setattr(people, "service", svc)
    monkeypatch.setattr(svc, "startup", noop)
    monkeypatch.setattr(svc, "shutdown", noop)
    monkeypatch.setattr(svc, "start_agent", noop)
    with TestClient(http.app) as client:
        users = []
        for name in ("alice", "bob", "cara"):
            result = client.post("/api/auth/signup", json={
                "username": name, "display_name": name.title(), "password": "test-password", "band_api_key": f"band_a_{name}",
            }).json()
            headers = {"Authorization": f"Bearer {result['token']}"}
            users.append((result["user"]["id"], headers))
        yield client, users, network, svc


def friends(client, users):
    (_, a), (_, b), (_, c) = users
    requested = client.post("/api/people/requests", json={"handle": "@bob"}, headers=a)
    assert requested.status_code == 200, requested.text
    request_id = requested.json()["id"]
    assert client.post(f"/api/people/requests/{request_id}/decision", json={"accept": True}, headers=c).status_code == 404
    accepted = client.post(f"/api/people/requests/{request_id}/decision", json={"accept": True}, headers=b)
    assert accepted.status_code == 200, accepted.text


def test_agent_management_account_is_separate_from_agent_transport(social):
    client, ((alice, a), _, _), _, svc = social
    assert client.get("/api/band/account", headers=a).json()["connected"] is False
    rejected = client.put("/api/band/connection", headers=a, json={"api_key": "band_u_unused"})
    assert rejected.status_code == 422 and "band_u_unused" not in rejected.text
    with db() as session:
        assert session.get(UserBandAccount, alice) is None
    channel = client.post("/api/channels", json={"name": "personal-replies"}, headers=a).json()
    assert svc.message_reply_to(alice, channel["id"]) == "alice"
    assert asyncio.run(svc.room_org(channel["id"])).identity == "alice"


def test_contact_handshake_and_human_dm_have_no_agents(social):
    client, users, network, _ = social
    (alice, a), (bob, b), (_, c) = users
    assert client.post(f"/api/people/{bob}/dm", headers=a).status_code == 403
    friends(client, users)
    contacts = client.get("/api/people", headers=a).json()["contacts"]
    assert contacts[0]["user_id"] == bob
    opened = client.post(f"/api/people/{bob}/dm", headers=a)
    assert opened.status_code == 200, opened.text
    dm = opened.json()
    assert dm["agent_ids"] == [] and dm["dm_user_ids"] == [alice, bob]
    assert client.post(f"/api/people/{alice}/dm", headers=b).json()["id"] == dm["id"]
    assert client.get(f"/api/channels/{dm['id']}/messages", headers=c).status_code == 404
    assert dm["id"] not in [x["id"] for x in client.get("/api/bootstrap", headers=c).json()["channels"]]
    sent = client.post(f"/api/channels/{dm['id']}/messages", json={"content": "Hello @Master"}, headers=b)
    assert sent.status_code == 200 and sent.json()["mentions"] == []
    with db() as session:
        assert not session.exec(select(AgentTask).where(AgentTask.channel_id == dm["id"])).all()
    assert dm["band_room_id"] is None
    assert network["sends"] == [] and network["rooms"] == {}


def test_human_contacts_and_private_channels_work_with_band_offline(social):
    client, users, network, svc = social
    (_, a), (bob, b), (_, c) = users
    friends(client, users)
    dm = client.post(f"/api/people/{bob}/dm", headers=a).json()
    assert dm["band_room_id"] is None
    assert client.post(f"/api/channels/{dm['id']}/messages", json={"content": "Hi Bob"}, headers=a).status_code == 200
    assert client.get(f"/api/channels/{dm['id']}/messages", headers=b).json()[0]["content"] == "Hi Bob"
    channel = client.post("/api/channels", json={"name": "humans-only"}, headers=a).json()
    # The workspace key is offline. Alice's own Band account still opens the room.
    assert channel["private"] and channel["band_room_id"] and not channel["agent_ids"]
    assert network["rooms"][channel["band_room_id"]] == {"alice"}
    assert client.post(f"/api/channels/{channel['id']}/invitations", json={"user_id": bob}, headers=a).status_code == 200
    invite = client.get("/api/people/invitations", headers=b).json()[0]
    assert client.post(f"/api/people/invitations/{invite['id']}/decision", json={"accept": True}, headers=b).status_code == 200
    assert client.get(f"/api/channels/{channel['id']}/messages", headers=c).status_code == 404
    assert network["sends"] == []


def test_local_contact_requests_are_idempotent_and_need_recipient_consent(social):
    client, users, _, _ = social
    (alice, a), (bob, b), (_, c) = users
    requested = client.post("/api/people/requests", json={"handle": "bob"}, headers=a).json()
    assert client.post("/api/people/requests", json={"handle": "bob"}, headers=a).json()["id"] == requested["id"]
    assert client.post("/api/people/requests", json={"handle": "alice"}, headers=b).status_code == 409
    decision = f"/api/people/requests/{requested['id']}/decision"
    assert client.post(decision, json={"accept": True}, headers=a).status_code == 404
    assert client.post(decision, json={"accept": True}, headers=c).status_code == 404
    assert client.post(f"/api/people/{bob}/dm", headers=a).status_code == 403
    assert client.post(decision, json={"accept": True}, headers=b).status_code == 200
    assert client.post(decision, json={"accept": True}, headers=b).status_code == 200
    assert client.get("/api/people", headers=a).json()["contacts"][0]["user_id"] == bob
    assert client.get("/api/people", headers=b).json()["contacts"][0]["user_id"] == alice


def test_app_users_share_agent_channels_using_the_workspace_band_account(social):
    client, users, network, _ = social
    (alice, a), (_, b), _ = users
    friends(client, users)
    channel = client.post("/api/channels", json={"name": "with-ai"}, headers=a).json()
    agent = client.post("/api/agents", json={"name": "Helper", "channel_id": channel["id"]}, headers=a).json()
    assert agent["owner_id"] == alice and agent["band_auth"] == "platform"
    client.post(f"/api/channels/{channel['id']}/invitations", json={"user_id": users[1][0]}, headers=a)
    invite = client.get("/api/people/invitations", headers=b).json()[0]
    client.post(f"/api/people/invitations/{invite['id']}/decision", json={"accept": True}, headers=b)
    sent = client.post(f"/api/channels/{channel['id']}/messages", json={"content": "@Helper hello"}, headers=b)
    assert sent.status_code == 200
    with db() as session:
        task = session.exec(select(AgentTask).where(AgentTask.agent_id == agent["id"])).one()
        assert task.reply_to == "alice"
    assert network["sends"][-1][0] == "alice"
    assert network["sends"][-1][2] == "**Bob:** @Helper hello"
    assert client.post(f"/api/agents/{agent['id']}/dm", headers=b).status_code == 403


def test_invitations_require_acceptance_and_leave_revokes_access(social):
    client, users, network, _ = social
    (_, a), (bob, b), (_, c) = users
    friends(client, users)
    channel = client.post("/api/channels", json={"name": "launch"}, headers=a).json()
    assert channel["private"] and channel["agent_ids"] == []
    path = f"/api/channels/{channel['id']}"
    assert client.post(path + "/invitations", json={"user_id": bob}, headers=b).status_code == 403
    assert client.post(path + "/invitations", json={"user_id": bob}, headers=a).status_code == 200
    invitation = client.get("/api/people/invitations", headers=b).json()[0]
    assert client.get(path + "/messages", headers=b).status_code == 404
    assert client.post(path + "/join", headers=b).status_code == 404
    decision = f"/api/people/invitations/{invitation['id']}/decision"
    assert client.post(decision, json={"accept": True}, headers=c).status_code == 404
    assert client.post(decision, json={"accept": True}, headers=b).status_code == 200
    assert network["rooms"][channel["band_room_id"]] == {"alice"}
    assert client.post(decision, json={"accept": True}, headers=b).status_code == 200
    assert client.get(path + "/messages", headers=b).status_code == 200
    assert client.post(path + "/leave", headers=b).status_code == 200
    assert client.get(path + "/messages", headers=b).status_code == 404
    assert "bob" not in network["rooms"][channel["band_room_id"]]


def test_personal_agents_use_owner_band_and_only_owner_can_manage_or_dm(social):
    client, users, network, svc = social
    (alice, a), (_, b), _ = users
    friends(client, users)
    channel = client.post("/api/channels", json={"name": "work"}, headers=a).json()
    created = client.post("/api/agents", json={"name": "Scout", "channel_id": channel["id"]}, headers=a)
    assert created.status_code == 200, created.text
    agent = created.json()
    assert agent["owner_id"] == alice and agent["band_auth"] == "platform"
    assert agent["id"] not in [x["id"] for x in client.get("/api/bootstrap", headers=b).json()["agents"]]
    for method, suffix, payload in (("POST", "/dm", None), ("POST", "/restart", None), ("PATCH", "", {"instructions": "steal"}), ("DELETE", "", None)):
        assert client.request(method, f"/api/agents/{agent['id']}{suffix}", json=payload, headers=b).status_code == 403
    with db() as session:
        connection = AgentConnection(id=uuid4().hex, provider="codex", label="Alice", auth_method="api_key",
                                     secret_enc=encrypt_secret("{}"), created_by=alice)
        session.add(connection)
        session.commit()
    assert client.get("/api/agent-connections", headers=b).json()["connections"] == []
    assert client.delete(f"/api/agent-connections/{connection.id}", headers=b).status_code == 404
    # An explicitly invited human can address the agent here, but still cannot open a private DM.
    bob = users[1][0]
    client.post(f"/api/channels/{channel['id']}/invitations", json={"user_id": bob}, headers=a)
    invite = client.get("/api/people/invitations", headers=b).json()[0]
    client.post(f"/api/people/invitations/{invite['id']}/decision", json={"accept": True}, headers=b)
    client.post(f"/api/channels/{channel['id']}/messages", json={"content": "@Scout hello"}, headers=b)
    with db() as session:
        task = session.exec(select(AgentTask).where(AgentTask.agent_id == agent["id"])).one()
        assert task.reply_to == "alice"
    assert client.post(f"/api/agents/{agent['id']}/dm", headers=b).status_code == 403
    assert hand_off(task, teammate="private-agent-elsewhere", request="Read this", report_back=False, call_id="cross-room").get("error")
    bob_agent = client.post("/api/agents", json={"name": "Writer"}, headers=b).json()
    path = f"/api/channels/{channel['id']}/agents"
    assert client.post(path, json={"agent_id": bob_agent["id"]}, headers=b).status_code == 200
    # App teammates share transport without becoming separate native participants.
    assert network["rooms"][channel["band_room_id"]] == {"alice"}
    assert client.delete(path + f"/{bob_agent['id']}", headers=a).status_code == 200
    assert client.post(path, json={"agent_id": bob_agent["id"]}, headers=b).status_code == 200


def test_private_events_reach_only_members(social):
    client, users, _, _ = social
    (alice, a), (bob, b), (cara, _) = users
    friends(client, users)
    dm = client.post(f"/api/people/{bob}/dm", headers=a).json()
    from app.workspace.access import channel_audience
    bus = EventBus()
    bus.audience = channel_audience
    received = {alice: [], bob: [], cara: []}

    class Socket:
        def __init__(self, user_id):
            self.user_id = user_id

        async def send_json(self, event):
            received[self.user_id].append(event)

    async def scenario():
        for user_id in received:
            await bus.connect(user_id, Socket(user_id))
        for items in received.values():
            items.clear()
        await bus.publish({"type": "message", "message": {"channel_id": dm["id"], "content": "Private"}})
    asyncio.run(scenario())
    assert len(received[alice]) == len(received[bob]) == 1 and not received[cara]


def test_shared_agent_creates_private_channel_teammates_for_its_owner(social):
    client, ((alice, a), (_, b), _), network, svc = social
    channel = client.post("/api/channels", json={"name": "private-team"}, headers=a).json()
    with db() as session:
        master = AgentProfile(name="Master", band_agent_id="master", band_api_key_enc=encrypt_secret("master"),
                              is_master=True)
        session.add(master)
        session.commit()
    child = asyncio.run(svc.create_agent(name="Scout", role="researcher", instructions="Private task instructions",
                                        model="test-model", channel_id=channel["id"], created_by_agent=master.id))
    with db() as session:
        assert session.get(BandAgentOwner, child.id).user_id == alice
    assert network["registers"] == []
    assert child.id not in [agent["id"] for agent in client.get("/api/bootstrap", headers=b).json()["agents"]]
    assert client.post(f"/api/agents/{child.id}/dm", headers=b).status_code == 403


def test_invitation_acceptance_does_not_require_a_band_connection(social, monkeypatch):
    client, users, _, svc = social
    (alice, a), (bob, b), _ = users
    friends(client, users)
    channel = client.post("/api/channels", json={"name": "failed-join"}, headers=a).json()
    client.post(f"/api/channels/{channel['id']}/invitations", json={"user_id": bob}, headers=a)
    invite = client.get("/api/people/invitations", headers=b).json()[0]

    async def refuse(*args):
        raise RuntimeError("provider error with a credential that must not reach the browser")
    monkeypatch.setattr(service_module.AgentPlatformOrg, "add_participant", refuse)
    response = client.post(f"/api/people/invitations/{invite['id']}/decision", json={"accept": True}, headers=b)
    assert response.status_code == 200 and "credential" not in response.text
    assert client.get(f"/api/channels/{channel['id']}/messages", headers=b).status_code == 200
    with db() as session:
        assert session.get(ChannelMember, (channel["id"], "human", bob)) is not None


def test_platform_listener_rejects_uninvited_senders_and_deduplicates(social):
    client, ((alice, a), _, _), network, svc = social
    channel = client.post("/api/channels", json={"name": "listen"}, headers=a).json()
    client.post("/api/agents", json={"name": "Scout", "channel_id": channel["id"]}, headers=a)

    async def receive(sender, content, message_id):
        await svc.platform_message(alice, "alice", channel["band_room_id"],
            Obj(id=message_id, sender_id=sender, sender_name=sender, content=content))

    asyncio.run(receive("bob", "@Scout uninvited", "uninvited"))
    posted = client.post(f"/api/channels/{channel['id']}/messages", json={"content": "@Scout from app"}, headers=a).json()
    asyncio.run(receive("alice", posted["content"], "relay-receipt"))
    network["rooms"][channel["band_room_id"]].add("peer")
    asyncio.run(receive("peer", "@Scout a native request", "native"))
    asyncio.run(receive("peer", "@Scout a native request", "native"))
    with db() as session:
        tasks = session.exec(select(AgentTask).where(AgentTask.channel_id == channel["id"])).all()
        assert len(tasks) == 2
        assert {t.reply_to for t in tasks} == {"alice", "peer"}


def test_another_accounts_agent_is_added_by_the_room_owner(social, monkeypatch):
    client, users, network, svc = social
    (_, a), (bob, b), _ = users
    friends(client, users)
    bob_agent = client.post("/api/agents", json={"name": "Pixel", "role": "writer", "band_agent_api_key": "band_a_pixel"}, headers=b)
    assert bob_agent.status_code == 200, bob_agent.text
    channel = client.post("/api/channels", json={"name": "lab"}, headers=a)
    assert channel.status_code == 200, channel.text
    invited = client.post(f"/api/channels/{channel.json()['id']}/invitations", json={"user_id": bob}, headers=a)
    assert invited.status_code == 200, invited.text
    invite = client.get("/api/people/invitations", headers=b).json()[0]
    assert client.post(f"/api/people/invitations/{invite['id']}/decision", json={"accept": True}, headers=b).status_code == 200
    callers = []

    async def record(self, room, band_id):
        callers.append(self.owner.id)
        network["rooms"][room].add(band_id)

    monkeypatch.setattr(service_module.AgentPlatformOrg, "add_participant", record)
    added = client.post(
        f"/api/channels/{channel.json()['id']}/agents",
        json={"agent_id": bob_agent.json()["id"]},
        headers=b,
    )
    assert added.status_code == 200, added.text
    assert callers == ["alice"]
    with db() as session:
        assert session.get(ChannelMember, (channel.json()["id"], "agent", bob_agent.json()["id"])) is not None


def test_agent_key_connects_and_chats_without_human_api(social):
    from app.persistence.models import BandAgentLink
    from app.tasks.worker import complete, deliver
    from app.tasks.queue import update_task
    client, users, network, svc = social
    signed = client.post('/api/auth/signup', json={
        'username': 'single-user', 'display_name': 'Single user', 'password': 'test-password', 'band_api_key': 'band_a_signup-single-user',
    }).json()
    headers = {'Authorization': f"Bearer {signed['token']}"}
    key = 'band_a_existing-writer'
    channel = client.post('/api/channels', headers=headers, json={'name': 'writing'}).json()
    created = client.post('/api/agents', headers=headers, json={
        'name': 'Writer', 'band_agent_api_key': key, 'specialty': 'Clear writing', 'plugins': [], 'channel_id': channel['id'],
    })
    assert created.status_code == 200, created.text
    profile = created.json()
    assert key not in created.text and profile['band_auth'] == 'agent_key'
    assert profile['owner_id'] == signed['user']['id']
    assert network['registers'] == []
    assert network['rooms'][channel['band_room_id']] == {'signup-single-user', 'existing-writer'}
    with db() as session:
        stored = session.get(AgentProfile, profile['id'])
        assert decrypt_secret(stored.band_api_key_enc) == key
        assert session.get(BandAgentLink, profile['id'])
        assert session.get(ChannelMember, (channel['id'], 'agent', profile['id']))
        assert session.get(UserBandAccount, signed['user']['id']) is None
    dm = client.post(f"/api/agents/{profile['id']}/dm", headers=headers)
    assert dm.status_code == 200 and dm.json()['band_room_id']
    assert client.post(f"/api/agents/{profile['id']}/dm", headers=users[1][1]).status_code == 403
    sent = client.post(f"/api/channels/{dm.json()['id']}/messages", headers=headers, json={'content': 'Draft an introduction.'})
    assert sent.status_code == 200, sent.text
    with db() as session:
        task = session.exec(select(AgentTask).where(AgentTask.agent_id == profile['id'])).one()
        assert task.room_id == dm.json()['band_room_id'] and task.reply_to == 'signup-single-user'
    task = update_task(task.id, status='running')
    complete(task, 'Here is your introduction.')
    with db() as session:
        finished = session.get(AgentTask, task.id)
    asyncio.run(deliver(finished))
    replies = client.get(f"/api/channels/{dm.json()['id']}/messages", headers=headers).json()
    assert any(m['content'] == 'Here is your introduction.' and m['to_humans'] for m in replies)
    with db() as session:
        assert session.get(AgentTask, task.id).delivery_status == 'sent'
    assert len(network['sends']) == 1
    assert network['sends'][0][0] == 'signup-single-user'
    assert network['sends'][0][2] == '**Single user:** Draft an introduction.'
    assert client.delete(f"/api/agents/{profile['id']}", headers=users[1][1]).status_code == 403
    # No Human API deletion is possible: the workspace connection is offline.
    assert client.delete(f"/api/agents/{profile['id']}", headers=headers).status_code == 200
    with db() as session:
        assert session.get(BandAgentLink, profile['id']) is None
        assert session.get(AgentProfile, profile['id']) is None


@pytest.mark.parametrize('key', ['band_u_human-key', 'sk-model-key', 'band_a_rejected-key'])
def test_agent_key_authentication_errors_never_expose_or_store_keys(social, monkeypatch, key):
    client, users, _, _ = social
    async def invalid(_key):
        raise ValueError('Provider rejected secret ' + key)
    monkeypatch.setattr(service_module, 'fetch_identity', invalid)
    response = client.post('/api/agents', headers=users[0][1], json={'name': 'Rejected Writer', 'band_agent_api_key': key})
    assert response.status_code == 422
    assert key not in response.text
    with db() as session:
        assert session.exec(select(AgentProfile).where(AgentProfile.name == 'Rejected Writer')).first() is None


def test_agent_key_cannot_be_claimed_twice_or_exposed_in_bootstrap(social):
    client, users, _, _ = social
    key = 'band_a_owned-writer'
    first = client.post('/api/agents', headers=users[0][1], json={'name': 'Owned Writer', 'band_agent_api_key': key})
    assert first.status_code == 200, first.text
    for _, headers in users[:2]:
        response = client.post('/api/agents', headers=headers, json={'name': 'Another Writer', 'band_agent_api_key': key})
        assert response.status_code == 409 and key not in response.text
    assert key not in client.get('/api/bootstrap', headers=users[0][1]).text
    response = client.post('/api/agents', headers=users[0][1], json={'name': 'Large key', 'band_agent_api_key': 'band_a_' + 's' * 2000})
    assert response.status_code == 422 and 's' * 100 not in response.text


def test_agent_key_listener_starts_without_a_human_connection(social, monkeypatch):
    client, users, _, svc = social
    started = []
    async def start(profile, *, owner_id):
        started.append((profile.band_agent_id, owner_id))
    monkeypatch.setattr(service_module, 'configuration', lambda: ('model-key', ['test-model'], 'test-model'))
    monkeypatch.setattr(svc.agents, 'start', start)
    monkeypatch.setattr(svc, 'start_agent', service_module.AppService.start_agent.__get__(svc))
    result = client.post('/api/agents', headers=users[0][1], json={'name': 'Listening Writer', 'band_agent_api_key': 'band_a_listening-writer'})
    assert result.status_code == 200, result.text
    assert started == [('listening-writer', '')]


def test_agent_key_handoffs_and_delivery_cannot_export_app_conversations(social):
    from app.tasks.queue import update_task
    from app.tasks.worker import complete, deliver
    client, users, _, _ = social
    headers = users[0][1]
    # An existing account without a saved platform connection has app-only rooms.
    with db() as session:
        session.delete(session.get(UserBandConnection, users[0][0]))
        session.commit()
    channel = client.post('/api/channels', headers=headers, json={'name': 'local-agent-team'}).json()
    profiles = []
    for name in ('Writer', 'Reviewer'):
        result = client.post('/api/agents', headers=headers, json={
            'name': name, 'band_agent_api_key': 'band_a_team-' + name.lower(), 'channel_id': channel['id'],
        })
        assert result.status_code == 200, result.text
        profiles.append(result.json())
    sent = client.post(f"/api/channels/{channel['id']}/messages", headers=headers, json={'content': '@Writer write a draft.'})
    assert sent.status_code == 200
    with db() as session:
        task = session.exec(select(AgentTask).where(AgentTask.agent_id == profiles[0]['id'])).one()
    task = update_task(task.id, room_id='foreign-room', reply_to='native-human')
    result = hand_off(task, teammate='Reviewer', request='Review the draft.', report_back=False, call_id='agent-key-review')
    assert result['status'] == 'handed_off'
    with db() as session:
        child = session.get(AgentTask, result['task_id'])
        assert child.room_id == child.reply_to == ''
    # Even old or externally supplied task metadata must not mirror this app's reply.
    task = update_task(task.id, status='running')
    complete(task, 'The app-only draft.')
    with db() as session:
        finished = session.get(AgentTask, task.id)
    asyncio.run(deliver(finished))
    with db() as session:
        assert session.get(AgentTask, task.id).delivery_status == 'local_only'
