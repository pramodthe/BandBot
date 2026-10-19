"""Persistent E2B desktop computers, one per agent per channel. No application credentials enter the sandbox."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shlex
from datetime import timedelta, timezone
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, uuid4, uuid5

from e2b import NotFoundException, RateLimitException, SandboxException
from fastapi import HTTPException

from app.computer.e2b import DesktopOptions, E2BDesktopClient

from sqlmodel import select

from app import documents
from app.config import settings
from app.computer.display import display_state, record_step, update_display
from app.persistence.models import (AgentProfile, AgentTask, ComputerControl, ComputerViewLease, Document, TaskSandbox,
                                    db, utcnow)

logger = logging.getLogger(__name__)


class ComputerUnavailable(RuntimeError):
    """An actionable message safe to show to the agent and user."""


VNC_PORT = 6080  # E2B desktop noVNC service, accessible only through the app proxy
TRANSFER_LIMIT = 25 * 1024 * 1024
CONTROL_MINUTES = 15


def active_control(key: str) -> ComputerControl | None:
    with db() as session:
        control = session.get(ComputerControl, key)
    if control is None:
        return None
    until = control.until if control.until.tzinfo else control.until.replace(tzinfo=timezone.utc)
    return control if until > utcnow() else None


def take_control(key: str, *, user_id: int, user_name: str) -> ComputerControl:
    """Pause the agent's input actions; its old screen ids become invalid because a person changes the screen."""
    with db() as session:
        control = session.get(ComputerControl, key) or ComputerControl(key=key, user_id=user_id, user_name=user_name,
                                                                     until=utcnow())
        control.user_id, control.user_name = user_id, user_name
        control.until = utcnow() + timedelta(minutes=CONTROL_MINUTES)
        session.add(control)
        session.commit()
        session.refresh(control)
    if sandbox_record(key):
        save_sandbox(key, frame_id="", last_action=f"{user_name} took control")
    return control


def release_control(key: str, *, user_name: str) -> None:
    with db() as session:
        control = session.get(ComputerControl, key)
        if control:
            session.delete(control)
            session.commit()
    if sandbox_record(key):
        save_sandbox(key, frame_id="", last_action=f"{user_name} handed control back")


def expire_controls() -> list[ComputerControl]:
    """Remove take-overs whose time ran out and return them, so each end is announced once."""
    with db() as session:
        expired = [control for control in session.exec(select(ComputerControl)).all()
                   if (control.until if control.until.tzinfo else control.until.replace(tzinfo=timezone.utc)) <= utcnow()]
        for control in expired:
            session.delete(control)
        session.commit()
    for control in expired:
        if sandbox_record(control.key):
            save_sandbox(control.key, frame_id="", last_action=f"{control.user_name}'s take-over ended")
    return expired


def is_quota_error(exc: Exception) -> bool:
    return not isinstance(exc, RateLimitException) and any(
        word in str(exc).lower() for word in ("quota", "storage", "disk limit"))


def workspace_id() -> str:
    return hashlib.sha256(settings.app_secret.encode()).hexdigest()[:16]


def computer_owner(task: AgentTask) -> str:
    """The agent's Band identity. Unlike SQLite row ids, it is never reused after an agent is deleted."""
    with db() as session:
        agent = session.get(AgentProfile, task.agent_id)
    return agent.band_agent_id if agent else f"deleted-agent-{task.agent_id}"


def computer_key(task: AgentTask) -> str:
    """Computers persist per agent and channel, matching memory scope; files never cross channels.

    The worker runs one task per agent per channel at a time, so a computer has one user at once.
    Stored in TaskSandbox.task_id, which predates persistent computers.
    """
    return f"e2b-agent-{computer_owner(task)}-channel-{task.channel_id}"


def sandbox_record(key: str) -> TaskSandbox | None:
    with db() as session:
        return session.get(TaskSandbox, key)


def save_sandbox(key: str, **values) -> TaskSandbox:
    with db() as session:
        row = session.get(TaskSandbox, key)
        name = values.pop("name", f"band-task-{key}")
        if row is None:
            row = TaskSandbox(task_id=key, name=name)
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


