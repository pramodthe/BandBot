"""Shared offline test helpers. Fixtures live in conftest.py."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import Field
from sqlmodel import select

from app.auth import encrypt_secret
from app.band.platform import AgentPlatformOrg, BandIdentity
from app.computer.tenki import WORKSPACE
from app.config import settings
from app.persistence.models import AgentProfile, AgentTask, User, db, utcnow
from app.providers.management import create_connection
from app.providers.models import ProviderRevision
from app.runtime.harness import build_harness
from app.tasks import update_task
from app.tasks.worker import execute, process


# Models and worker attempts

class ScriptedModel(BaseChatModel):
    """Run the actual Deep Agents graph and tools without provider credentials."""
    responses: list
    index: int = 0

    @property
    def _llm_type(self):
        return "offline-scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.responses[self.index]
        self.index += 1
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


class RecordingModel(ScriptedModel):
    """Also records each prompt, to prove a resumed graph sees the human decision."""
    seen: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def run_attempt(tmp_path, task, model):
    """One worker attempt with a fresh checkpoint connection, as after a restart."""
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "approvals.db")) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            await process(task, saver, executor=lambda t, s: execute(
                t, s, factory=lambda t2, p, c, **kw: build_harness(t2, p, c, model=model, **kw)))
    asyncio.run(scenario())
    with db() as session:
        return session.get(AgentTask, task.id)


def approval_call(call_id="approve-1"):
    return AIMessage(content="", tool_calls=[{"name": "request_approval", "id": call_id, "args": {
        "action": "Submit the signup form on example.com", "details": "Email: test@example.com"}}])


def isolate_queue(keep: set[str]) -> None:
    """The suite shares one database; park other tests' leftover queued jobs."""
    with db() as session:
        for leftover in session.exec(select(AgentTask).where(AgentTask.status == "queued")).all():
            if leftover.id not in keep:
                leftover.status = "cancelled"
                session.add(leftover)
        session.commit()


# Workspace rows and API calls

def teammate(name_prefix="Writer"):
    key = uuid4().hex[:8]
    with db() as session:
        agent = AgentProfile(name=f"{name_prefix}-{key}", role="writer", band_agent_id=f"band-{key}",
                             band_api_key_enc=encrypt_secret("offline"))
        session.add(agent)
        session.commit()
        session.refresh(agent)
    return agent


def make_alice() -> User:
    with db() as session:
        user = User(username="alice", display_name="Alice", password_hash="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def signup(client: TestClient, username: str, name: str) -> dict:
    response = client.post("/api/auth/signup", json={
        "username": username, "display_name": name, "password": "secret123", "band_api_key": f"band_a_signup-{username}",
    })
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeOrg(AgentPlatformOrg):
    """Offline Agent API transport for tests that inspect native room membership."""
    def __init__(self, *, user_id=1, identity="user-owner"):
        super().__init__("band_a_" + identity, user_id=user_id, identity=identity)
        self.owner = BandIdentity(id=identity, name="Pat Owner", handle="pat")
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


# Documents (used with the `space` fixture)

def upload(space, name="notes.txt", data=b"Project Amber: launch date is 12 December. Budget 1234."):
    response = space.api.post(f"/api/channels/{space.channel.id}/documents", params={"name": name},
                              content=data, headers=space.auth)
    assert response.status_code == 200, response.text
    return response.json()


def publish(space, doc):
    response = space.api.post(f"/api/channels/{space.channel.id}/messages", headers=space.auth,
                              json={"content": "", "attachment_ids": [doc["id"]]})
    assert response.status_code == 200, response.text
    return response.json()


def sdk_mock():
    """An offline OpenAI SDK for document upload, indexing and scanning."""
    sdk = NS()
    sdk.files = NS(create=AsyncMock(side_effect=[NS(id="original"), NS(id="index")]), delete=AsyncMock())
    sdk.vector_stores = NS(create=AsyncMock(return_value=NS(id="vector")), files=NS(
        create_and_poll=AsyncMock(), retrieve=AsyncMock(return_value=NS(status="completed"))), search=AsyncMock())
    sdk.responses = NS(create=AsyncMock(return_value=NS(id="response", output_text="SCANNED INVOICE TOTAL 1234",
        usage=NS(input_tokens=30, output_tokens=10, total_tokens=40), status="completed", output=[])))
    wrapper = NS()

    class Context:
        async def __aenter__(self):
            return sdk

        async def __aexit__(self, *args):
            pass

    wrapper.client = Context
    wrapper.sdk = sdk
    return wrapper


# Provider settings (used with the `provider_scope` fixture)

def connection(session, provider="openai", model="test-model"):
    row = create_connection(session, provider=provider, label="Offline " + uuid4().hex,
        key="offline-secret", config={"models": [model]}, user_id=1)
    revision = session.get(ProviderRevision, (row.id, 1))
    revision.verification = {"passed": True, "models": {model: {"tools": True, "vision": True, "usage": True}}}
    session.add(revision)
    session.flush()
    return {"connection_id": row.id, "revision": 1, "provider": provider, "model": model}


def make_task(session, scope):
    agent, channel = scope
    task = AgentTask(id=uuid4().hex, source_key=uuid4().hex, agent_id=agent.id,
                     channel_id=channel.id, room_id="offline", prompt="Test immutable configuration")
    session.add(task)
    session.flush()
    return task


# Tenki computers

def tenki_config():
    return replace(settings, tenki_api_key="offline", tenki_desktop_image="snapshot:clean")


def fake_tenki_sdk():
    return SimpleNamespace(
        volumes=SimpleNamespace(get=AsyncMock(), list=AsyncMock(return_value=[]), create=AsyncMock(), wait_ready=AsyncMock()),
        get=AsyncMock(), list=AsyncMock(), create=AsyncMock(), close=AsyncMock())


def fake_tenki_remote():
    info = SimpleNamespace(
        metadata={"workspace": "workspace", "agent": "agent", "channel": "1", "data_volume": "vol"},
        timeout_at=utcnow() + timedelta(hours=1),
        volume_mounts=[SimpleNamespace(volume_id="vol", mount_path=WORKSPACE, read_only=False)])
    return SimpleNamespace(
        id="vm", state="PAUSED", name="name", info=info, refresh=AsyncMock(), resume=AsyncMock(),
        wait_ready=AsyncMock(), pause=AsyncMock(), wait_paused=AsyncMock(), extend=AsyncMock(),
        detach=Mock(), _reset_data_plane=AsyncMock(), close=AsyncMock(),
        fs=SimpleNamespace(remove=AsyncMock(), write_stream=AsyncMock()))
