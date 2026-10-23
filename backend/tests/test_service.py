from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import app.workspace.service as service_module
from app.band.listener import BandIdentity
from app.auth import encrypt_secret
from app.band.org import BandOrg, Owner
from app.persistence.models import (AgentPersona, AgentProfile, AgentTask, AuditEvent, Budget, Channel, ChannelMember, ChannelPolicy,
                                    DirectMessage, Message, TaskCacheUsage, TaskUsage, User, WorkspaceRole, db, init_db)
from app.config import settings
from app.workspace.service import AppService
from app.persistence.models import UserBandAccount, PrivateConversation, HumanDirectMessage, BandAgentOwner, ChannelInvitation
from app.persistence.models import MessageComponent, ComponentResponse, TaskInput, AgentRuntime, AgentConnection, AgentOAuthAttempt
from app.persistence.models import AppContact, AppContactRequest, BandRoomAccount
from app.persistence.models import (AuthSession, ClientMessage, ConnectionHealth, ContactBlock, PasswordReset,
                                    RecoveryAddress, WorkerHeartbeat)


class FakeOrg(BandOrg):
    def __init__(self) -> None:
        super().__init__()
        self.owner = Owner(id="user-owner", name="Pat Owner", handle="pat")
        self._rest = object()
        self.rooms: list[str] = []
        self.participants: dict[str, set[str]] = {}
        self.registered: list[str] = []
        self.inbox: list[SimpleNamespace] = []

    async def create_room(self, title: str) -> str:
        room_id = f"room-{len(self.rooms) + 1}"
        self.rooms.append(title)
        self.participants[room_id] = {self.owner.id}
        return room_id

    async def add_participant(self, room_id: str, band_id: str) -> None:
        self.participants.setdefault(room_id, set()).add(band_id)

    async def register_agent(self, name: str, description: str) -> tuple[str, str]:
        self.registered.append(name)
        return f"agent-{name.lower()}", f"key-{name.lower()}"

    async def messages_since(self, room_id: str, since):
        return list(self.inbox)

    async def send(self, room_id: str, content: str, mentions: list[tuple[str, str]]) -> str:
        return f"band-{len(content)}"


@pytest.fixture
def svc(monkeypatch) -> Iterator[AppService]:
    init_db()

    def clear_database() -> None:
        with db() as session:
            for model in (ClientMessage, AuthSession, RecoveryAddress, PasswordReset, ContactBlock, ConnectionHealth,
                          WorkerHeartbeat, AppContact, AppContactRequest, BandRoomAccount,
                          ComponentResponse, TaskInput, MessageComponent, AgentRuntime, AgentOAuthAttempt, AgentConnection,
                          ChannelInvitation, HumanDirectMessage, PrivateConversation, BandAgentOwner, UserBandAccount,
                          AgentTask, DirectMessage, Message, ChannelMember, AgentPersona, AgentProfile,
                          ChannelPolicy, WorkspaceRole, Channel, User, AuditEvent,
                          TaskCacheUsage, TaskUsage, Budget):
                for row in session.exec(select(model)).all():
                    session.delete(row)
            session.commit()

    clear_database()

    async def identity(api_key: str) -> BandIdentity:
        name = api_key.removeprefix("key-")
        return BandIdentity(id=f"agent-{name}", name=name.title(), handle=f"pat/{name}")

    monkeypatch.setattr(service_module, "fetch_identity", identity)
    service = AppService()
    service.org = FakeOrg()
    try:
        yield service
    finally:
        clear_database()


def _messages(channel_id: int) -> list[Message]:
    with db() as session:
        return session.exec(select(Message).where(Message.channel_id == channel_id).order_by(Message.id)).all()


