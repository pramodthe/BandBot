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
from app.band.org import BandOrg, Owner
from app.persistence.models import (AgentConnection, AgentProfile, AgentTask, BandAgentOwner, ChannelMember, User,
                                    Message, UserBandAccount, db)
from app.tasks.queue import hand_off
from app.workspace.events import EventBus
from test_service import svc  # Reuse the isolated offline service/database fixture.


@pytest.fixture
def social(svc, monkeypatch):
    network = {"rooms": {}, "sends": [], "registers": []}

    class PersonalOrg(BandOrg):
        async def connect(self):
            identity = self._api_key.removeprefix("band_u_")
            self.owner = Owner(id=identity, name=identity.title(), handle=identity)
            self._rest = object()

        async def close(self):
            self._rest = None

        async def create_room(self, title):
            room = uuid4().hex
            network["rooms"][room] = {self.owner.id}
            return room

        async def add_participant(self, room, band_id):
            network["rooms"][room].add(band_id)

        async def remove_participant(self, room, band_id):
            network["rooms"][room].discard(band_id)

        async def send(self, room, content, mentions):
            network["sends"].append((self.owner.id, room, content, mentions))
            return uuid4().hex

        async def register_agent(self, name, description):
            network["registers"].append(self.owner.id)
            return f"{self.owner.id}/{name}", f"key-{self.owner.id}:{name}"

        async def messages_since(self, room, since):
            return []

    async def noop(*args, **kwargs):
        pass

    async def identity(key):
        owner, name = key.removeprefix("key-").split(":")
        return BandIdentity(id=f"{owner}/{name}", name=name, handle=f"{owner}/{name.lower()}")

    monkeypatch.setattr(people, "BandOrg", PersonalOrg)
    monkeypatch.setattr(service_module, "BandOrg", PersonalOrg)
    monkeypatch.setattr(service_module, "fetch_identity", identity)
    monkeypatch.setattr(http, "service", svc)
    monkeypatch.setattr(people, "service", svc)
    monkeypatch.setattr(svc, "startup", noop)
    monkeypatch.setattr(svc, "shutdown", noop)
    monkeypatch.setattr(svc, "start_agent", noop)
    svc.org = PersonalOrg("band_u_workspace")
    asyncio.run(svc.org.connect())
    with TestClient(http.app) as client:
        users = []
        for name in ("alice", "bob", "cara"):
            result = client.post("/api/auth/signup", json={
                "username": name, "display_name": name.title(), "password": "test-password",
            }).json()
            headers = {"Authorization": f"Bearer {result['token']}"}
            linked = client.put("/api/band/account", json={"api_key": f"band_u_{name}"}, headers=headers)
            assert linked.status_code == 200, linked.text
            users.append((result["user"]["id"], headers))
        yield client, users, network, svc


def link(client, headers, name):
    response = client.put("/api/band/account", json={"api_key": f"band_u_{name}"}, headers=headers)
    assert response.status_code == 200, response.text


def friends(client, users):
    (_, a), (_, b), (_, c) = users
    requested = client.post("/api/people/requests", json={"handle": "@bob"}, headers=a)
    assert requested.status_code == 200, requested.text
    request_id = requested.json()["id"]
    assert client.post(f"/api/people/requests/{request_id}/decision", json={"accept": True}, headers=c).status_code == 404
    accepted = client.post(f"/api/people/requests/{request_id}/decision", json={"accept": True}, headers=b)
    assert accepted.status_code == 200, accepted.text


