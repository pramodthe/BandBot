"""Shared Tenki identity, real process locking, privacy and lifecycle regressions."""
import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.auth import encrypt_secret, issue_token
from app.computer import (ComputerUnavailable, TaskComputer, computer_key, sandbox_record,
                          take_control, release_control)
from app.computer.identity import shared_key
from app.computer.locking import ComputerLock
from app.computer.prepare import register_access
from app.computer.providers import provider_for
from app.config import settings
from app.main import app
from app.persistence.models import (AgentProfile, AgentTask, Channel, ChannelMember, ComputerDisk,
                                    ComputerScope, ComputerEndpoint, DirectMessage, PrivateConversation,
                                    db, utcnow)
from app.tasks import enqueue

from app.computer.e2b import DesktopOptions
from app.computer.tenki import TenkiDesktopClient, WORKSPACE
from helpers import fake_tenki_sdk, fake_tenki_remote, tenki_config
@pytest.fixture
def shared_jobs(desktop_job, monkeypatch):
    import app.computer.desktop as desktop
    import app.computer.providers as providers
    import app.persistence.models as models
    import app.api.http as http
    task, agent, user, _, client, sandbox = desktop_job
    config = replace(settings, tenki_computer_sharing="workspace", tenki_api_key="offline",
                     tenki_desktop_image="snapshot:offline", computer_provider="tenki")
    for module in (desktop, providers, models, http):
        monkeypatch.setattr(module, "settings", config)
    with db() as session:
        other = AgentProfile(name="Other", band_agent_id=uuid4().hex, band_api_key_enc=encrypt_secret("offline"))
        channel = Channel(name=uuid4().hex)
        session.add_all([other, channel])
        session.commit()
        session.add_all([
            ChannelMember(channel_id=task.channel_id, member_type="agent", member_id=agent.id),
            ChannelMember(channel_id=channel.id, member_type="agent", member_id=other.id),
            ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id),
        ])
        scope = session.get(ComputerScope, f"agent-{agent.band_agent_id}-channel-{task.channel_id}")
        scope.provider = "tenki"
        session.add(scope)
        session.commit()
    first = enqueue(agent_id=agent.id, channel_id=task.channel_id, room_id=task.room_id,
                    source_id=uuid4().hex, prompt="First shared task")
    second = enqueue(agent_id=other.id, channel_id=channel.id, room_id="offline",
                     source_id=uuid4().hex, prompt="Second shared task")
    a, b = [TaskComputer(t, client_factory=lambda: client) for t in (first, second)]
    return first, second, a, b, client, sandbox, user


def test_agents_across_channels_reuse_one_vm_and_disk_identity(shared_jobs):
    first, second, a, b, client, sandbox, _ = shared_jobs
    assert a.key == b.key == shared_key() and a.shared and b.shared
    assert a.labels() == b.labels() == {"app": "band-slack", "workspace": a.key.removeprefix("tenki-workspace-"), "scope": "workspace"}
    assert a.sandbox_name() == b.sandbox_name()
    async def scenario():
        await a.screenshot()
        await b.screenshot()
        client.create.assert_awaited_once()
        assert a.sandbox.id == b.sandbox.id == sandbox.id
        # A finishing task must not pause another agent's running task.
        with db() as session:
            row = session.get(AgentTask, second.id)
            row.status = "running"
            session.add(row)
            session.commit()
        await a.close()
        sandbox.stop.assert_not_awaited()
        with pytest.raises(ComputerUnavailable, match="using the shared computer"):
            await a.close(force=True)
        with db() as session:
            row = session.get(AgentTask, second.id)
            row.status = "completed"
            session.add(row)
            session.commit()
        await b.close()
        sandbox.stop.assert_awaited_once()
    asyncio.run(scenario())


def test_simultaneous_agents_cannot_replay_the_same_desktop_frame(shared_jobs):
    _, _, a, b, _, sandbox, _ = shared_jobs
    async def scenario():
        shot = await a.screenshot()
        frame = json.loads(shot[0]["text"])["screen_id"]
        results = await asyncio.gather(
            a.action(screen_id=frame, action="type", text="first"),
            b.action(screen_id=frame, action="type", text="second"), return_exceptions=True)
        assert sum(isinstance(result, ComputerUnavailable) for result in results) == 1
        assert sandbox.computer_use.keyboard.type.await_count == 1
        await a.detach()
        await b.detach()
    asyncio.run(scenario())


