"""User hosts, distinct displays, cross-user denial, and immutable task authority."""
import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.auth import issue_token
from app.computer import ComputerUnavailable, TaskComputer, sandbox_record, take_control
from app.computer.identity import selected_key
from app.computer.providers import ComputerMissing
from app.config import settings
from app.main import app
from app.persistence.models import (AgentProfile, AgentTask, BandAgentOwner, Channel, ChannelMember,
    ComputerPublishApproval, DirectMessage, Message, TaskApproval, TaskComputerBinding, TaskPrincipal, User, UserDesktop, db)
from app.tasks.queue import await_approval, hand_off
from test_service import svc  # noqa: F401


class Remote:
    def __init__(self, host):
        self.host = host
        self.id, self.labels, self.state = host.id, host.labels, host.state
        self.number = 0
        self.start = AsyncMock()
        self.stop = AsyncMock(side_effect=self.pause)
        self.computer_use = SimpleNamespace(start=AsyncMock(),
            display=SimpleNamespace(get_info=AsyncMock(return_value=SimpleNamespace(displays=[
                SimpleNamespace(width=1280, height=800, is_active=True)]))),
            screenshot=SimpleNamespace(take_compressed=self.screenshot),
            mouse=SimpleNamespace(click=self.click), keyboard=SimpleNamespace(type=self.type))
        self.fs = SimpleNamespace(upload_file=self.write)

    def select_desktop(self, screen):
        self.number = screen.display

    async def screenshot(self, **kwargs):
        return SimpleNamespace(screenshot=base64.b64encode(f"screen-{self.number}".encode()).decode())

    async def click(self, *args, **kwargs):
        self.host.inputs.append((self.number, args))

    async def type(self, *args, **kwargs):
        self.host.inputs.append((self.number, args))

    async def write(self, data, path, **kwargs):
        self.host.files[path] = data

    async def pause(self, **kwargs):
        self.host.state = "stopped"
        self.host.pauses += 1


class Client:
    def __init__(self):
        self.hosts = {}
        self.creations = 0

    async def get(self, reference, **kwargs):
        host = next((host for host in self.hosts.values() if reference in (host.id, host.name)), None)
        if not host:
            raise ComputerMissing("Missing")
        return Remote(host)

    async def create(self, options, **kwargs):
        self.creations += 1
        host = SimpleNamespace(id=uuid4().hex, name=options.name, labels=options.labels,
                               state="started", files={}, inputs=[], pauses=0)
        self.hosts[host.id] = host
        return Remote(host)

    async def close(self):
        pass


@pytest.fixture
def crew(svc, monkeypatch):  # noqa: F811
    import app.computer.desktop as desktop
    import app.computer.providers as providers
    import app.persistence.models as models
    import app.api.http as http
    config = replace(settings, tenki_computer_sharing="user", tenki_api_key="offline",
                     tenki_desktop_image="snapshot:offline", computer_provider="tenki")
    for module in (desktop, providers, models, http):
        monkeypatch.setattr(module, "settings", config)
    from app.providers.models import ServiceSelection
    with db() as session:
        selected = session.get(ServiceSelection, "workspace")
        selected.bindings = {**selected.bindings, "computer": {"legacy": True}}
        session.add(selected)
        users = [User(username=name, display_name=name, password_hash="unused") for name in ("alice", "bob")]
        session.add_all(users); session.flush()
        agents = [AgentProfile(name=name, band_agent_id=uuid4().hex, band_api_key_enc="") for name in ("A1", "A2", "B1")]
        session.add_all(agents); session.flush()
        for agent, user in zip(agents, (users[0], users[0], users[1])):
            session.add(BandAgentOwner(agent_id=agent.id, user_id=user.id, band_user_id=""))
        group = Channel(name="shared"); session.add(group); session.flush()
        for user in users:
            session.add(ChannelMember(channel_id=group.id, member_type="human", member_id=user.id))
        for agent in agents:
            session.add(ChannelMember(channel_id=group.id, member_type="agent", member_id=agent.id))
        session.commit()
    client = Client()

    def task(agent, requester, channel_id=None):
        with db() as session:
            if channel_id is None:
                channel = Channel(name=uuid4().hex); session.add(channel); session.flush()
                session.add_all([DirectMessage(channel_id=channel.id, user_id=requester.id, agent_id=agent.id),
                    ChannelMember(channel_id=channel.id, member_type="human", member_id=requester.id),
                    ChannelMember(channel_id=channel.id, member_type="agent", member_id=agent.id)])
                channel_id = channel.id
            message = Message(channel_id=channel_id, author_type="human", author_id=requester.id,
                              author_name=requester.display_name, content="Use the computer")
            session.add(message); session.flush()
            task = AgentTask(id=uuid4().hex, source_key=f"{agent.id}::app-message-{message.id}",
                             agent_id=agent.id, channel_id=channel_id, room_id="", prompt="Use the computer")
            session.add(task); session.commit()
            return task
    return SimpleNamespace(users=users, agents=agents, group=group, client=client, task=task, config=config)