def _user() -> User:
    with db() as session:
        user = User(username="alice", display_name="Alice", password_hash="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def test_channels_are_band_rooms_and_visible_agents_can_be_created(svc: AppService):
    async def scenario():
        await svc._ensure_master()
        user = _user()
        channel = await svc.create_channel("launch", "", user)
        master = svc.master()
        assert svc.org.rooms == ["#launch"]
        assert channel["agent_ids"] == []
        await svc.add_agent_to_channel(channel["id"], master)
        assert master.band_agent_id in svc.org.participants[channel["band_room_id"]]
        scout = await svc.create_agent(name="Scout", role="researcher", instructions="Find facts",
                                       model=master.model, channel_id=channel["id"], created_by=user.id)
        assert svc.org.registered == [settings.master_agent_name, "Scout"]
        assert scout.band_agent_id in svc.org.participants[channel["band_room_id"]]
        with db() as session:
            assert session.get(ChannelMember, (channel["id"], "agent", scout.id)) is not None
    asyncio.run(scenario())


def test_agent_replies_to_the_owner_reach_humans_and_polling_dedupes(svc: AppService):
    with db() as session:
        channel = Channel(name="svc", band_room_id="room-svc")
        writer = AgentProfile(name="Writer", handle="pat/writer", band_agent_id="agent-writer", band_api_key_enc=encrypt_secret("k"))
        session.add_all([channel, writer])
        session.commit()
        channel_id, writer_id = channel.id, writer.id

    asyncio.run(svc.agent_message(writer_id, "room-svc", "@pat Here is the draft, Alice.", ["pat"], "m1"))
    asyncio.run(svc.agent_message(writer_id, "room-unknown", "lost", ["pat"], "m2"))

    now = datetime.now(timezone.utc)

    def item(i: int, sender_id: str, content: str, name: str | None = None):
        return SimpleNamespace(
            id=f"m{i}", sender_id=sender_id, sender_name=name, content=content, inserted_at=now + timedelta(seconds=i)
        )

    svc.org.inbox = [
        item(1, "agent-writer", "@pat Here is the draft, Alice."),
        item(3, "user-owner", "**Alice:** hi"),
        item(4, "user-guest", "@pat hello from the Band app", "Guest"),
        item(5, "agent-writer", "@pat fallback reply"),
    ]
    asyncio.run(svc.sync_room(channel_id, "room-svc", now))

    stored = _messages(channel_id)
    assert [(m.author_type, m.author_name, m.content, m.to_humans) for m in stored] == [
        ("agent", "Writer", "Here is the draft, Alice.", True),
        ("external", "Guest", "hello from the Band app", True),
        ("agent", "Writer", "fallback reply", True),
    ]
    with db() as session:
        assert session.get(ChannelMember, (channel_id, "agent", writer_id)) is not None


def test_failed_model_selection_removes_the_new_agent(svc: AppService, monkeypatch):
    import app.api.http as http
    from app.providers.management import create_connection
    from app.providers.models import ProviderConnection, ProviderRevision

    async def noop(*_args, **_kwargs):
        return None

    removed: list[str] = []

    async def delete_agent(agent_id: str) -> None:
        removed.append(agent_id)

    def reject_model(*_args, **_kwargs):
        raise ValueError("Choose a verified model")

    monkeypatch.setattr(http, "service", svc)
    monkeypatch.setattr(svc, "startup", noop)
    monkeypatch.setattr(svc, "shutdown", noop)
    monkeypatch.setattr(svc.org, "delete_agent", delete_agent)
    monkeypatch.setattr("app.providers.display.set_agent_model", reject_model)

    with TestClient(http.app) as client:
        signed = client.post("/api/auth/signup", json={"username": "ada", "display_name": "Ada", "password": "secret123"})
        assert signed.status_code == 200, signed.text
        headers = {"Authorization": f"Bearer {signed.json()['token']}"}
        user_id = signed.json()["user"]["id"]
        with db() as session:
            row = create_connection(session, provider="openai", label="Offline model", key="offline-secret",
                                    config={"models": ["test-model"]}, user_id=user_id)
            revision = session.get(ProviderRevision, (row.id, 1))
            revision.verification = {"passed": True, "models": {"test-model": {"tools": True, "vision": True}}}
            session.add(revision)
            session.commit()
            ref = {"connection_id": row.id, "revision": 1, "provider": "openai", "model": "test-model"}
        created = client.post("/api/agents", json={"name": "Tempest", "role": "writer", "model_ref": ref}, headers=headers)
        assert created.status_code == 422, created.text
        assert "verified" in created.json()["detail"]
        with db() as session:
            assert session.exec(select(AgentProfile).where(AgentProfile.name == "Tempest")).first() is None
            session.delete(session.get(ProviderRevision, (ref["connection_id"], 1)))
            session.delete(session.get(ProviderConnection, ref["connection_id"]))
            session.commit()
    assert removed == ["agent-tempest"]


def test_direct_messages_are_private_rooms_the_agent_answers_without_a_mention(svc: AppService):
    async def scenario():
        await svc._ensure_master()
        user = _user()
        master = svc.master()
        scout = await svc.create_agent(name="Scout", role="researcher", instructions="", model=master.model,
                                       channel_id=None, created_by=user.id)
        dm = await svc.open_dm(user, scout)
        assert await svc.open_dm(user, scout) == dm
        assert svc.org.rooms == ["Alice + Scout"]
        assert dm["is_dm"] and dm["dm_agent_id"] == scout.id
        assert dm["human_ids"] == [user.id] and dm["agent_ids"] == [scout.id]
        assert svc.org.participants[dm["band_room_id"]] == {svc.org.owner.id, scout.band_agent_id}

        await svc._ensure_master()
        with db() as session:
            assert session.get(ChannelMember, (dm["id"], "agent", master.id)) is None

        posted = await svc.post_human_message(user, dm["id"], "what changed this week? ask @Master too")
        assert posted["mentions"] == [{"type": "agent", "id": scout.id, "name": "Scout"}]
        with db() as session:
            task = session.exec(select(AgentTask).where(AgentTask.channel_id == dm["id"])).one()
            assert task.agent_id == scout.id
            assert session.get(ChannelMember, (dm["id"], "agent", master.id)) is None

        await svc.delete_agent(scout)
        with db() as session:
            assert session.get(DirectMessage, dm["id"]).agent_id is None
        await svc.post_human_message(user, dm["id"], "anyone there?")
        with db() as session:
            assert len(session.exec(select(AgentTask).where(AgentTask.channel_id == dm["id"])).all()) == 1
    asyncio.run(scenario())
