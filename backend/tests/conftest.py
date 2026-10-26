import os
import tempfile
from pathlib import Path

_db = Path(tempfile.mkdtemp()) / "test.db"
if os.environ.get("BAND_USE_ENV_DATABASE") != "1":
    os.environ["DATABASE_URL"] = f"sqlite:///{_db.as_posix()}"
os.environ["DOCUMENTS_PATH"] = str(_db.parent / "documents")
os.environ["AUTH_ATTEMPT_LIMIT"] = "100000"
os.environ["RECOVERY_DELIVERY"] = "memory"
os.environ["BAND_ENV"] = "test"
os.environ["OPENCLAW_GATEWAY_URLS"] = ""
for key in ("BAND_USER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CURSOR_API_KEY", "E2B_API_KEY", "EXA_API_KEY", "COMPOSIO_API_KEY", "MODEL_PRICES", "GOOGLE_API_KEY", "GEMINI_API_KEY", "TAVILY_API_KEY", "PROVIDER_ENDPOINT_ALLOWLIST", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
    os.environ[key] = ""
for key in ("TENKI_API_KEY", "TENKI_AUTH_TOKEN", "TENKI_DESKTOP_IMAGE"):
    os.environ[key] = ""
os.environ["COMPUTER_PROVIDER"] = "e2b"
os.environ["TENKI_COMPUTER_SHARING"] = "agent_channel"

# Configuration is read at import time, so app modules are imported only after the environment above.
import base64
import json
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from e2b import NotFoundException
from fastapi.testclient import TestClient
from sqlmodel import select

import app.computer.desktop as computer_module
import app.workspace.service as service_module
from app import documents
from app.auth import encrypt_secret, issue_token
from app.band.platform import BandIdentity
from app.computer import TaskComputer
from app.main import app
from app.persistence.models import (
    AgentConnection, AgentOAuthAttempt, AgentPersona, AgentPluginAccess, AgentProfile, AgentRuntime, AgentTask,
    AppContact, AppContactRequest, AuditEvent, AuthSession, BandAgentLink, BandAgentOwner, BandPlatformRoom,
    BandRoomAccount, Budget, Channel, ChannelInvitation, ChannelMember, ChannelPolicy, ClientMessage,
    ComponentResponse, ComputerPublishApproval, ConnectionHealth, ContactBlock, DirectMessage, Document,
    HumanDirectMessage, LocalTeammate, Message, MessageComponent, OpenAISettings, OutsideAgent, PasswordReset,
    PlatformTeammate, PrivateConversation, RecoveryAddress, TaskCacheUsage, TaskInput, TaskPrincipal, TaskUsage,
    TeammateRequest, User, UserBandAccount, UserBandConnection, UserDesktop, WorkerHeartbeat, WorkspaceRole, db,
    init_db)
from app.providers.models import ServiceSelection, ServiceSelectionHistory
from app.tasks import enqueue
from app.workspace.service import AppService


@pytest.fixture(autouse=True)
def offline_band_platform(monkeypatch):
    from fake_band_platform import OfflineBandNetwork
    import app.band.platform as transport
    import app.band.connections as connections
    import app.band.accounts as accounts
    network = OfflineBandNetwork()
    monkeypatch.setattr(transport, "AsyncRestClient", network.client)
    monkeypatch.setattr(connections, "AsyncRestClient", network.client)
    monkeypatch.setattr(accounts, "AsyncRestClient", network.management_client)
    monkeypatch.setattr(service_module, "PlatformListener", network.listener)
    return network


@pytest.fixture(autouse=True)
def offline_agent_keys(monkeypatch):
    from app.band.listener import AgentManager
    import app.band.connections as connections

    async def identity(key):
        if not key.startswith("band_a_"):
            raise ValueError("Invalid test Agent API key")
        return BandIdentity(id=key.removeprefix("band_a_"), name="Remote Writer", handle="remote/writer")

    async def start(manager, profile, *, owner_id=""):
        manager._band_ids[profile.id] = profile.band_agent_id
        manager.status[profile.id] = {"state": "online", "error": None}

    monkeypatch.setattr(connections, "fetch_identity", identity)
    monkeypatch.setattr(service_module, "fetch_identity", identity)
    monkeypatch.setattr(AgentManager, "start", start)


@pytest.fixture
def job():
    """A queued task for a Master agent in a channel with one human member."""
    init_db()
    key = uuid4().hex
    with db() as session:
        agent = AgentProfile(name=f"Agent-{key[:8]}", role="master", is_master=True,
                             band_agent_id=key, band_api_key_enc=encrypt_secret("offline"))
        channel = Channel(name=key, band_room_id=key)
        user = User(username=key, display_name="Tester", password_hash="unused")
        session.add_all([agent, channel, user])
        session.commit()
        session.refresh(agent)
        session.refresh(channel)
        session.refresh(user)
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.commit()
    task = enqueue(agent_id=agent.id, channel_id=channel.id, room_id=key,
                   source_id=key, prompt="Create a draft and verify it")
    return task, agent, user


@pytest.fixture
def svc(monkeypatch) -> Iterator[AppService]:
    """A fresh AppService over an emptied workspace (workspace-wide provider settings are kept)."""
    init_db()

    def clear_database() -> None:
        with db() as session:
            for model in (ServiceSelectionHistory, ServiceSelection):
                for row in session.exec(select(model).where(model.scope != "workspace")).all():
                    session.delete(row)
            for model in (UserDesktop, TaskPrincipal, ComputerPublishApproval, AgentPluginAccess, TeammateRequest, ClientMessage, AuthSession, RecoveryAddress, PasswordReset, ContactBlock, ConnectionHealth,
                          WorkerHeartbeat, AppContact, AppContactRequest, BandRoomAccount, BandAgentLink, UserBandConnection, BandPlatformRoom, PlatformTeammate, LocalTeammate,
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


@pytest.fixture
def space(monkeypatch):
    """An API client, admin member, outsider, agent and task in a channel using legacy OpenAI documents."""
    init_db()
    with db() as session:
        selection = session.get(ServiceSelection, "workspace")
        original_bindings = selection.bindings
        selection.bindings = {**selection.bindings, "llm": {"legacy": True, "provider": "openai"}, "retrieval": {"legacy": True, "provider": "openai"},
                              "document_analysis": {"legacy": True, "provider": "openai"}}
        session.add(selection)
        session.commit()
    uid = uuid4().hex
    with db() as session:
        for row in session.exec(select(OpenAISettings)).all():
            session.delete(row)
        user = User(username=uid, display_name="Document tester", password_hash="unused")
        other = User(username=uid + "other", display_name="Other", password_hash="unused")
        agent = AgentProfile(name="DocAgent" + uid[:6], band_agent_id=uid, band_api_key_enc=encrypt_secret("offline"))
        channel = Channel(name=uid)
        session.add_all([user, other, agent, channel])
        session.flush()
        session.add_all([ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id),
                         ChannelMember(channel_id=channel.id, member_type="agent", member_id=agent.id),
                         WorkspaceRole(user_id=user.id, role="admin")])
        task = AgentTask(id=uid, source_key=uid, channel_id=channel.id, agent_id=agent.id, room_id="", prompt="Read files")
        session.add(task)
        session.commit()
    with TestClient(app) as api:
        yield NS(api=api, channel=channel, user=user, other=other, agent=agent, task=task,
                 auth={"Authorization": "Bearer " + issue_token(user.id)},
                 other_auth={"Authorization": "Bearer " + issue_token(other.id)})
    with db() as session:
        for doc in session.exec(select(Document).where(Document.channel_id == channel.id)).all():
            documents.path_for(doc).unlink(missing_ok=True)
            session.delete(doc)
        selection = session.get(ServiceSelection, "workspace")
        selection.bindings = original_bindings
        session.add(selection)
        session.delete(session.get(AgentTask, task.id))
        for row in session.exec(select(OpenAISettings)).all():
            session.delete(row)
        session.commit()


@pytest.fixture
def provider_scope():
    """An agent and channel; workspace provider defaults are restored afterwards."""
    init_db()
    with db() as session:
        selection = session.get(ServiceSelection, "workspace")
        original = deepcopy(selection.bindings)
        agent = AgentProfile(name="Provider test", band_agent_id=uuid4().hex, band_api_key_enc="offline", model="test-model")
        channel = Channel(name=uuid4().hex)
        session.add_all([agent, channel])
        session.commit()
    yield agent, channel
    with db() as session:
        selection = session.get(ServiceSelection, "workspace")
        selection.bindings = original
        session.add(selection)
        for document in session.exec(select(Document).where(Document.channel_id == channel.id)).all():
            document.status = "deleted"
            document.units = []
            documents.path_for(document).unlink(missing_ok=True)
            session.add(document)
        session.commit()


@pytest.fixture
def admin_space(provider_scope):
    """An API client with a workspace admin and a plain member in the provider_scope channel."""
    agent, channel = provider_scope
    with db() as session:
        admin = User(username=uuid4().hex, display_name="Provider admin", password_hash="unused")
        member = User(username=uuid4().hex, display_name="Member", password_hash="unused")
        session.add_all([admin, member])
        session.flush()
        session.add(WorkspaceRole(user_id=admin.id, role="admin"))
        session.add_all([ChannelMember(channel_id=channel.id, member_type="human", member_id=admin.id),
                         ChannelMember(channel_id=channel.id, member_type="agent", member_id=agent.id)])
        session.commit()
    with TestClient(app) as client:
        yield NS(client=client, agent=agent, channel=channel,
            auth={"Authorization": "Bearer " + issue_token(admin.id)},
            member={"Authorization": "Bearer " + issue_token(member.id)})


@pytest.fixture
def desktop_job(monkeypatch):
    """A queued computer task with an offline E2B client; live E2B construction fails the test."""
    init_db()
    with db() as session:
        selection = session.get(ServiceSelection, "workspace")
        previous_computer = selection.bindings.get("computer", {"disabled": True})
        selection.bindings = {**selection.bindings, "computer": {"legacy": True}}
        session.add(selection)
        session.commit()

    def forbid_live_e2b(*args, **kwargs):
        raise AssertionError("Offline tests must not initialize a live E2B client")

    monkeypatch.setattr(computer_module.E2BDesktopClient, "__init__", forbid_live_e2b)
    unique = uuid4().hex
    with db() as session:
        agent = AgentProfile(name=f"Desktop-{unique[:8]}", band_agent_id=unique, role="master", is_master=True,
                             band_api_key_enc=encrypt_secret("offline"))
        channel = Channel(name=unique)
        user = User(username=unique, display_name="Desktop Tester", password_hash="unused")
        session.add_all([agent, channel, user])
        session.commit()
        for row in (agent, channel, user):
            session.refresh(row)
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.commit()
    task = enqueue(agent_id=agent.id, channel_id=channel.id, room_id=unique, source_id=unique,
                   prompt="Use the computer to verify the page")
    monkeypatch.setattr(computer_module, "settings", replace(computer_module.settings, e2b_api_key="offline-test"))
    client = SimpleNamespace()
    screen = base64.b64encode(b"offline-test-image").decode()
    sandbox = SimpleNamespace(id=f"sandbox-{unique}", state="started", labels={},
        computer_use=SimpleNamespace(
            start=AsyncMock(),
            display=SimpleNamespace(get_info=AsyncMock(return_value=SimpleNamespace(displays=[
                SimpleNamespace(width=1280, height=800, is_active=True)]))),
            screenshot=SimpleNamespace(take_compressed=AsyncMock(return_value=SimpleNamespace(screenshot=screen))),
            mouse=SimpleNamespace(click=AsyncMock(), move=AsyncMock(), drag=AsyncMock(), scroll=AsyncMock()),
            keyboard=SimpleNamespace(type=AsyncMock(), press=AsyncMock())),
        fs=SimpleNamespace(
            upload_file=AsyncMock(), create_folder=AsyncMock(),
            list_files=AsyncMock(return_value=[
                SimpleNamespace(name="notes.txt", path="/home/user/notes.txt", is_dir=False, size=12,
                                modified_at="2026-10-16T10:00:00Z"),
                SimpleNamespace(name="Downloads", path="/home/user/Downloads", is_dir=True, size=0,
                                modified_at=None)]),
            get_file_info=AsyncMock(side_effect=lambda path, **kwargs: SimpleNamespace(
                is_dir=False, size=12, path=f"/home/user/{path}" if not path.startswith("/") else path)),
            # Like the real SDK, download_file accepts positional arguments only.
            download_file=AsyncMock(side_effect=lambda *args: b"hello notes")),
        get_work_dir=AsyncMock(return_value="/home/user"),
        create_signed_preview_url=AsyncMock(return_value=SimpleNamespace(
            url="https://6080-signedtoken.proxy.example/vnc.html", token="signedtoken")),
        process=SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(exit_code=0,
                               result=json.dumps({"exit_code": 0, "stdout": "2\n", "stderr": ""})))),
        start=AsyncMock(), stop=AsyncMock())
    client.allocated = False

    async def get(name, **kwargs):
        if not client.allocated:
            raise NotFoundException("not found")
        return sandbox

    async def create(params, **kwargs):
        sandbox.labels = params.labels
        client.allocated = True
        return sandbox

    client.get = AsyncMock(side_effect=get)
    client.create = AsyncMock(side_effect=create)
    client.close = AsyncMock()
    controller = TaskComputer(task, client_factory=lambda: client)
    yield task, agent, user, controller, client, sandbox
    with db() as session:
        selection = session.get(ServiceSelection, "workspace")
        selection.bindings = {**selection.bindings, "computer": previous_computer}
        session.add(selection)
        session.commit()