def controller(crew, task):
    return TaskComputer(task, client_factory=lambda: crew.client)


def test_user_agents_share_host_files_but_not_screens_and_other_user_is_separate(crew):
    a1, a2, b = crew.agents
    first, second, other = [crew.task(agent, user) for agent, user in
                            ((a1, crew.users[0]), (a2, crew.users[0]), (b, crew.users[1]))]
    ca, cb, cc = [controller(crew, task) for task in (first, second, other)]
    assert ca.host_key == cb.host_key != cc.host_key
    assert ca.key != cb.key != cc.key
    async def scenario():
        frame_a = json.loads((await ca.screenshot())[0]["text"])["screen_id"]
        frame_b = json.loads((await cb.screenshot())[0]["text"])["screen_id"]
        await cc.screenshot()
        assert crew.client.creations == 2
        assert ca.sandbox.number != cb.sandbox.number
        await ca.write_file("/home/tenki/workspace/shared.txt", "shared")
        assert cb.sandbox.host.files["/home/tenki/workspace/shared.txt"] == b"shared"
        assert not cc.sandbox.host.files
        with pytest.raises(ComputerUnavailable, match="Screen changed"):
            await cb.action(screen_id=frame_a, action="click", x=1, y=1)
        # A's shell invalidates only its own observation; B's frame survives.
        await cb.action(screen_id=frame_b, action="click", x=1, y=1)
        assert cb.sandbox.host.inputs[-1][0] == cb.sandbox.number
        assert not cc.sandbox.host.inputs
        for computer in (ca, cb, cc): await computer.detach()
    asyncio.run(scenario())


def test_same_agent_reuses_desktop_across_private_and_group_channels(crew):
    agent, user = crew.agents[0], crew.users[0]
    first = crew.task(agent, user)
    group = crew.task(agent, user, crew.group.id)
    assert controller(crew, first).key == controller(crew, group).key


def test_takeover_is_per_desktop_and_finishing_agent_cannot_pause_sibling(crew):
    a = controller(crew, crew.task(crew.agents[0], crew.users[0]))
    b_task = crew.task(crew.agents[1], crew.users[0]); b = controller(crew, b_task)
    async def scenario():
        await a.screenshot(); await b.screenshot()
        with db() as session:
            task = session.get(AgentTask, b_task.id); task.status = "running"; session.add(task); session.commit()
        await a.close()
        assert all(host.pauses == 0 for host in crew.client.hosts.values())
        take_control(a.key, user_id=crew.users[0].id, user_name="Alice")
        with pytest.raises(ComputerUnavailable, match="using this computer"):
            await a.write_file("/blocked", "no")
        await b.write_file("/allowed", "yes")
        await b.close()
        assert all(host.pauses == 0 for host in crew.client.hosts.values())
    asyncio.run(scenario())


def test_another_user_group_request_and_handoff_cannot_use_owner_computer(crew):
    task = crew.task(crew.agents[0], crew.users[1], crew.group.id)
    computer = controller(crew, task)
    with pytest.raises(ComputerUnavailable, match="owner"):
        asyncio.run(computer.screenshot())
    assert crew.client.creations == 0
    outcome = hand_off(task, teammate=crew.agents[1].name, request="Read private files", report_back=False, call_id="hop")
    assert outcome["status"] == "handed_off"
    with db() as session:
        child = session.get(AgentTask, outcome["task_id"])
        assert session.get(TaskPrincipal, child.id).user_id == crew.users[1].id
    with pytest.raises(ComputerUnavailable, match="owner"):
        asyncio.run(controller(crew, child).screenshot())


