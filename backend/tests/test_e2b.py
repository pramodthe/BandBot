"""Offline E2B adapter and private desktop proxy regression tests."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from e2b import NotFoundException
from fastapi import HTTPException, WebSocketDisconnect
from fastapi.testclient import TestClient

import app.computer.e2b as driver
import app.computer.viewer as viewer
from app.auth import encrypt_secret
from app.computer import release_control, save_sandbox, take_control
from app.main import app
from app.persistence.models import ChannelMember, ChannelPolicy, ComputerConnection, DirectMessage, db



def test_e2b_create_is_private_internet_enabled_and_pauses_on_timeout(monkeypatch):
    sdk = SimpleNamespace(sandbox_id="vm", _envd_api=SimpleNamespace(aclose=AsyncMock()), kill=AsyncMock())
    create = AsyncMock(return_value=sdk)
    monkeypatch.setattr(driver.AsyncSandbox, "create", create)
    async def scenario():
        client = driver.E2BDesktopClient()
        await client.create(driver.DesktopOptions("name", "desktop", {"agent": "alice"}, 1200))
        kwargs = create.call_args.kwargs
        assert kwargs["network"] == {"allow_public_traffic": False, "mask_request_host": "localhost:${PORT}"}
        assert kwargs["allow_internet_access"] is True
        assert kwargs["lifecycle"] == {"on_timeout": "pause", "auto_resume": False}
        assert kwargs["envs"] == {"DISPLAY": ":0"}
        assert kwargs["metadata"] == {"agent": "alice", "preview_host": "localhost", "computer_name": "name"}
        await client.close()
        sdk._envd_api.aclose.assert_awaited_once()
        sdk.kill.assert_not_awaited()
    asyncio.run(scenario())


def test_e2b_metadata_lookup_does_not_resume_or_attach_before_ownership_check(monkeypatch):
    get_info = AsyncMock(return_value=SimpleNamespace(sandbox_id="foreign", metadata={"agent": "other"}, state="paused"))
    connect = AsyncMock()
    monkeypatch.setattr(driver.AsyncSandbox, "get_info", get_info)
    monkeypatch.setattr(driver.AsyncSandbox, "connect", connect)
    async def scenario():
        client = driver.E2BDesktopClient()
        session = await client.get("foreign")
        assert session.labels == {"agent": "other"} and session.state == "stopped"
        connect.assert_not_awaited()
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("count,has_next", [(0, False), (2, False), (1, True)])
def test_e2b_name_recovery_refuses_missing_or_ambiguous_computers(monkeypatch, count, has_next):
    pages = SimpleNamespace(next_items=AsyncMock(return_value=[object()] * count), has_next=has_next)
    monkeypatch.setattr(driver.AsyncSandbox, "list", lambda *args, **kwargs: pages)
    async def scenario():
        with pytest.raises(NotFoundException if count == 0 else RuntimeError):
            await driver.E2BDesktopClient().get("band-e2b-owned-name")
    asyncio.run(scenario())


def test_e2b_pause_preserves_vm_and_typed_text_is_shell_quoted(monkeypatch):
    pause = AsyncMock()
    monkeypatch.setattr(driver.AsyncSandbox, "pause", pause)
    async def scenario():
        session = driver.DesktopSession(driver.E2BDesktopClient(), "vm", {}, "running")
        session.checked = AsyncMock()
        await session.type("hello'; $(echo unsafe)\n--arg")
        import shlex
        command = shlex.split(session.checked.call_args.args[0])
        assert command[-2:] == ["--", "hello'; $(echo unsafe)\n--arg"]
        await session.stop()
        assert pause.call_args.args == ("vm",) and session.state == "stopped"
    asyncio.run(scenario())


def test_e2b_atomic_overwrite_and_cleanup_on_failure():
    files = SimpleNamespace(write=AsyncMock(), rename=AsyncMock(), remove=AsyncMock())
    sdk = SimpleNamespace(files=files)
    async def scenario():
        session = driver.DesktopSession(driver.E2BDesktopClient(), "vm", {}, "running", sdk)
        await session.write_file(b"new contents", "/tmp/existing.txt")
        temporary = files.write.call_args.args[0]
        assert temporary.startswith("/tmp/.band-write-") and temporary != "/tmp/existing.txt"
        files.rename.assert_awaited_once_with(temporary, "/tmp/existing.txt", user="user", request_timeout=30)
        files.remove.assert_not_awaited()
        files.rename.side_effect = RuntimeError("permission denied")
        with pytest.raises(RuntimeError):
            await session.write_file(b"failed replacement", "/tmp/existing.txt")
        files.remove.assert_awaited_once()
        assert files.remove.call_args.args[0] == files.write.call_args.args[0]
    asyncio.run(scenario())


def test_e2b_download_is_bounded_even_when_file_grows_after_stat():
    class Stream:
        closed = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            self.closed = True
        async def __aiter__(self):
            for _ in range(26):
                yield b"x" * (1024 * 1024)
    stream = Stream()
    sdk = SimpleNamespace(files=SimpleNamespace(read=AsyncMock(return_value=stream)))
    async def scenario():
        session = driver.DesktopSession(driver.E2BDesktopClient(), "vm", {}, "running", sdk)
        with pytest.raises(RuntimeError, match="exceeds limit"):
            await session.read_file("/tmp/growing.txt")
        assert stream.closed
    asyncio.run(scenario())


@pytest.fixture
def private_view(desktop_job):
    task, _, user, computer, _, sandbox = desktop_job
    save_sandbox(computer.key, sandbox_id=sandbox.id, status="running")
    with db() as session:
        session.add(ComputerConnection(sandbox_id=sandbox.id, host="6080-test.e2b.app",
                                      access_token_enc=encrypt_secret("private-traffic-token")))
        session.commit()
    url = viewer.signed_viewer_url(task.id, user.id, sandbox.id)
    token = urlsplit(url).path.split("/")[4]
    return task, user, computer, sandbox, url, token


def test_viewer_links_are_short_lived_and_do_not_expose_provider_credentials(private_view, monkeypatch):
    *_, url, token = private_view
    assert viewer.resolve_viewer(token)[:2] == ("6081-test.e2b.app", "private-traffic-token")
    assert "private-traffic-token" not in url
    assert url == f"/api/computers/view/{token}/websockify"
    with pytest.raises(HTTPException) as error:
        viewer.resolve_viewer("forged")
    assert error.value.status_code == 403
    monkeypatch.setattr(viewer, "VIEW_SECONDS", -1)
    with pytest.raises(HTTPException) as error:
        viewer.resolve_viewer(token)
    assert error.value.status_code == 403


def test_only_active_takeover_owner_gets_an_interactive_vnc_server(private_view):
    _, user, computer, _, _, token = private_view
    assert viewer.resolve_viewer(token)[0].startswith("6081-")
    take_control(computer.key, user_id=user.id, user_name=user.display_name)
    assert viewer.resolve_viewer(token)[0].startswith("6080-")
    take_control(computer.key, user_id=user.id + 1, user_name="Another member")
    assert viewer.resolve_viewer(token)[0].startswith("6081-")
    release_control(computer.key, user_name="Another member")
    assert viewer.resolve_viewer(token)[0].startswith("6081-")


@pytest.mark.parametrize("change,expected", [("membership", 403), ("dm_owner", 403), ("policy", 403), ("paused", 409), ("replaced", 409), ("host", 502)])
def test_viewer_rechecks_current_scope_policy_and_computer(private_view, change, expected):
    task, user, computer, sandbox, _, token = private_view
    with db() as session:
        if change == "membership":
            session.delete(session.get(ChannelMember, (task.channel_id, "human", user.id)))
        elif change == "dm_owner":
            session.add(DirectMessage(channel_id=task.channel_id, user_id=user.id + 1, agent_id=task.agent_id))
        elif change == "policy":
            session.add(ChannelPolicy(channel_id=task.channel_id, computer_control="admins"))
        elif change == "host":
            row = session.get(ComputerConnection, sandbox.id)
            row.host = "localhost.attacker.test"
            session.add(row)
        session.commit()
    if change == "paused":
        save_sandbox(computer.key, status="stopped")
    elif change == "replaced":
        save_sandbox(computer.key, sandbox_id="different-vm")
    with pytest.raises(HTTPException) as error:
        viewer.resolve_viewer(token)
    assert error.value.status_code == expected


def test_viewer_websocket_relays_binary_and_keeps_ingress_token_server_side(private_view, monkeypatch):
    *_, token = private_view
    @asynccontextmanager
    async def upstream(url, **kwargs):
        assert url == "wss://6081-test.e2b.app/websockify"
        assert kwargs["additional_headers"] == {"e2b-traffic-access-token": "private-traffic-token"}
        queue = asyncio.Queue()
        class Socket:
            async def send(self, message):
                await queue.put(message)
            async def __aiter__(self):
                yield b"RFB 003.008\n"
                while True:
                    yield await queue.get()
        yield Socket()
    monkeypatch.setattr(viewer, "connect", upstream)
    with TestClient(app).websocket_connect(f"/api/computers/view/{token}/websockify", subprotocols=["binary"]) as socket:
        assert socket.receive_bytes() == b"RFB 003.008\n"
        socket.send_bytes(b"client-frame")
        assert socket.receive_bytes() == b"client-frame"


def test_interactive_socket_rejects_input_after_control_is_handed_back(private_view, monkeypatch):
    _, user, computer, _, _, token = private_view
    take_control(computer.key, user_id=user.id, user_name=user.display_name)
    forwarded = AsyncMock()
    @asynccontextmanager
    async def upstream(url, **kwargs):
        assert url == "wss://6080-test.e2b.app/websockify"
        class Socket:
            send = forwarded
            async def __aiter__(self):
                yield b"RFB 003.008\n"
                await asyncio.Event().wait()
        yield Socket()
    monkeypatch.setattr(viewer, "connect", upstream)
    with TestClient(app).websocket_connect(f"/api/computers/view/{token}/websockify", subprotocols=["binary"]) as socket:
        assert socket.receive_bytes() == b"RFB 003.008\n"
        release_control(computer.key, user_name=user.display_name)
        socket.send_bytes(b"input-must-not-reach-the-computer")
        with pytest.raises(WebSocketDisconnect):
            socket.receive_bytes()
    forwarded.assert_not_awaited()
