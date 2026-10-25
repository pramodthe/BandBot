from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select

from app.auth import issue_token
from app.main import app
from app.persistence.models import (AgentPluginAccess, AgentProfile, BandAgentOwner, Channel, ChannelMember, DirectMessage,
                                    LocalTeammate, TeammateRequest, User, db)
from app.runtime.tools import tools_for
from app.runtime.harness import build_harness
from app.tasks import enqueue, update_task
from app.tasks.teammates import _request, create_teammate
from app.tasks.worker import execute
from app.workspace.masters import ensure_user_master
from app.workspace.service import AppService
from test_service import svc  # noqa: F401
from test_tasks import ScriptedModel


def _account(label: str):
    with db() as session:
        user = User(username=label, display_name=label, password_hash="unused")
        session.add(user); session.flush()
        master, created = ensure_user_master(session, user)
        assert created
        session.commit()
        return user, master


def test_signup_provisions_private_master_without_band_or_model(svc, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.api.http.service", svc)
    monkeypatch.setattr("app.providers.connections.has_llm", lambda *_args: False)
    monkeypatch.setattr("app.workspace.service.configuration", lambda: ("", [], "test-model"))
    with TestClient(app) as client:
        headers = []
        masters = []
        for name in ("master-a", "master-b"):
            response = client.post("/api/auth/signup", json={
                "username": name, "display_name": name, "password": "test-password",
            })
            assert response.status_code == 200, response.text
            uid = response.json()["user"]["id"]
            headers.append({"Authorization": "Bearer " + response.json()["token"]})
            with db() as session:
                master = session.exec(select(AgentProfile).where(
                    AgentProfile.band_agent_id == f"local-master-{uid}")).one()
                assert master.is_master and master.created_by == uid and not master.band_api_key_enc
                assert session.get(LocalTeammate, master.id).user_id == uid
                assert session.get(BandAgentOwner, master.id).user_id == uid
                assert session.get(AgentPluginAccess, master.id).slugs == []
                masters.append(master.id)
        for own, other, auth in ((masters[0], masters[1], headers[0]), (masters[1], masters[0], headers[1])):
            for _ in range(2):
                boot = client.get("/api/bootstrap", headers=auth)
                assert boot.status_code == 200, boot.text
                visible = {a["id"]: a for a in boot.json()["agents"]}
                assert own in visible and other not in visible
                assert visible[own]["status"]["error"] == "Configure an agent model in Providers & APIs"
            dm = client.post(f"/api/agents/{own}/dm", headers=auth)
            assert dm.status_code == 200, dm.text
            assert dm.json()["agent_ids"] == [own]
            assert client.post(f"/api/agents/{other}/dm", headers=auth).status_code == 403
        with db() as session:
            assert len(session.exec(select(LocalTeammate)).all()) == 2


def test_bootstrap_repairs_existing_account_once(svc):  # noqa: F811
    with db() as session:
        user = User(username="existing", display_name="Existing", password_hash="unused")
        session.add(user); session.commit()
        uid = user.id
    auth = {"Authorization": "Bearer " + issue_token(uid)}
    with TestClient(app) as client:
        first = client.get("/api/bootstrap", headers=auth)
        second = client.get("/api/bootstrap", headers=auth)
        assert first.status_code == second.status_code == 200
        first_master = [a["id"] for a in first.json()["agents"] if a["is_master"]]
        assert len(first_master) == 1
        assert [a["id"] for a in second.json()["agents"] if a["is_master"]] == first_master


def test_master_creation_rolls_back_with_signup_transaction(svc):  # noqa: F811
    with db() as session:
        user = User(username="rollback", display_name="Rollback", password_hash="unused")
        session.add(user); session.flush()
        ensure_user_master(session, user)
        session.rollback()
    with db() as session:
        assert not session.exec(select(AgentProfile)).all()
        assert not session.exec(select(LocalTeammate)).all()


def test_existing_owned_master_keeps_its_identity(svc):  # noqa: F811
    with db() as session:
        user = User(username="legacy-owner", display_name="Legacy", password_hash="unused")
        session.add(user); session.flush()
        old = AgentProfile(name="Original Master", is_master=True, band_agent_id="native-master",
                           band_api_key_enc="preserved", created_by=user.id)
        session.add(old); session.flush()
        session.add(BandAgentOwner(agent_id=old.id, user_id=user.id, band_user_id="owner")); session.commit()
        master, created = ensure_user_master(session, user)
        assert not created and master.id == old.id and master.band_api_key_enc == "preserved"
        assert len(session.exec(select(AgentProfile)).all()) == 1


def test_master_creates_owner_scoped_teammates_from_private_chat(svc: AppService, monkeypatch):  # noqa: F811
    async def no_band(*args, **kwargs):
        raise AssertionError("Local Master setup must not call Band")
    monkeypatch.setattr(svc, "platform_org", no_band)
    monkeypatch.setattr("app.band.accounts.register_agent", no_band)
    first, master_a = _account("alice")
    second, master_b = _account("bob")

    async def scenario():
        for user, master in ((first, master_a), (second, master_b)):
            dm = await svc.open_dm(user, master)
            task = enqueue(agent_id=master.id, channel_id=dm["id"], room_id="",
                           source_id=uuid4().hex, prompt="Create Scout")
            assert "create_teammate" in {tool.name for tool in tools_for(task)}
            outcome = _request(task, name="Scout", role="researcher", instructions="Research cardamom", call_id="make")
            assert isinstance(outcome, int)
            await svc.process_teammate_request(outcome)
            result = await create_teammate(task, name="Scout", role="researcher", instructions="", call_id="make", wait=0)
            assert result["status"] == "created", result
            assert "one-on-one" in result["note"]
            repeat = await create_teammate(task, name="Scout", role="researcher", instructions="", call_id="again", wait=0)
            assert repeat["status"] == "exists", repeat
            with db() as session:
                child = session.exec(select(AgentProfile).where(AgentProfile.created_by_agent == master.id)).one()
                assert session.get(BandAgentOwner, child.id).user_id == user.id
                assert session.get(LocalTeammate, child.id).user_id == user.id
                assert session.get(AgentPluginAccess, child.id).slugs == []
                assert not child.band_api_key_enc
                assert not session.get(ChannelMember, (dm["id"], "agent", child.id))
                assert session.get(DirectMessage, dm["id"]).agent_id == master.id
                assert len(session.exec(select(TeammateRequest).where(TeammateRequest.requested_by_agent_id == master.id)).all()) == 1
            child_dm = await svc.open_dm(user, child)
            assert child_dm["id"] != dm["id"] and child_dm["agent_ids"] == [child.id]
    asyncio.run(scenario())


def test_master_cannot_create_for_a_group_request_or_another_owner(svc):  # noqa: F811
    _, master = _account("alice")
    second, _ = _account("bob")
    with db() as session:
        group = Channel(name="group")
        session.add(group); session.commit()
        channel_id = group.id
    task = enqueue(agent_id=master.id, channel_id=channel_id, room_id="", source_id="group", prompt="Create Scout")
    assert "private conversation" in _request(task, name="Scout", role="researcher", instructions="", call_id="x")["error"]
    with db() as session:
        session.add(DirectMessage(channel_id=channel_id, user_id=second.id, agent_id=master.id)); session.commit()
    assert "private conversation" in _request(task, name="Scout", role="researcher", instructions="", call_id="y")["error"]
    with db() as session:
        assert not session.exec(select(TeammateRequest)).all()


def test_private_master_harness_creates_a_persistent_teammate(tmp_path, svc):  # noqa: F811
    owner, master = _account("coordinator-owner")
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "create_teammate", "id": "create-scout", "args": {
            "name": "Scout", "role": "researcher", "instructions": "Research large cardamom markets"}}]),
        AIMessage(content="Scout is ready. Open its private chat from AI teammates."),
    ])

    async def scenario():
        dm = await svc.open_dm(owner, master)
        task = enqueue(agent_id=master.id, channel_id=dm["id"], room_id="", source_id="setup", prompt="Create Scout")
        watcher = asyncio.create_task(svc._watch_teammate_requests())
        try:
            async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "master-checkpoints.db")) as saver:
                await saver.setup()
                update_task(task.id, status="running")
                return await execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw))
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    assert asyncio.run(scenario()) == "Scout is ready. Open its private chat from AI teammates."
    with db() as session:
        child = session.exec(select(AgentProfile).where(AgentProfile.created_by_agent == master.id)).one()
        assert child.name == "Scout" and session.get(LocalTeammate, child.id).user_id == owner.id