def test_cross_user_computer_endpoints_and_export_approval_are_denied(crew):
    task = crew.task(crew.agents[0], crew.users[0], crew.group.id)
    owner, other = crew.users
    auth_b = {"Authorization": "Bearer " + issue_token(other.id)}
    auth_a = {"Authorization": "Bearer " + issue_token(owner.id)}
    with TestClient(app) as client:
        for path in (f"/api/tasks/{task.id}/computer", f"/api/tasks/{task.id}/computer/files",
                     f"/api/tasks/{task.id}/computer/files/download?path=/private.txt",
                     f"/api/agents/{task.agent_id}/computers"):
            assert client.get(path, headers=auth_b).status_code == 403
        assert client.post(f"/api/tasks/{task.id}/computer/live", json={"control": True}, headers=auth_b).status_code == 403
        assert client.get(f"/api/channels/{crew.group.id}/computers", headers=auth_a).json() == []
        with db() as session:
            row = session.get(AgentTask, task.id); row.status = "running"; session.add(row); session.commit()
        await_approval(task, [("private-export", {"kind": "private_computer_export", "action": "Publish file"})])
        assert client.post("/api/approvals/private-export", json={"approve": True}, headers=auth_b).status_code == 403
        assert client.post("/api/approvals/private-export", json={"approve": True}, headers=auth_a).status_code == 200


def test_shared_and_channel_computers_stay_available_to_other_members(crew):
    from fastapi import HTTPException
    from app.computer.access import require_computer_owner
    from app.computer.identity import shared_key
    agent, owner, member = crew.agents[0], crew.users[0], crew.users[1]
    task = crew.task(agent, owner, crew.group.id)
    with db() as session:
        stored = session.get(AgentTask, task.id)
        binding = session.get(TaskComputerBinding, task.id)
        assert binding.computer_key.startswith("tenki-desktop-")
        with pytest.raises(HTTPException) as denied:
            require_computer_owner(session, stored, member.id)
        assert denied.value.status_code == 403
        for key, provider in ((shared_key(crew.config), "tenki"),
                              (f"e2b-agent-{agent.band_agent_id}-channel-{crew.group.id}", "e2b")):
            binding.computer_key = key
            binding.provider = provider
            session.add(binding)
            session.commit()
            require_computer_owner(session, stored, member.id)


def test_private_export_owner_can_approve_when_approvers_are_admins(crew):
    from app.persistence.models import ChannelPolicy, WorkspaceRole
    task = crew.task(crew.agents[0], crew.users[0], crew.group.id)
    owner, admin = crew.users
    with db() as session:
        session.add(ChannelPolicy(channel_id=crew.group.id, approvers="admins", computer_control="members"))
        for user_id in (owner.id, admin.id):
            role = session.get(WorkspaceRole, user_id)
            if role:
                session.delete(role)
        session.add(WorkspaceRole(user_id=admin.id, role="admin"))
        row = session.get(AgentTask, task.id)
        row.status = "running"
        session.add(row)
        session.commit()
        assert session.get(WorkspaceRole, owner.id) is None
    await_approval(task, [("private-export-admin", {"kind": "private_computer_export", "action": "Publish file"})])
    owner_headers = {"Authorization": "Bearer " + issue_token(owner.id)}
    admin_headers = {"Authorization": "Bearer " + issue_token(admin.id)}
    with TestClient(app) as client:
        denied = client.post("/api/approvals/private-export-admin", json={"approve": True}, headers=admin_headers)
        assert denied.status_code == 403 and "computer owner" in denied.json()["detail"]
        assert client.post("/api/approvals/private-export-admin", json={"approve": True},
                           headers=owner_headers).status_code == 200


def test_new_defaults_and_desktop_ports_are_bounded(crew):
    from app.computer.desktop_session import setup_script, profile_root
    from app.computer.user_desktop import ports
    assert crew.config.tenki_disk_gib == 1
    script = setup_script(2, "desktop-two")
    assert "Xvfb :2" in script and "-display :2" in script and "-viewonly" in script
    assert profile_root("desktop-one") != profile_root("desktop-two")
    assert ports(1) != ports(2)
    with pytest.raises(ValueError): setup_script(0, "invalid")
