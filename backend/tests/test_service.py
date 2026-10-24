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
from app.band.platform import AgentPlatformOrg, Owner
from app.persistence.models import (AgentPersona, AgentProfile, AgentTask, AuditEvent, Budget, Channel, ChannelMember, ChannelPolicy,
                                    DirectMessage, Message, TaskCacheUsage, TaskUsage, User, WorkspaceRole, db, init_db)
from app.config import settings
from app.workspace.service import AppService
from app.persistence.models import UserBandAccount, PrivateConversation, HumanDirectMessage, BandAgentOwner, ChannelInvitation
from app.persistence.models import MessageComponent, ComponentResponse, TaskInput, AgentRuntime, AgentConnection, AgentOAuthAttempt
from app.persistence.models import AppContact, AppContactRequest, BandRoomAccount, BandAgentLink, UserBandConnection, BandPlatformRoom, PlatformTeammate
from app.persistence.models import OutsideAgent
from app.persistence.models import (AuthSession, ClientMessage, ConnectionHealth, ContactBlock, PasswordReset,
                                    RecoveryAddress, WorkerHeartbeat)


class FakeOrg(AgentPlatformOrg):
    """Offline Agent API transport for tests that inspect native room membership."""
    def __init__(self, *, user_id=1, identity="user-owner"):
        super().__init__("band_a_" + identity, user_id=user_id, identity=identity)
        self.owner = Owner(id=identity, name="Pat Owner", handle="pat")
        self._rest = object()
        self.rooms = []
        self.participants = {}

    async def connect(self):
        pass

    async def close(self):
        self._rest = None

    async def create_room(self, title):
        room_id = f"room-{len(self.rooms) + 1}"
        self.rooms.append(title)
        self.participants[room_id] = {self.identity}
        return room_id

    async def add_participant(self, room_id, band_id):
        self.participants.setdefault(room_id, set()).add(band_id)

    async def _members(self, room_id):
        return self.participants.get(room_id, set())

    async def send(self, room_id, content, mentions):
        return f"band-{len(content)}"


@pytest.fixture
def svc(monkeypatch) -> Iterator[AppService]:
    init_db()

    def clear_database() -> None:
        with db() as session:
            for model in (ClientMessage, AuthSession, RecoveryAddress, PasswordReset, ContactBlock, ConnectionHealth,
                          WorkerHeartbeat, AppContact, AppContactRequest, BandRoomAccount, BandAgentLink, UserBandConnection, BandPlatformRoom, PlatformTeammate,
                          ComponentResponse, TaskInput, MessageComponent, AgentRuntime, AgentOAuthAttempt, AgentConnection,
                          ChannelInvitation, HumanDirectMessage, PrivateConversation, BandAgentOwner, UserBandAccount, OutsideAgent,
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


def test_channels_and_teammates_use_the_saved_agent_api_connection(svc):
    async def scenario():
        user = _user()
        with db() as session:
            session.add(UserBandConnection(user_id=user.id, band_agent_id="alice", secret_enc=encrypt_secret("band_a_alice")))
            session.commit()
        channel = await svc.create_channel("launch", "", user)
        scout = await svc.create_agent(name="Scout", role="researcher", instructions="", model="test-model",
                                       channel_id=channel["id"], created_by=user.id)
        with db() as session:
            assert session.get(BandPlatformRoom, channel["id"]).band_agent_id == "alice"
            assert session.get(PlatformTeammate, scout.id).user_id == user.id
            assert session.get(ChannelMember, (channel["id"], "agent", scout.id))
        assert scout.band_api_key_enc == ""
    asyncio.run(scenario())


def test_unreadable_management_credentials_fail_without_a_fallback(svc):
    from app.workspace.service import ServiceError
    user = _user()
    with db() as session:
        session.add(UserBandAccount(user_id=user.id, band_user_id="alice", secret_enc="cannot-decrypt"))
        session.commit()
    with pytest.raises(ServiceError, match="Update your Band user API key"):
        asyncio.run(svc.create_agent(name="Scout", role="researcher", instructions="", model="test-model", created_by=user.id))
    with db() as session:
        assert session.exec(select(AgentProfile)).first() is None


def test_failed_model_selection_removes_the_new_agent(svc: AppService, monkeypatch):
    import app.api.http as http
    from app.providers.management import create_connection
    from app.providers.models import ProviderConnection, ProviderRevision

    async def noop(*_args, **_kwargs):
        return None

    def reject_model(*_args, **_kwargs):
        raise ValueError("Choose a verified model")

    monkeypatch.setattr(http, "service", svc)
    monkeypatch.setattr(svc, "startup", noop)
    monkeypatch.setattr(svc, "shutdown", noop)
    monkeypatch.setattr("app.providers.display.set_agent_model", reject_model)

    with TestClient(http.app) as client:
        signed = client.post("/api/auth/signup", json={"username": "ada", "display_name": "Ada", "password": "secret123", "band_api_key": "band_a_signup-ada"})
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
            assert not session.exec(select(PlatformTeammate)).first()
            assert session.get(UserBandConnection, user_id)
            session.delete(session.get(ProviderRevision, (ref["connection_id"], 1)))
            session.delete(session.get(ProviderConnection, ref["connection_id"]))
            session.commit()


def test_direct_messages_are_private_and_answer_without_a_mention(svc):
    async def scenario():
        user = _user()
        with db() as session:
            session.add(UserBandConnection(user_id=user.id, band_agent_id="alice", secret_enc=encrypt_secret("band_a_alice")))
            session.commit()
        scout = await svc.create_agent(name="Scout", role="researcher", instructions="", model="test-model", created_by=user.id)
        dm = await svc.open_dm(user, scout)
        assert await svc.open_dm(user, scout) == dm
        assert dm["is_dm"] and dm["dm_agent_id"] == scout.id
        assert dm["human_ids"] == [user.id] and dm["agent_ids"] == [scout.id]
        posted = await svc.post_human_message(user, dm["id"], "what changed this week? ask @Master too")
        assert posted["mentions"] == [{"type": "agent", "id": scout.id, "name": "Scout"}]
        await svc.delete_agent(scout)
        with db() as session:
            assert session.get(DirectMessage, dm["id"]).agent_id is None
        await svc.post_human_message(user, dm["id"], "anyone there?")
        with db() as session:
            assert len(session.exec(select(AgentTask).where(AgentTask.channel_id == dm["id"])).all()) == 1
    asyncio.run(scenario())
