"""Offline contract tests for cloud lifecycle, desktop replay protection, and image tools."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from e2b import RateLimitException, SandboxException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select

from app.runtime.tools import tools_for
from app.auth import issue_token
from app.computer import ComputerUnavailable, TaskComputer, sandbox_record, save_sandbox
from app.runtime.harness import build_harness
from app.main import app
from app.persistence.models import AgentTask, Channel, ChannelMember, User, db
from app.tasks import enqueue
from app.tasks.worker import execute
from app.workspace.audit import record_agent
from helpers import ScriptedModel
def frame_id(blocks):
    return json.loads(blocks[0]["text"])["screen_id"]


def test_computer_create_observe_act_and_stop(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        async with computer:
            screenshot = await computer.screenshot()
            assert screenshot[1]["type"] == "image_url"
            original = frame_id(screenshot)
            after = await computer.action(screen_id=original, action="click", x=200, y=100)
            assert frame_id(after) != original
            sandbox.computer_use.mouse.click.assert_awaited_once_with(200, 100, "left", False, request_timeout=20)
            params = client.create.call_args.args[0]
            assert params.timeout > 0 and params.template == "desktop"
            assert sandbox_record(computer.key).sandbox_id == sandbox.id
        assert sandbox_record(computer.key).status == "stopped"
        assert sandbox_record(computer.key).latest_image
        sandbox.stop.assert_awaited_once()
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_screen_replay_and_ambiguous_click_do_not_repeat_input(desktop_job):
    task, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            token = frame_id(await computer.screenshot())
            sandbox.computer_use.mouse.click.side_effect = TimeoutError("response lost")
            with pytest.raises(TimeoutError):
                await computer.action(screen_id=token, action="click", x=20, y=20)
            assert sandbox_record(computer.key).frame_id == ""
            with pytest.raises(ComputerUnavailable, match="already attempted"):
                await computer.action(screen_id=token, action="click", x=20, y=20)
            assert sandbox.computer_use.mouse.click.await_count == 1
    asyncio.run(scenario())


def test_desktop_calls_are_serialized_and_stale_helper_rejected(desktop_job):
    _, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            token = frame_id(await computer.screenshot())
            results = await asyncio.gather(
                computer.action(screen_id=token, action="type", text="first"),
                computer.action(screen_id=token, action="type", text="second"), return_exceptions=True)
            assert sum(isinstance(result, ComputerUnavailable) for result in results) == 1
            assert sandbox.computer_use.keyboard.type.await_count == 1
    asyncio.run(scenario())


def test_restart_reattaches_stopped_computer_without_creation(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        async with computer:
            await computer.screenshot()
        sandbox.state = "stopped"
        second = TaskComputer(task, client_factory=lambda: client)
        async with second:
            await second.screenshot()
        assert client.create.await_count == 1
        sandbox.start.assert_awaited_once_with(timeout=60)
        assert client.get.call_args.args[0] == sandbox.id
    asyncio.run(scenario())


def test_uncertain_create_recovers_by_persisted_name(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    save_sandbox(computer.key, name=computer.sandbox_name())  # Crashed after allocation, before saving the ID.
    sandbox.labels = computer.labels()
    client.allocated = True
    async def scenario():
        async with computer:
            await computer.screenshot()
        client.create.assert_not_awaited()
        assert client.get.call_args.args[0] == computer.sandbox_name()
    asyncio.run(scenario())


def test_expired_computer_is_replaced_with_a_notice_and_foreign_one_refused(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    save_sandbox(computer.key, name=computer.sandbox_name(), sandbox_id="expired", status="stopped")
    run_python = next(t for t in tools_for(task, computer) if t.name == "run_python")
    async def scenario():
        async with computer:
            result = await run_python.ainvoke({"code": "print(2)"})
            assert "previous computer in this channel had expired" in result["notice"]
            assert client.create.await_count == 1
            assert sandbox_record(computer.key).sandbox_id == sandbox.id
            assert "notice" not in await run_python.ainvoke({"code": "print(3)"})  # Reported once.
        sandbox.labels = {**sandbox.labels, "workspace": "someone-else"}
        other = TaskComputer(task, client_factory=lambda: client)
        async with other:
            with pytest.raises(ComputerUnavailable, match="ownership"):
                await other.screenshot()
        assert client.create.await_count == 1
    asyncio.run(scenario())


def test_computer_persists_per_agent_and_channel(desktop_job):
    task, agent, _, computer, client, sandbox = desktop_job
    later = enqueue(agent_id=agent.id, channel_id=task.channel_id, room_id=task.room_id,
                    source_id=f"later-{task.id}", prompt="Use the same computer")
    with db() as session:
        elsewhere_channel = Channel(name=uuid4().hex)
        session.add(elsewhere_channel)
        session.commit()
        session.refresh(elsewhere_channel)
    elsewhere = enqueue(agent_id=agent.id, channel_id=elsewhere_channel.id, room_id=task.room_id,
                        source_id=f"elsewhere-{task.id}", prompt="Different channel")
    async def scenario():
        async with computer:
            await computer.command("echo first")
        sandbox.state = "stopped"
        second = TaskComputer(later, client_factory=lambda: client)
        assert second.key == computer.key
        async with second:
            await second.command("echo second")
        assert client.create.await_count == 1  # Reattached to the same computer, restarted after stop.
        sandbox.start.assert_awaited_once_with(timeout=60)
    asyncio.run(scenario())
    other = TaskComputer(elsewhere, client_factory=lambda: client)
    assert other.key != computer.key and other.sandbox_name() != computer.sandbox_name()
    assert other.labels()["channel"] == str(elsewhere_channel.id)


def test_coordinates_checked_and_commands_run_only_remotely(desktop_job):
    _, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            token = frame_id(await computer.screenshot())
            with pytest.raises(ComputerUnavailable, match="outside"):
                await computer.action(screen_id=token, action="click", x=1300, y=20)
            sandbox.computer_use.mouse.click.assert_not_awaited()
            assert (await computer.command("print(1+1)", python=True))["stdout"] == "2\n"
            command = sandbox.process.exec.call_args.args[0]
            assert "python3 -c" in command and "subprocess.Popen" in command
            assert sandbox.process.exec.call_args.kwargs["timeout"] == 40
    asyncio.run(scenario())


def test_screenshot_tool_returns_image_to_real_deepagents_loop(tmp_path, desktop_job):
    task, profile, _, computer, _, sandbox = desktop_job
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "see-1", "name": "computer_screenshot", "args": {}}]),
        AIMessage(content="Observed the desktop."),
    ])
    async def scenario():
        async with computer:
            async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "images.db")) as saver:
                await saver.setup()
                result = await execute(task, saver, computer=computer, factory=lambda t, p, c, **kwargs:
                    build_harness(t, p, c, model=model, computer=computer))
                assert result == "Observed the desktop."
                graph = build_harness(task, profile, saver, model=model, computer=computer)
                snapshot = await graph.aget_state({"configurable": {"thread_id": task.id}})
                tool_message = next(m for m in snapshot.values["messages"] if getattr(m, "name", "") == "computer_screenshot")
                assert tool_message.content[1]["type"] == "image_url"
        sandbox.stop.assert_awaited_once()
    asyncio.run(scenario())


def test_computer_endpoint_requires_channel_membership(desktop_job):
    task, _, user, computer, _, sandbox = desktop_job
    asyncio.run(computer.screenshot())
    asyncio.run(computer.close())
    with db() as session:
        outsider = User(username=uuid4().hex, display_name="Outsider", password_hash="unused")
        session.add(outsider)
        session.commit()
        session.refresh(outsider)
    url = f"/api/tasks/{task.id}/computer"
    with TestClient(app) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": f"Bearer {issue_token(outsider.id)}"}).status_code == 403
        response = client.get(url, headers={"Authorization": f"Bearer {issue_token(user.id)}"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["sandbox_id"] == sandbox.id
        assert response.json()["image"].startswith("data:image/jpeg;base64,")
        assert "frame_id" not in response.json()


def test_channel_lists_agent_computers_without_screenshots(desktop_job):
    task, agent, user, computer, _, _ = desktop_job
    url = f"/api/channels/{task.channel_id}/computers"
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.get(url, headers=auth).json() == []
        asyncio.run(computer.screenshot())
        record_agent(task, "tool_call", tool="computer_screenshot")
        with db() as session:
            stored = session.get(AgentTask, task.id)
            stored.status = "running"
            session.add(stored)
            session.commit()
        [listed] = client.get(url, headers=auth).json()
    assert listed["agent_id"] == agent.id and listed["task_id"] == task.id
    assert listed["status"] == "running" and listed["agent_working"] is True
    assert listed["computer_working"] is True
    assert listed["last_action"] and "image" not in listed
    asyncio.run(computer.close())


def test_running_text_or_inline_ui_task_does_not_mark_old_computer_live(desktop_job):
    task, agent, user, computer, client, _ = desktop_job
    asyncio.run(_create_computer(computer))
    record_agent(task, "tool_call", tool="computer_open_browser")
    newer = enqueue(agent_id=agent.id, channel_id=task.channel_id, room_id=task.room_id,
                    source_id=uuid4().hex, prompt="Explain binary search with an inline UI")
    with db() as session:
        stored = session.get(AgentTask, newer.id)
        stored.status = "running"
        session.add(stored)
        session.commit()
    for tool in ("read_file", "generate_sandboxed_ui", "code_interpreter"):
        record_agent(newer, "tool_call", tool=tool)
    client.create.reset_mock()
    client.get.reset_mock()
    url = f"/api/channels/{task.channel_id}/computers"
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as http:
        [listed] = http.get(url, headers=auth).json()
        assert listed["agent_working"] is True
        assert listed["computer_working"] is False
        # A helper's computer call on this task counts; a newer queued task must not mask it.
        queued = enqueue(agent_id=agent.id, channel_id=task.channel_id, room_id=task.room_id,
                         source_id=uuid4().hex, prompt="Later queued question")
        record_agent(newer, "tool_call", tool="sandbox_exec", via="helper")
        [listed] = http.get(url, headers=auth).json()
        assert listed["computer_working"] is True and listed["task_id"] == newer.id
        assert listed["task_id"] != queued.id
        with db() as session:
            stored = session.get(AgentTask, newer.id)
            stored.status = "completed"
            session.add(stored)
            session.commit()
        [listed] = http.get(url, headers=auth).json()
        assert listed["computer_working"] is False
    client.create.assert_not_awaited()
    client.get.assert_not_awaited()


def test_browser_launch_and_file_export_survive_final_result(tmp_path, desktop_job):
    task, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            blocks = await computer.open_browser("https://example.com")
            assert blocks[1]["type"] == "image_url"
            browser_command = sandbox.process.exec.call_args_list[0].args[0]
            assert "https://example.com" in browser_command
            assert "nohup" in browser_command and "</dev/null" in browser_command
            assert "--disable-quic" in browser_command
            await computer.write_file("/tmp/report.txt", "Verified result")
            sandbox.fs.upload_file.assert_awaited_once_with(b"Verified result", "/tmp/report.txt", timeout=30)
            sandbox.process.exec.return_value.result = base64.b64encode(b"Verified result").decode()
            exported = await computer.export_file("/tmp/report.txt")
            assert exported["bytes"] == 15
        model = ScriptedModel(responses=[AIMessage(content="Your report is ready.")])
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "exports.db")) as saver:
            await saver.setup()
            result = await execute(task, saver, factory=lambda t, p, c, **kwargs:
                build_harness(t, p, c, model=model, **kwargs))
            assert "ready" in result
        with db() as session:
            saved = session.get(AgentTask, task.id)
            assert saved.artifacts == [{"path": "/tmp/report.txt", "content": "Verified result"}]
    asyncio.run(scenario())


def test_browser_metadata_omits_url_credentials_and_clears_old_network_warning(desktop_job):
    from app.computer.display import display_state, update_display
    _, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        update_display(computer.key, network_warning="Previous provider restriction")
        async with computer:
            await computer.open_browser("https://example.com/path?secret=do-not-store")
            display = display_state(computer.key)
            assert display.url == "example.com" and display.network_warning == ""
            assert "do-not-store" not in json.dumps(display.model_dump())
            assert display.steps[-1]["target"] == "Opened example.com"
    asyncio.run(scenario())


def test_computer_action_trail_persists_positions_without_typed_secrets(desktop_job):
    from app.computer.display import display_state, record_step
    task, _, user, computer, _, _ = desktop_job
    async def scenario():
        async with computer:
            screen = frame_id(await computer.screenshot())
            screen = frame_id(await computer.action(screen_id=screen, action="click", x=640, y=200))
            step = display_state(computer.key).steps[-1]
            assert step["action"] == "click" and step["x"] == 50 and step["y"] == 25
            await computer.action(screen_id=screen, action="type", text="private-password")
            assert "private-password" not in json.dumps(display_state(computer.key).steps)
        for i in range(22):
            record_step(computer.key, "screenshot", f"Observation {i}")
        assert len(display_state(computer.key).steps) == 20
    asyncio.run(scenario())
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        response = client.get(f"/api/tasks/{task.id}/computer", headers=auth)
    assert response.status_code == 200
    assert len(response.json()["steps"]) == 20
    assert response.json()["steps"][-1]["target"] == "Observation 21"


def test_computer_observations_hide_other_users_dm_even_with_stray_membership(desktop_job):
    from app.persistence.models import DirectMessage
    task, agent, user, _, _, _ = desktop_job
    with db() as session:
        other = User(username=uuid4().hex, display_name="Other owner", password_hash="unused")
        session.add(other)
        session.commit()
        session.refresh(other)
        session.add(DirectMessage(channel_id=task.channel_id, agent_id=agent.id, user_id=other.id))
        session.commit()
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.get(f"/api/tasks/{task.id}/computer", headers=auth).status_code == 404


def test_cancellation_stops_desktop_and_closes_sdk(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        ready = asyncio.Event()
        async def task_body():
            async with computer:
                await computer.screenshot()
                ready.set()
                await asyncio.sleep(30)
        running = asyncio.create_task(task_body())
        await ready.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert sandbox_record(computer.key).status == "stopped"
        sandbox.stop.assert_awaited_once()
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_failed_stop_is_visible_without_losing_task_result(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    sandbox.stop.side_effect = TimeoutError("cloud stop unconfirmed")
    async def scenario():
        async with computer:
            await computer.screenshot()
        assert sandbox_record(computer.key).status == "stop_failed"
        assert "not confirmed" in sandbox_record(computer.key).error
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_computer_subagent_can_observe_desktop(tmp_path, desktop_job):
    from pydantic import Field
    task, _, _, computer, _, _ = desktop_job
    class RecordingModel(ScriptedModel):
        observed: list = Field(default_factory=list)
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed.append(list(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "delegate-desktop", "name": "task", "args": {
            "subagent_type": "computer", "description": "Inspect the desktop and report what is visible."}}]),
        AIMessage(content="", tool_calls=[{"id": "observe-desktop", "name": "computer_screenshot", "args": {}}]),
        AIMessage(content="Desktop inspected."),
        AIMessage(content="The computer specialist verified the desktop."),
    ])
    async def scenario():
        async with computer:
            async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "computer-helper.db")) as saver:
                await saver.setup()
                answer = await execute(task, saver, computer=computer, factory=lambda t, p, c, **kwargs:
                    build_harness(t, p, c, model=model, computer=computer))
        assert "specialist verified" in answer
        assert any(getattr(message, "name", None) == "computer_screenshot"
                   and isinstance(message.content, list) and message.content[1]["type"] == "image_url"
                   for turn in model.observed for message in turn)
    asyncio.run(scenario())


def test_disabled_research_allows_browser_work_and_preserves_purchase_approval(tmp_path, desktop_job):
    from app.persistence.models import TaskApproval
    from app.providers.models import TaskServiceBinding
    from app.tasks.worker import process
    from app.tasks import update_task
    task, _, _, computer, _, sandbox = desktop_job
    with db() as session:
        binding = session.get(TaskServiceBinding, task.id)
        binding.bindings = {**binding.bindings, 'research': {'disabled': True}}
        session.add(binding)
        session.commit()

    class BrowserModel(ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.index in (0, 1):
                prompt = '\n'.join(str(message.content) for message in messages if message.type == 'system')
                assert 'A cloud computer is enabled' in prompt
                assert 'browser and network access are independent of the Web research API' in prompt
                assert 'Never use the computer only to read a page' not in prompt
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = BrowserModel(responses=[
        AIMessage(content='', tool_calls=[{'id': 'delegate-browser', 'name': 'task', 'args': {
            'subagent_type': 'computer', 'description': 'Use the enabled browser to inspect the requested seller, then request approval before a purchase.'}}]),
        AIMessage(content='', tool_calls=[{'id': 'browse-seller', 'name': 'computer_open_browser',
            'args': {'url': 'https://example.com'}}]),
        AIMessage(content='', tool_calls=[{'id': 'approve-purchase', 'name': 'request_approval', 'args': {
            'action': 'Purchase the selected phone', 'details': 'Confirm the exact model, seller, total and delivery before checkout.'}}]),
    ])

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'browser-without-research.db')) as saver:
            await saver.setup()
            update_task(task.id, status='running')
            await process(task, saver, executor=lambda t, s: execute(t, s, computer=computer,
                factory=lambda t2, p, c, **kwargs: build_harness(t2, p, c, model=model, computer=computer)))
    asyncio.run(scenario())
    with db() as session:
        assert session.get(AgentTask, task.id).status == 'waiting'
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
        assert approval.status == 'pending' and approval.action == 'Purchase the selected phone'
    assert any('https://example.com' in str(call.args) for call in sandbox.process.exec.await_args_list)
    sandbox.computer_use.mouse.click.assert_not_awaited()


def test_cleanup_recovers_computer_when_graph_already_finished(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    save_sandbox(computer.key, sandbox_id=sandbox.id, status="running")
    client.allocated = True
    sandbox.labels = computer.labels()
    asyncio.run(computer.close())
    sandbox.stop.assert_awaited_once_with(timeout=20)
    sandbox.start.assert_not_awaited()
    assert sandbox_record(computer.key).status == "stopped"


def test_quota_failures_explain_storage_limits(desktop_job):
    _, _, _, computer, client, _ = desktop_job
    client.create.side_effect = SandboxException("Total disk quota exceeded")
    async def scenario():
        async with computer:
            with pytest.raises(ComputerUnavailable, match="storage or quota limit reached"):
                await computer.screenshot()
        client.create.side_effect = RateLimitException("Rate limit exceeded")
        retry = TaskComputer(computer.task, client_factory=lambda: client)
        async with retry:
            with pytest.raises(RateLimitException):  # Rate limits are not storage problems.
                await retry.screenshot()
    asyncio.run(scenario())


def test_live_view_signs_novnc_link_without_creating_a_computer(desktop_job):
    _, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        with pytest.raises(ComputerUnavailable, match="no computer in this channel yet"):
            await computer.live_view()
        client.create.assert_not_awaited()
        async with computer:
            await computer.command("echo setup")  # The agent created its computer earlier.
        view = await computer.live_view()
        await computer.detach()
        assert view["url"] == "https://6080-signedtoken.proxy.example/vnc.html"
        sandbox.create_signed_preview_url.assert_awaited_once_with(6080, expires_in_seconds=900,
                                                                  task_id=computer.task.id, user_id=None)
        sandbox.computer_use.start.assert_awaited()
    asyncio.run(scenario())


def test_live_view_keeps_desktop_running_after_task_until_explicit_stop(desktop_job):
    from app.persistence.models import ComputerViewLease
    task, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        async with computer:
            await computer.command("echo setup")
            await computer.live_view()
        sandbox.stop.assert_not_awaited()
        assert sandbox_record(computer.key).status == "running"
        with db() as session:
            assert session.get(ComputerViewLease, computer.key) is not None
        stop = TaskComputer(task, client_factory=lambda: client)
        await stop.close(force=True)
        sandbox.stop.assert_awaited_once()
        assert sandbox_record(computer.key).status == "stopped"
        with db() as session:
            assert session.get(ComputerViewLease, computer.key) is None
    asyncio.run(scenario())


def test_expired_live_view_does_not_prevent_task_cleanup(desktop_job):
    from datetime import timedelta
    from app.persistence.models import ComputerViewLease, utcnow
    _, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            await computer.command("echo setup")
            await computer.live_view()
            with db() as session:
                session.merge(ComputerViewLease(key=computer.key, until=utcnow() - timedelta(seconds=1)))
                session.commit()
        sandbox.stop.assert_awaited_once()
        with db() as session:
            assert session.get(ComputerViewLease, computer.key) is None
    asyncio.run(scenario())


def test_human_control_pauses_agent_input_but_not_observation(desktop_job):
    from app.computer import active_control, release_control, take_control
    _, _, user, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            token = frame_id(await computer.screenshot())
            take_control(computer.key, user_id=user.id, user_name="Desktop Tester")
            assert sandbox_record(computer.key).frame_id == ""  # The person changes the screen.
            with pytest.raises(ComputerUnavailable, match="Desktop Tester is using this computer"):
                await computer.action(screen_id=token, action="click", x=5, y=5)
            with pytest.raises(ComputerUnavailable, match="is using this computer"):
                await computer.command("rm -rf /tmp/x")
            with pytest.raises(ComputerUnavailable, match="is using this computer"):
                await computer.open_browser("https://example.com")
            await computer.screenshot()  # Watching is still allowed.
            sandbox.computer_use.mouse.click.assert_not_awaited()
            release_control(computer.key, user_name="Desktop Tester")
            assert active_control(computer.key) is None
            fresh = frame_id(await computer.screenshot())
            await computer.action(screen_id=fresh, action="click", x=5, y=5)
            sandbox.computer_use.mouse.click.assert_awaited_once()
    asyncio.run(scenario())


def test_people_can_browse_download_and_upload_files(desktop_job):
    _, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            await computer.command("echo setup")
        entries = await computer.list_files("")
        assert [e["name"] for e in entries] == ["Downloads", "notes.txt"]  # Folders first.
        assert await computer.download("/home/user/notes.txt") == b"hello notes"
        path = await computer.upload("..\\..\\secret plan?.pdf", b"%PDF")
        assert path == "/home/user/uploads/secret plan_.pdf"
        sandbox.fs.upload_file.assert_awaited_with(b"%PDF", "uploads/secret plan_.pdf", timeout=120)
        sandbox.fs.get_file_info.side_effect = lambda path, **kwargs: SimpleNamespace(is_dir=False, size=26 * 1024 * 1024)
        with pytest.raises(ComputerUnavailable, match="25 MB"):
            await computer.download("/home/user/huge.bin")
        with pytest.raises(ComputerUnavailable, match="25 MB"):
            await computer.upload("big.bin", b"x" * (25 * 1024 * 1024 + 1))
        await computer.detach()
    asyncio.run(scenario())


def test_live_computer_api_requires_membership_and_coordinates_control(desktop_job, monkeypatch):
    import app.api.http as main_module
    from app.persistence.models import Message
    task, _, user, computer, client, sandbox = desktop_job
    asyncio.run(_create_computer(computer))
    monkeypatch.setattr(main_module, "computer_factory", lambda t: TaskComputer(t, client_factory=lambda: client))
    with db() as session:
        outsider = User(username=uuid4().hex, display_name="Outsider", password_hash="unused")
        session.add(outsider)
        session.commit()
        session.refresh(outsider)
    member = {"Authorization": f"Bearer {issue_token(user.id)}"}
    base = f"/api/tasks/{task.id}/computer"
    with TestClient(app) as http:
        assert http.post(base + "/live", json={}, headers={"Authorization": f"Bearer {issue_token(outsider.id)}"}).status_code == 403
        view = http.post(base + "/live", json={"control": True}, headers=member)
        assert view.status_code == 200 and view.headers["cache-control"] == "no-store"
        assert view.json()["url"].endswith("/vnc.html") and view.json()["control"]["mine"] is True
        assert http.get(base, headers=member).json()["control"]["user_name"] == "Desktop Tester"
        assert http.delete(base + "/control", headers=member).status_code == 200
        assert http.get(base, headers=member).json()["control"] is None
        assert http.get(base + "/files", headers=member).json()[0]["name"] == "Downloads"
        download = http.get(base + "/files/download", params={"path": "/home/user/notes.txt"}, headers=member)
        assert download.content == b"hello notes" and 'filename="notes.txt"' in download.headers["content-disposition"]
        uploaded = http.post(base + "/files", params={"name": "brief.txt"}, content=b"Read me", headers=member)
        assert uploaded.json()["path"] == "/home/user/uploads/brief.txt"
        with db() as session:
            notes = [m.content for m in session.exec(select(Message).where(Message.channel_id == task.channel_id))]
        assert any("took control" in n for n in notes) and any("handed" in n and "back" in n for n in notes)
        assert any("uploaded brief.txt" in n and "/home/user/uploads/brief.txt" in n for n in notes)
        with db() as session:
            stored = session.get(AgentTask, task.id)
            stored.status = "running"
            session.add(stored)
            session.commit()
        assert http.post(base + "/stop", headers=member).status_code == 409
        with db() as session:
            stored = session.get(AgentTask, task.id)
            stored.status = "completed"
            session.add(stored)
            session.commit()
        sandbox.stop.reset_mock()
        assert http.post(base + "/stop", headers=member).status_code == 200
        sandbox.stop.assert_awaited_once()
        events = http.get(f"/api/channels/{task.channel_id}/audit", params={"task_id": task.id}, headers=member).json()
        assert [e["action"] for e in reversed(events)] == [
            "computer_taken_over", "computer_handed_back", "computer_download", "computer_upload", "computer_stopped"]
        assert {e["actor_name"] for e in events} == {"Desktop Tester"}


async def _create_computer(computer):
    async with computer:
        await computer.command("echo setup")


def test_channel_member_can_use_an_owned_agents_per_channel_computer(desktop_job, monkeypatch):
    import app.api.http as main_module
    from app.persistence.models import BandAgentOwner
    task, agent, member, computer, client, _ = desktop_job
    asyncio.run(_create_computer(computer))
    monkeypatch.setattr(main_module, "computer_factory", lambda current: TaskComputer(current, client_factory=lambda: client))
    with db() as session:
        owner = User(username=uuid4().hex, display_name="Agent owner", password_hash="unused")
        session.add(owner)
        session.commit()
        session.refresh(owner)
        session.add(BandAgentOwner(agent_id=agent.id, user_id=owner.id, band_user_id="band-user"))
        session.commit()
    headers = {"Authorization": f"Bearer {issue_token(member.id)}"}
    base = f"/api/tasks/{task.id}/computer"
    with TestClient(app) as http:
        assert http.get(base + "/files", headers=headers).status_code == 200
        assert http.post(base + "/live", json={}, headers=headers).status_code == 200
        assert http.post(base + "/files", params={"name": "brief.txt"}, content=b"Read me", headers=headers).status_code == 200
        assert http.post(base + "/stop", headers=headers).status_code == 200


def test_agent_profile_discovers_channel_computers_without_starting_them(desktop_job):
    from app.computer import computer_key
    task, agent, user, computer, client, _ = desktop_job
    asyncio.run(_create_computer(computer))
    newer = enqueue(agent_id=agent.id, channel_id=task.channel_id, room_id=task.room_id,
                    source_id=uuid4().hex, prompt="A later task in the same channel")
    with db() as session:
        fresh = Channel(name=f"fresh-{uuid4().hex}")
        session.add(fresh)
        session.commit()
        session.add_all([ChannelMember(channel_id=fresh.id, member_type="human", member_id=user.id),
                         ChannelMember(channel_id=fresh.id, member_type="agent", member_id=agent.id)])
        session.commit()
        fresh_id = fresh.id
    client.create.reset_mock()
    client.get.reset_mock()
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    url = f"/api/agents/{agent.id}/computers"
    with TestClient(app) as http:
        assert http.get(url).status_code == 401
        result = http.get(url, headers=auth)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        rows = {row["channel_id"]: row for row in result.json()["computers"]}
        assert rows[task.channel_id]["task_id"] == newer.id
        assert rows[task.channel_id]["computer_key"] == computer_key(task) == computer_key(newer)
        assert rows[task.channel_id]["status"] == "stopped"
        assert rows[fresh_id]["task_id"] is None and rows[fresh_id]["status"] == "not_started"
        assert "image" not in rows[task.channel_id] and "sandbox_id" not in rows[task.channel_id]
        assert http.get("/api/agents/999999999/computers", headers=auth).status_code == 404
    client.create.assert_not_awaited()
    client.get.assert_not_awaited()


def test_agent_computer_discovery_hides_other_channels_and_private_chats(desktop_job):
    from app.persistence.models import ChannelPolicy, DirectMessage
    task, agent, owner, _, _, _ = desktop_job
    with db() as session:
        reader = User(username=uuid4().hex, display_name="Computer reader", password_hash="unused")
        session.add(reader)
        session.commit()
        session.add(ChannelMember(channel_id=task.channel_id, member_type="human", member_id=reader.id))
        session.add(ChannelPolicy(channel_id=task.channel_id, computer_control="admins"))
        hidden = Channel(name=f"hidden-{uuid4().hex}")
        private = Channel(name=f"private-{uuid4().hex}")
        session.add_all([hidden, private])
        session.commit()
        session.add_all([ChannelMember(channel_id=hidden.id, member_type="agent", member_id=agent.id),
                         ChannelMember(channel_id=private.id, member_type="agent", member_id=agent.id),
                         # Even an inconsistent membership cannot expose another person's direct chat.
                         ChannelMember(channel_id=private.id, member_type="human", member_id=reader.id),
                         DirectMessage(channel_id=private.id, user_id=owner.id, agent_id=agent.id)])
        session.commit()
        auth = {"Authorization": f"Bearer {issue_token(reader.id)}"}
    with TestClient(app) as http:
        rows = http.get(f"/api/agents/{agent.id}/computers", headers=auth).json()["computers"]
        assert [row["channel_id"] for row in rows] == [task.channel_id]
        assert rows[0]["can_control"] is False
        assert http.post(f"/api/tasks/{task.id}/computer/live", headers=auth, json={}).status_code == 403