class TaskComputer:
    def __init__(self, task: AgentTask, *, client_factory=E2BDesktopClient):
        self.task = task
        self.owner = computer_owner(task)
        self.key = computer_key(task)
        self.client_factory = client_factory
        self.notice = ""
        self.client = None
        self.sandbox = None
        self.desktop_started = False
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    def labels(self) -> dict[str, str]:
        return {"app": "band-slack", "workspace": workspace_id(),
                "agent": self.owner, "channel": str(self.task.channel_id)}

    def sandbox_name(self) -> str:
        owner = hashlib.sha256(self.owner.encode()).hexdigest()[:12]
        return f"band-e2b-{workspace_id()[:8]}-{owner}-c{self.task.channel_id}"

    async def ensure(self, *, desktop: bool = False, create: bool = True):
        """Caller holds lock. Persist a deterministic name before allocating cloud resources.

        People viewing a computer pass create=False: looking must never allocate a new one.
        """
        if not settings.e2b_api_key:
            raise ComputerUnavailable("E2B is not configured. Set E2B_API_KEY to enable the computer.")
        if self.sandbox is None:
            if self.client is None:
                self.client = self.client_factory()
            record = sandbox_record(self.key)
            if record is None and not create:
                raise ComputerUnavailable("This agent has no computer in this channel yet. "
                                          "It starts one when it first needs desktop or code tools.")
            record = record or save_sandbox(self.key, name=self.sandbox_name())
            try:
                sandbox = await self.client.get(record.sandbox_id or record.name, request_timeout=30)
            except NotFoundException:
                if not create:
                    raise ComputerUnavailable("This agent's computer in this channel has expired. "
                                              "It starts a fresh one when it next needs desktop or code tools.") from None
                if record.sandbox_id:
                    # The old computer was removed. Replace it, and tell the agent its files are gone.
                    record = save_sandbox(self.key, sandbox_id=None, status="provisioning", frame_id="",
                                          latest_image="", error=None)
                    update_display(self.key, steps=[], url="", network_warning="", observed_at="")
                    self.notice = ("This agent's previous computer in this channel had expired, so a fresh one "
                                   "was started. Files and logins from earlier tasks are gone.")
                # A cloud timeout after allocation is recovered by the deterministic name on the next attempt.
                try:
                    sandbox = await self.client.create(DesktopOptions(
                        name=record.name, template=settings.e2b_template, labels=self.labels(),
                        timeout=settings.e2b_timeout), timeout=60)
                except SandboxException as exc:
                    if is_quota_error(exc):
                        raise ComputerUnavailable(
                            "E2B storage or quota limit reached, so a new computer could not be created. "
                            "Check sandbox limits and usage in the E2B dashboard.") from None
                    raise
            if any((sandbox.labels or {}).get(key) != value for key, value in self.labels().items()):
                raise ComputerUnavailable("Sandbox ownership did not match this agent and channel. Refusing to attach.")
            # Save identity before start; cancellation/restart can reattach without recreating it.
            save_sandbox(self.key, sandbox_id=sandbox.id, status="starting", error=None, frame_id="")
            self.sandbox = sandbox
            state = str(getattr(sandbox.state, "value", sandbox.state))
            if state != "started":
                await sandbox.start(timeout=60)
            save_sandbox(self.key, status="running")
        if desktop and not self.desktop_started:
            await self.sandbox.computer_use.start(request_timeout=60)
            info = await self.sandbox.computer_use.display.get_info(request_timeout=15)
            display = next((d for d in info.displays or [] if d.is_active), None)
            if display is None and info.displays:
                display = info.displays[0]
            if display is None or not display.width or not display.height:
                raise ComputerUnavailable("Desktop has no display. Use the E2B desktop template or a compatible template.")
            save_sandbox(self.key, width=display.width, height=display.height)
            self.desktop_started = True
        return self.sandbox

    async def _capture(self, action: str, *, target: str = "", x: float | None = None,
                       y: float | None = None) -> list[dict]:
        shot = await self.sandbox.computer_use.screenshot.take_compressed(
            request_timeout=20)
        encoded = shot.screenshot or ""
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        if not encoded or len(encoded) > 7_000_000:
            raise ComputerUnavailable("Desktop screenshot is empty or too large.")
        try:
            base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ComputerUnavailable("E2B returned an invalid screenshot.") from None
        frame = uuid4().hex
        row = save_sandbox(self.key, latest_image=f"data:image/jpeg;base64,{encoded}",
                           frame_id=frame, last_action=action, error=None)
        record_step(self.key, action, target or "Observed the screen", x=x, y=y)
        update_display(self.key, observed_at=row.updated_at.isoformat())
        display = display_state(self.key)
        return [
            {"type": "text", "text": json.dumps({"screen_id": frame, "width": row.width,
                "height": row.height, "action": action,
                "browser_network_warning": display.network_warning or None,
                "instruction": "Use this screen_id for the next action. Coordinates refer to the full image."})},
            {"type": "image_url", "image_url": {"url": row.latest_image}},
        ]

    def _check_control(self) -> None:
        control = active_control(self.key)
        if control:
            raise ComputerUnavailable(
                f"{control.user_name} is using this computer (until {control.until:%H:%M} UTC). Do not act on it now; "
                "wait, ask them in your reply, or take a screenshot to watch.")

    async def screenshot(self) -> list[dict]:
        async with self.lock:
            await self.ensure(desktop=True)
            return await self._capture("screenshot")

    async def action(self, *, screen_id: str, action: str, x: int = 0, y: int = 0,
                     end_x: int = 0, end_y: int = 0, text: str = "", key: str = "",
                     modifiers: list[str] | None = None, button: str = "left", double: bool = False,
                     direction: str = "down", amount: int = 3, seconds: float = 1) -> list[dict]:
        async with self.lock:
            self._check_control()
            await self.ensure(desktop=True)
            row = sandbox_record(self.key)
            if not screen_id or screen_id != row.frame_id:
                raise ComputerUnavailable("Screen changed or this action was already attempted. Take a new screenshot before acting.")
            if action in {"click", "move", "scroll", "drag"}:
                if not (0 <= x < row.width and 0 <= y < row.height):
                    raise ComputerUnavailable("Coordinates are outside the screenshot.")
            if action == "drag" and not (0 <= end_x < row.width and 0 <= end_y < row.height):
                raise ComputerUnavailable("Drag destination is outside the screenshot.")
            if action not in {"click", "move", "drag", "scroll", "type", "key", "wait"}:
                raise ComputerUnavailable("Unsupported computer action")
            # Consume the frame before the remote side effect. A replay after failure cannot repeat a click/type.
            save_sandbox(self.key, frame_id="", last_action=f"Attempting {action}")
            cu = self.sandbox.computer_use
            if action == "click":
                await cu.mouse.click(x, y, button, double, request_timeout=20)
            elif action == "move":
                await cu.mouse.move(x, y, request_timeout=20)
            elif action == "drag":
                await cu.mouse.drag(x, y, end_x, end_y, button, request_timeout=20)
            elif action == "scroll":
                await cu.mouse.scroll(x, y, direction, amount, request_timeout=20)
            elif action == "type":
                await cu.keyboard.type(text, request_timeout=20)
            elif action == "key":
                await cu.keyboard.press(key, modifiers, request_timeout=20)
            elif action == "wait":
                await asyncio.sleep(seconds)
            await asyncio.sleep(0.25)
            positioned = action in {"click", "move", "scroll", "drag"}
            target = {"click": "Clicked the screen", "move": "Moved the pointer", "drag": "Dragged on the screen",
                      "scroll": f"Scrolled {direction}", "type": "Typed text", "key": "Pressed a key",
                      "wait": "Waited for the screen"}[action]
            return await self._capture(action, target=target,
                x=(end_x if action == "drag" else x) / row.width * 100 if positioned else None,
                y=(end_y if action == "drag" else y) / row.height * 100 if positioned else None)

    async def open_browser(self, url: str) -> list[dict]:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or len(url) > 4000:
            raise ComputerUnavailable("Use an http:// or https:// browser URL")
        async with self.lock:
            self._check_control()
            sandbox = await self.ensure(desktop=True)
            save_sandbox(self.key, frame_id="", last_action="Opening browser")
            command = (
                "export DISPLAY=${DISPLAY:-:0}; "
                "browser=$(command -v chromium || command -v chromium-browser || command -v google-chrome || true); "
                'if [ -n "$browser" ]; then nohup "$browser" --no-sandbox --no-first-run --disable-quic '
                "--disable-dev-shm-usage --user-data-dir=$HOME/.config/band-browser " + shlex.quote(url) +
                " >/tmp/band-browser.log 2>&1 </dev/null & "
                "elif command -v firefox >/dev/null; then nohup firefox " + shlex.quote(url) +
                " >/tmp/band-browser.log 2>&1 </dev/null & "
                "else echo 'No browser installed in this template'; exit 1; fi"
            )
            result = await sandbox.process.exec(command, timeout=15)
            if result.exit_code != 0:
                raise ComputerUnavailable("No supported browser could be launched. Use a desktop template with Chromium or Firefox.")
            # Keep only the host in presentation metadata; URLs can contain passwords and signed query parameters.
            update_display(self.key, url=parsed.hostname, network_warning="")
            await asyncio.sleep(1)
            return await self._capture("open_browser", target=f"Opened {parsed.hostname}")

    async def command(self, command: str, *, python: bool = False) -> dict:
        if len(command) > 30000:
            raise ComputerUnavailable("Code or command exceeds 30000 characters")
        # The wrapper runs remotely, not on the host. Bound stdout/stderr before SDK collection.
        argv = ["python3", "-c", command] if python else ["/bin/bash", "-lc", command]
        wrapper = f"""
import json, os, signal, subprocess, threading
argv = {argv!r}
p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
output = {{"stdout": bytearray(), "stderr": bytearray()}}
def drain(stream, name):
    while True:
        block = stream.read(4096)
        if not block:
            return
        remaining = max(0, 12000 - len(output[name]))
        output[name].extend(block[:remaining])
threads = [threading.Thread(target=drain, args=(p.stdout, "stdout"), daemon=True),
           threading.Thread(target=drain, args=(p.stderr, "stderr"), daemon=True)]
for thread in threads:
    thread.start()
timed_out = False
try:
    p.wait(timeout=30)
except subprocess.TimeoutExpired:
    timed_out = True
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait()
for thread in threads:
    thread.join(timeout=1)
print(json.dumps({{"exit_code": p.returncode, "timed_out": timed_out,
    "stdout": bytes(output["stdout"]).decode("utf-8", errors="replace"),
    "stderr": bytes(output["stderr"]).decode("utf-8", errors="replace")}}))
"""

        async with self.lock:
            self._check_control()
            sandbox = await self.ensure()
            save_sandbox(self.key, frame_id="", last_action="Running Python" if python else "Running command")
            result = await sandbox.process.exec("python3 -c " + shlex.quote(wrapper), timeout=40)
            if result.exit_code != 0:
                return {"error": "Remote command timed out or failed", "output": (result.result or "")[:12000]}
            return json.loads(result.result)

    async def write_file(self, path: str, content: str) -> dict:
        if not path.startswith("/") or len(path) > 500 or len(content.encode()) > 200000:
            raise ComputerUnavailable("Use an absolute sandbox path and content up to 200000 bytes")
        async with self.lock:
            self._check_control()
            sandbox = await self.ensure()
            save_sandbox(self.key, frame_id="", last_action="Writing file")
            await sandbox.fs.upload_file(content.encode(), path, timeout=30)
            return {"path": path, "bytes": len(content.encode())}

    async def export_file(self, path: str) -> dict:
        if not path.startswith("/") or len(path) > 500:
            raise ComputerUnavailable("Use an absolute sandbox file path")
        media = documents.TYPES.get(PurePosixPath(path).suffix.lower(), "")
        if media and not media.startswith("text/"):
            return await self._export_document(path)
        code = f"import base64;print(base64.b64encode(open({path!r},'rb').read(200001)).decode())"
        async with self.lock:
            sandbox = await self.ensure()
            response = await sandbox.process.exec("python3 -c " + shlex.quote(code), timeout=15)
            if response.exit_code != 0:
                raise ComputerUnavailable("Could not read the sandbox file")
            data = base64.b64decode(response.result.strip(), validate=True)
            if len(data) > 200000:
                raise ComputerUnavailable("Export supports text files up to 200000 bytes")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ComputerUnavailable("Export supports UTF-8 text and DOCX, XLSX, PDF, PNG, JPEG, or WebP files") from None
            with db() as session:
                task = session.get(AgentTask, self.task.id)
                artifacts = [a for a in task.artifacts if a["path"] != path]
                if len(artifacts) >= 20 or sum(len(a["content"]) for a in artifacts) + len(content) > 200000:
                    raise ComputerUnavailable("Task export limit reached (20 files / 200000 characters)")
                task.artifacts = [*artifacts, {"path": path, "content": content}]
                session.add(task)
                session.commit()
            return {"path": path, "bytes": len(data), "download": "Available in task results"}

    async def _export_document(self, path: str) -> dict:
        """Binary deliverables (DOCX, XLSX, PDF, images) become durable channel documents."""
        async with self.lock:
            sandbox = await self.ensure()
            info = await sandbox.fs.get_file_info(path, request_timeout=30)
            if info.is_dir or info.size > documents.LIMIT:
                raise ComputerUnavailable("Export supports document files up to 25 MB")
            data = bytes(await sandbox.fs.download_file(path, 120))
        try:
            name, media = documents.validate_file(PurePosixPath(path).name, data)
        except HTTPException as exc:
            raise ComputerUnavailable(f"Could not export {PurePosixPath(path).name}: {exc.detail}") from None
        # Content-addressed: replays reuse the document, while a re-edited file becomes a new version.
        document_id = uuid5(NAMESPACE_URL, f"band:{self.task.id}:sandbox:{path}:{hashlib.sha256(data).hexdigest()}").hex
        with db() as session:
            existing = session.get(Document, document_id)
        saved = ({"name": existing.name, "url": f"/api/documents/{existing.id}/download"} if existing
                 else documents.store_generated(self.task, document_id, name, media, data))
        if saved is None:
            raise ComputerUnavailable("That exported file was deleted")
        return {"path": path, "bytes": len(data), "name": saved["name"], "download": saved["url"],
                "link": f"[{saved['name']}]({saved['url']})"}

    async def live_view(self, *, minutes: int = CONTROL_MINUTES, user_id: int | None = None) -> dict:
        """A signed noVNC link for people. The token is in the link, so treat it as a short-lived secret."""
        async with self.lock:
            sandbox = await self.ensure(desktop=True, create=False)
            # A worker finishing its task must not stop the desktop a person is watching.
            until = utcnow() + timedelta(minutes=minutes)
            with db() as session:
                session.merge(ComputerViewLease(key=self.key, until=until))
                session.commit()
            signed = await sandbox.create_signed_preview_url(VNC_PORT, expires_in_seconds=minutes * 60,
                                                              task_id=self.task.id, user_id=user_id)
            return {"url": signed.url,
                    "expires_at": until.isoformat()}

    async def list_files(self, path: str) -> list[dict]:
        async with self.lock:
            sandbox = await self.ensure(create=False)
            entries = await sandbox.fs.list_files(path or ".", request_timeout=30)
            return sorted(({"name": e.name, "path": e.path or f"{path.rstrip('/')}/{e.name}", "is_dir": e.is_dir,
                            "size": e.size, "modified_at": e.modified_at} for e in entries),
                          key=lambda e: (not e["is_dir"], e["name"].lower()))

    async def download(self, path: str) -> bytes:
        async with self.lock:
            sandbox = await self.ensure(create=False)
            info = await sandbox.fs.get_file_info(path, request_timeout=30)
            if info.is_dir or info.size > TRANSFER_LIMIT:
                raise ComputerUnavailable("Downloads support files up to 25 MB.")
            # The SDK's download_file takes positional arguments only: (path, timeout_seconds).
            return await sandbox.fs.download_file(path, 120)

    async def upload(self, name: str, content: bytes) -> str:
        """Put a person's file in the agent's uploads folder; returns its path on the computer."""
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", name.replace("\\", "/").rsplit("/", 1)[-1]).strip(" .")[:120]
        if not name or len(content) > TRANSFER_LIMIT:
            raise ComputerUnavailable("Upload a named file of up to 25 MB.")
        async with self.lock:
            sandbox = await self.ensure(create=False)
            try:
                await sandbox.fs.create_folder("uploads", "755", request_timeout=30)
            except SandboxException:
                pass  # Usually it already exists; the upload below reports any real problem.
            await sandbox.fs.upload_file(content, f"uploads/{name}", timeout=120)
            # Relative paths resolve from the working directory; report an absolute path for the agent.
            work_dir = (await sandbox.get_work_dir() or "").rstrip("/")
            return f"{work_dir}/uploads/{name}" if work_dir else f"uploads/{name}"

    async def detach(self) -> None:
        """Release this connection without stopping the computer (people may still be viewing it)."""
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                logger.warning("Could not close E2B client for computer %s", self.key)
        self.client = None
        self.sandbox = None
        self.desktop_started = False

    async def close(self, *, force: bool = False) -> None:
        from app.computer.app_previews import has_active_view
        with db() as session:
            lease = session.get(ComputerViewLease, self.key)
            until = (lease.until if lease.until.tzinfo else lease.until.replace(tzinfo=timezone.utc)) if lease else None
            watching = until is not None and until > utcnow()
            if lease and (force or not watching):
                session.delete(lease)
                session.commit()
        if (watching or has_active_view(self.key)) and not force:
            await self.detach()
            return  # E2B's configured timeout auto-pauses the desktop.
        # Also recover an allocation made just before a worker crash, even if the graph
        # is already complete and never calls a computer tool on its resumed attempt.
        try:
            record = sandbox_record(self.key)
            if self.sandbox is None and record and settings.e2b_api_key and record.status in {
                "running", "starting", "provisioning", "stop_failed"
            }:
                try:
                    if self.client is None:
                        self.client = self.client_factory()
                    sandbox = await self.client.get(record.sandbox_id or record.name, request_timeout=10)
                    if any((sandbox.labels or {}).get(key) != value for key, value in self.labels().items()):
                        raise ComputerUnavailable("Sandbox ownership did not match this agent and channel")
                    self.sandbox = sandbox
                    save_sandbox(self.key, sandbox_id=sandbox.id)
                except NotFoundException:
                    save_sandbox(self.key, status="missing", frame_id="")
                except Exception:
                    logger.warning("Could not recover task computer %s for cleanup", self.task.id)
                    save_sandbox(self.key, status="stop_failed", frame_id="",
                                 error="Cleanup was not confirmed; check E2B dashboard")
            if self.sandbox is not None:
                if has_active_view(self.key) and not force:
                    return  # A viewer connected while cleanup was recovering the SDK client.
                try:
                    state = str(getattr(self.sandbox.state, "value", self.sandbox.state))
                    if state != "stopped":
                        await self.sandbox.stop(timeout=20)
                    save_sandbox(self.key, status="stopped", frame_id="")
                except Exception:
                    logger.warning("Could not stop task computer %s", self.task.id)
                    save_sandbox(self.key, status="stop_failed", frame_id="",
                                 error="Stop was not confirmed; check E2B dashboard")
        finally:
            if self.client is not None:
                try:
                    await self.client.close()
                except Exception:
                    logger.warning("Could not close E2B client for task %s", self.task.id)
                self.client = None
            self.sandbox = None
            self.desktop_started = False