def test_takeover_from_one_channel_blocks_every_agent(shared_jobs):
    first, _, a, b, _, sandbox, user = shared_jobs
    async def scenario():
        take_control(a.key, user_id=user.id, user_name=user.display_name, task_id=first.id)
        for computer in (a, b):
            with pytest.raises(ComputerUnavailable, match="using this computer"):
                await computer.write_file("/home/tenki/workspace/blocked.txt", "blocked")
        sandbox.fs.upload_file.assert_not_awaited()
        release_control(a.key, user_name=user.display_name)
        await b.write_file("/home/tenki/workspace/allowed.txt", "allowed")
        sandbox.fs.upload_file.assert_awaited_once()
        await b.detach()
    asyncio.run(scenario())


def test_another_agents_file_read_does_not_disconnect_live_viewer(shared_jobs, monkeypatch):
    from app.computer import viewer
    first, _, a, b, _, sandbox, user = shared_jobs
    monkeypatch.setattr(viewer, "allowed", lambda *_args: True)
    async def scenario():
        await a.screenshot()
        with db() as session:
            session.add(ComputerEndpoint(key=a.key, port=6081, sandbox_id=sandbox.id,
                expires_at=utcnow() + timedelta(minutes=5), endpoint_enc=encrypt_secret(json.dumps({
                    "origin": "https://shared.example", "secret": "a" * 64}))))
            session.commit()
        token = viewer.signed_viewer_url(first.id, user.id, sandbox.id).split("/")[4]
        assert viewer.resolve_viewer(token)[0] == "shared.example"
        async def reconnect(**_kwargs):
            # This runs during another agent's SDK refresh, while the viewer's
            # periodic authorization check may also run in the API process.
            assert viewer.resolve_viewer(token)[0] == "shared.example"
        sandbox.start.side_effect = reconnect
        await b.list_files("/")
        assert viewer.resolve_viewer(token)[0] == "shared.example"
        await a.detach()
        await b.detach()
    asyncio.run(scenario())


def test_private_conversations_never_bind_to_workspace_computer(shared_jobs):
    first, _, a, _, _, _, user = shared_jobs
    with db() as session:
        dm = Channel(name=uuid4().hex)
        private = Channel(name=uuid4().hex)
        session.add_all([dm, private])
        session.commit()
        session.add(DirectMessage(channel_id=dm.id, agent_id=first.agent_id, user_id=user.id))
        session.add(PrivateConversation(channel_id=private.id, owner_id=user.id))
        session.commit()
    for channel in (dm, private):
        task = enqueue(agent_id=first.agent_id, channel_id=channel.id, room_id="offline",
                       source_id=uuid4().hex, prompt="Private work")
        assert computer_key(task) != a.key
        assert computer_key(task).startswith("tenki-agent-")


@pytest.mark.parametrize("kind", ["dm", "private"])
def test_private_marker_and_task_created_atomically_never_use_shared_computer(shared_jobs, kind):
    first, _, shared, _, _, _, user = shared_jobs
    with db() as session:
        channel = Channel(name=uuid4().hex)
        session.add(channel)
        session.commit()
        marker = (DirectMessage(channel_id=channel.id, agent_id=first.agent_id, user_id=user.id)
                  if kind == "dm" else PrivateConversation(channel_id=channel.id, owner_id=user.id))
        task = AgentTask(id=uuid4().hex, source_key=uuid4().hex, agent_id=first.agent_id,
                         channel_id=channel.id, room_id="offline", prompt="Atomic private task")
        session.add_all([marker, task])
        session.commit()
    assert computer_key(task) != shared.key
    assert computer_key(task).startswith("tenki-agent-")


def test_membership_removal_blocks_shared_computer_attachment(shared_jobs):
    first, _, a, _, client, _, _ = shared_jobs
    with db() as session:
        session.delete(session.get(ChannelMember, (first.channel_id, "agent", first.agent_id)))
        session.commit()
    with pytest.raises(ComputerUnavailable):
        asyncio.run(a.screenshot())
    client.create.assert_not_awaited()


def test_profiles_show_same_shared_computer_and_historical_task_stays_e2b(shared_jobs):
    first, second, a, b, _, _, user = shared_jobs
    async def scenario():
        await a.screenshot()
        await a.detach()
    asyncio.run(scenario())
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer " + issue_token(user.id)}
        for task in (first, second):
            data = client.get(f"/api/agents/{task.agent_id}/computers", headers=auth).json()
            scope = next(s for s in data["computers"] if s["channel_id"] == task.channel_id)
            assert scope["shared"] and scope["status"] == "running"
            detail = client.get(f"/api/tasks/{scope['task_id']}/computer", headers=auth).json()
            assert detail["sandbox_id"] == sandbox_record(a.key).sandbox_id and detail["shared"]
    with db() as session:
        old = session.exec(select(AgentTask).where(AgentTask.agent_id == first.agent_id,
            AgentTask.id != first.id)).first()
    assert provider_for(old) == "e2b" and computer_key(old) != b.key