def test_signup_does_not_require_a_band_key_and_linked_rooms_use_it(social):
    client, ((alice, a), (_, b), _), network, svc = social
    created = client.post("/api/auth/signup", json={"username": "erin", "display_name": "Erin", "password": "test-password"})
    assert created.status_code == 200, created.text
    assert "band_u_" not in created.text
    with db() as session:
        erin = session.exec(select(User).where(User.username == "erin")).one()
        assert session.get(UserBandAccount, erin.id) is None
    with db() as session:
        account = session.get(UserBandAccount, alice)
        assert account.band_user_id == "alice"
        assert account.secret_enc != "band_u_alice" and decrypt_secret(account.secret_enc) == "band_u_alice"
    private = client.post("/api/channels", json={"name": "personal-replies"}, headers=a).json()
    assert svc.message_reply_to(alice, private["id"]) == "alice"
    assert asyncio.run(svc.room_org(private["id"])).owner.id == "alice"
    general = next(c for c in client.get("/api/bootstrap", headers=a).json()["channels"] if c["name"] == "general")
    assert svc.message_reply_to(alice, general["id"]) == svc.org.owner.id
    agent = client.post("/api/agents", json={"name": "Helper", "channel_id": private["id"]}, headers=a).json()
    assert agent["owner_id"] == alice and network["registers"] == ["alice"]
    assert "band_u_alice" not in client.get("/api/bootstrap", headers=a).text
    assert client.put("/api/band/account", json={"api_key": "band_u_alice"}, headers=b).status_code == 409
    assert client.put("/api/band/account", json={"api_key": "band_u_different"}, headers=a).status_code == 409
    invalid = client.put("/api/band/account", json={"api_key": "secret"}, headers=a)
    assert invalid.status_code == 422 and "secret" not in invalid.text


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
    svc.org._rest = None
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
    assert agent["owner_id"] == alice and network["registers"] == ["alice"]
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
    link(client, a, "alice")
    channel = client.post("/api/channels", json={"name": "work"}, headers=a).json()
    created = client.post("/api/agents", json={"name": "Scout", "channel_id": channel["id"]}, headers=a)
    assert created.status_code == 200, created.text
    agent = created.json()
    assert agent["owner_id"] == alice and network["registers"] == ["alice"]
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
    bob_org = svc.org
    bob_org.note_participant(channel["band_room_id"], bob_agent["band_agent_id"])
    bob_org._room_members[channel["band_room_id"]] = {bob_agent["band_agent_id"]}
    assert client.delete(path + f"/{bob_agent['id']}", headers=a).status_code == 200
    assert bob_agent["band_agent_id"] not in bob_org._room_members[channel["band_room_id"]]
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
    assert network["registers"] == ["alice"]
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
    monkeypatch.setattr(svc.org, "add_participant", refuse)
    svc.org._rest = None
    response = client.post(f"/api/people/invitations/{invite['id']}/decision", json={"accept": True}, headers=b)
    assert response.status_code == 200 and "credential" not in response.text
    assert client.get(f"/api/channels/{channel['id']}/messages", headers=b).status_code == 200
    with db() as session:
        assert session.get(ChannelMember, (channel["id"], "human", bob)) is not None


def test_listener_rejects_uninvited_senders_and_deduplicates_personal_relay(social):
    client, users, _, _ = social
    (alice, a), (_, b), _ = users
    friends(client, users)
    link(client, a, "alice")
    channel = client.post("/api/channels", json={"name": "listen"}, headers=a).json()
    agent = client.post("/api/agents", json={"name": "Scout", "channel_id": channel["id"]}, headers=a).json()
    adapter = QueuedAgentAdapter(profile_id=agent["id"], reply_to="alice")

    async def deliver(sender, content, message_id):
        await adapter.on_message(Obj(id=message_id, sender_id=sender, content=content), None, [], None, None,
                                 is_session_bootstrap=False, room_id=channel["band_room_id"])
    asyncio.run(deliver("bob", "@Scout read my request", "uninvited"))
    posted = client.post(f"/api/channels/{channel['id']}/messages", json={"content": "@Scout from app"}, headers=a).json()
    asyncio.run(deliver("alice", posted["content"], "relay-receipt"))
    asyncio.run(deliver("alice", "A new request from Band", "native-band"))
    with db() as session:
        tasks = session.exec(select(AgentTask).where(AgentTask.channel_id == channel["id"])).all()
        assert len(tasks) == 2
        assert {t.reply_to for t in tasks} == {"alice"}
        assert session.get(BandAgentOwner, agent["id"]).user_id == alice


def test_band_poll_waits_for_the_outbound_message_receipt(social, monkeypatch):
    client, users, _, svc = social
    (alice, a), (bob, _), _ = users
    friends(client, users)
    channel = client.post("/api/channels", json={"name": "poll-receipt"}, headers=a).json()
    with db() as session:
        message = Message(channel_id=channel["id"], author_type="human", author_id=alice,
                          author_name="Alice", content="Hello from the app")
        session.add(message)
        session.commit()
    org = asyncio.run(svc.room_org(channel["id"]))
    now = datetime.now(timezone.utc)

    async def scenario():
        sending, release = asyncio.Event(), asyncio.Event()

        async def delayed_send(*args):
            sending.set()
            await release.wait()
            return "outbound-receipt"

        async def inbox(*args):
            return [Obj(id="outbound-receipt", sender_id=org.owner.id, content=f"**Alice:** {message.content}", inserted_at=now)]
        monkeypatch.setattr(org, "send", delayed_send)
        monkeypatch.setattr(org, "messages_since", inbox)
        relay = asyncio.create_task(svc._relay(channel["id"], message.id, "Alice", message.content, [], alice))
        await sending.wait()
        poll = asyncio.create_task(svc.sync_room(channel["id"], channel["band_room_id"], now))
        await asyncio.sleep(0)
        assert not poll.done()
        release.set()
        await asyncio.gather(relay, poll)
    asyncio.run(scenario())
    with db() as session:
        messages = session.exec(select(Message).where(Message.channel_id == channel["id"])).all()
        assert len(messages) == 1 and messages[0].band_message_id == "outbound-receipt"
