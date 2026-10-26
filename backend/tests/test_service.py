from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.auth import encrypt_secret
from app.persistence.models import (AgentProfile, AgentTask, ChannelMember, DirectMessage, Message, db)
from app.workspace.service import AppService
from app.persistence.models import UserBandAccount
from app.persistence.models import UserBandConnection, BandPlatformRoom, PlatformTeammate
from helpers import make_alice


def _messages(channel_id: int) -> list[Message]:
    with db() as session:
        return session.exec(select(Message).where(Message.channel_id == channel_id).order_by(Message.id)).all()


def test_channels_and_teammates_use_the_saved_agent_api_connection(svc):
    async def scenario():
        user = make_alice()
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
    user = make_alice()
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
        user = make_alice()
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