def test_register_access_creates_only_completed_tasks_and_deduplicates(shared_jobs):
    first, _, a, _, client, _, user = shared_jobs
    registered = register_access(first.agent_id, first.channel_id)[0]
    again = register_access(first.agent_id, first.channel_id)[0]
    assert again.id == registered.id
    assert registered.status == "completed" and registered.delivery_status == "sent"
    assert computer_key(registered) == a.key
    client.create.assert_not_awaited()
    with TestClient(app) as api:
        response = api.get(f"/api/channels/{first.channel_id}/tasks",
            headers={"Authorization": "Bearer " + issue_token(user.id)})
        assert response.status_code == 200
        assert registered.id not in {task["id"] for task in response.json()}


def test_shared_provider_mounts_workspace_owned_volume_without_agent_identity(shared_jobs):
    _, _, a, b, _, _, _ = shared_jobs
    sdk = fake_tenki_sdk()
    sdk.create.return_value = fake_tenki_remote()
    driver = TenkiDesktopClient(config=tenki_config(), sdk=sdk)
    driver.volume = AsyncMock(return_value=SimpleNamespace(id="shared-disk", active_attachments=[]))
    asyncio.run(driver.create(DesktopOptions(a.sandbox_name(), "unused", a.labels(), 1200)))
    driver.volume.assert_awaited_once_with(a.labels()["workspace"] + ":" + a.key)
    options = sdk.create.call_args.kwargs
    assert options["volumes"] == [{"volume_id": "shared-disk", "mount_path": WORKSPACE}]
    assert options["metadata"]["scope"] == "workspace"
    assert "agent" not in options["metadata"] and "channel" not in options["metadata"]
    assert a.key == b.key


def test_removing_one_teammate_keeps_workspace_computer_and_disk(shared_jobs):
    from app.workspace.service import AppService
    from app.computer.desktop import workspace_id
    _, second, a, b, _, _, _ = shared_jobs
    async def scenario():
        await a.screenshot()
        await a.detach()
        with db() as session:
            agent = session.get(AgentProfile, second.agent_id)
            disk_key = workspace_id() + ":" + a.key
            session.add(ComputerDisk(key=disk_key, name=uuid4().hex, volume_id="kept", state="ready"))
            session.commit()
        await AppService().delete_agent(agent)
        with db() as session:
            assert session.get(ComputerDisk, disk_key).state == "ready"
        assert sandbox_record(a.key).status == "running"
        with pytest.raises(ComputerUnavailable):
            await b.screenshot()
        # An existing teammate still attaches to the same workspace computer.
        await a.screenshot()
        await a.detach()
    asyncio.run(scenario())


def test_os_lock_blocks_another_process_and_is_released_on_crash():
    lock = ComputerLock("process-test-" + uuid4().hex, timeout=0)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_bytes(b"0")
    script = """
import sys
handle=open(sys.argv[1],'r+b')
if sys.platform=='win32':
 import msvcrt
 msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
else:
 import fcntl
 fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
print('locked',flush=True)
sys.stdin.read()
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(lock.path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        async def blocked():
            with pytest.raises(ComputerUnavailable, match="busy"):
                async with lock:
                    pytest.fail("A second process acquired the occupied computer")
        asyncio.run(blocked())
        child.kill()
        child.wait(timeout=5)
        async def available():
            async with lock:
                pass
        asyncio.run(available())
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.communicate(timeout=5)


def test_tenki_app_preview_survives_gateway_endpoint_rotation(shared_jobs):
    from app.computer.app_previews import create_lease, publish, resolve
    from app.persistence.models import AppPreview
    first, _, computer, _, _, sandbox, user = shared_jobs
    async def prepare(*, seconds, port):
        with db() as session:
            session.merge(ComputerEndpoint(key=computer.key, port=port, sandbox_id=sandbox.id,
                expires_at=utcnow() + timedelta(seconds=seconds), endpoint_enc=encrypt_secret(json.dumps({
                    "origin": "https://initial.example", "secret": "a" * 64}))))
            session.commit()
    sandbox.prepare_app_connection = prepare
    async def scenario():
        await computer.screenshot()
        detail = await publish(computer, port=3000, title="Shared app", path="/", call_id="rotating-preview")
        await computer.detach()
        return detail
    detail = asyncio.run(scenario())
    with db() as session:
        preview = session.get(AppPreview, detail["id"])
        assert preview.request_host == "localhost:3000"
        row = session.get(ComputerEndpoint, (computer.key, 3000))
        row.endpoint_enc = encrypt_secret(json.dumps({"origin": "https://renewed.example", "secret": "a" * 64}))
        session.add(row)
        session.commit()
    lease = create_lease(preview, user, "http://localhost:8000")
    from urllib.parse import urlsplit
    token = urlsplit(lease["url"]).hostname.split(".")[0]
    assert resolve(token)[2] == "renewed.example"
