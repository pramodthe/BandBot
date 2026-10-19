"""Async E2B desktop driver. All commands execute inside the channel's microVM."""
from __future__ import annotations

import asyncio
import base64
import io
import posixpath
import shlex
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

from e2b import AsyncSandbox, CommandExitException, FileType, NotFoundException, SandboxQuery, SandboxState
from PIL import Image

from app.auth import encrypt_secret
from app.config import settings
from app.persistence.models import ComputerConnection, db


@dataclass
class DesktopOptions:
    name: str
    template: str
    labels: dict[str, str]
    timeout: int


class E2BDesktopClient:
    def __init__(self):
        self.sessions: list[DesktopSession] = []

    def options(self, request_timeout: int = 30) -> dict:
        return {"api_key": settings.e2b_api_key, "request_timeout": request_timeout}

    async def get(self, reference: str, *, request_timeout: int = 30):
        if reference.startswith("band-e2b-"):
            pages = AsyncSandbox.list(SandboxQuery(metadata={"computer_name": reference},
                state=[SandboxState.RUNNING, SandboxState.PAUSED]), **self.options(request_timeout))
            items = await pages.next_items()
            if not items:
                raise NotFoundException("Computer not found")
            if len(items) != 1 or pages.has_next:
                raise RuntimeError("Ambiguous computer identity")
            info = items[0]
        else:
            # Read metadata before resuming anything; the controller checks ownership first.
            info = await AsyncSandbox.get_info(reference, **self.options(request_timeout))
        session = DesktopSession(self, info.sandbox_id, info.metadata or {},
                                 str(getattr(info.state, "value", info.state)))
        self.sessions.append(session)
        return session

    async def create(self, options: DesktopOptions, *, timeout: int = 60):
        labels = {**options.labels, "preview_host": "localhost"}
        sdk = await AsyncSandbox.create(template=options.template, timeout=options.timeout,
            metadata={**labels, "computer_name": options.name}, envs={"DISPLAY": ":0"},
            allow_internet_access=True, network={"allow_public_traffic": False, "mask_request_host": "localhost:${PORT}"},
            lifecycle={"on_timeout": "pause", "auto_resume": False}, **self.options(timeout))
        session = DesktopSession(self, sdk.sandbox_id, labels, "running", sdk)
        self.sessions.append(session)
        return session

    async def close(self):
        for session in self.sessions:
            # E2B's async context manager kills the VM. Close only its HTTP transport here.
            transport = getattr(session.sdk, "_envd_api", None)
            if transport:
                await transport.aclose()
        self.sessions.clear()


class DesktopSession:
    def __init__(self, client, sandbox_id: str, labels: dict, state: str, sdk=None):
        self.client, self.id, self.labels, self.sdk = client, sandbox_id, labels, sdk
        self.state = "started" if state == "running" else "stopped"
        self.process = SimpleNamespace(exec=self.exec)
        self.fs = SimpleNamespace(upload_file=self.write_file, list_files=self.list_files,
            get_file_info=self.file_info, download_file=self.read_file, create_folder=self.make_dir)
        self.computer_use = SimpleNamespace(start=self.desktop,
            display=SimpleNamespace(get_info=self.display_info),
            screenshot=SimpleNamespace(take_compressed=self.screenshot),
            mouse=SimpleNamespace(click=self.click, move=self.move, drag=self.drag, scroll=self.scroll),
            keyboard=SimpleNamespace(type=self.type, press=self.press))

    async def connection(self):
        if self.sdk is None:
            self.sdk = await AsyncSandbox.connect(self.id, timeout=settings.e2b_timeout, **self.client.options())
        return self.sdk

    async def start(self, *, timeout=60):
        await self.connection()
        self.state = "started"

    async def stop(self, *, timeout=20):
        await AsyncSandbox.pause(self.id, **self.client.options(timeout))
        self.state = "stopped"

    async def exec(self, command: str, *, timeout=30):
        sdk = await self.connection()
        try:
            result = await sdk.commands.run(command, timeout=timeout, request_timeout=timeout + 5)
        except CommandExitException as result:
            return SimpleNamespace(exit_code=result.exit_code, result=result.stdout, stderr=result.stderr)
        return SimpleNamespace(exit_code=result.exit_code, result=result.stdout, stderr=result.stderr)

    async def checked(self, command: str, timeout=20):
        result = await self.exec(command, timeout=timeout)
        if result.exit_code:
            raise RuntimeError("Desktop command failed")
        return result.result

    async def desktop(self, **kwargs):
        # The official desktop template contains the applications; initialize its display idempotently.
        await self.checked("pgrep -x Xvfb >/dev/null || (nohup Xvfb :0 -ac -screen 0 1280x800x24 "
            "-nolisten tcp >/tmp/band-display.log 2>&1 </dev/null &)")
        for _ in range(40):
            if not (await self.exec("DISPLAY=:0 xdpyinfo >/dev/null 2>&1", timeout=5)).exit_code:
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Desktop display did not start")
        await self.checked("pgrep -x xfce4-session >/dev/null || (nohup env DISPLAY=:0 startxfce4 "
                           ">/tmp/band-desktop.log 2>&1 </dev/null &)")
        for _ in range(80):
            if not (await self.exec("pgrep -x xfce4-session >/dev/null && pgrep -x xfwm4 >/dev/null "
                                   "&& pgrep -x xfdesktop >/dev/null", timeout=5)).exit_code:
                return
            await asyncio.sleep(0.25)
        raise RuntimeError("Desktop session did not start")

    async def display_info(self, **kwargs):
        output = await self.checked("DISPLAY=:0 xdotool getdisplaygeometry")
        width, height = map(int, output.split())
        return SimpleNamespace(displays=[SimpleNamespace(width=width, height=height, is_active=True)])

    async def screenshot(self, options=None, **kwargs):
        path = "/tmp/band-observation.png"
        await self.checked(f"DISPLAY=:0 scrot --overwrite --pointer {path}")
        sdk = await self.connection()
        data = await sdk.files.read(path, format="bytes", user="user", request_timeout=20)
        if len(data) > 12_000_000:
            raise RuntimeError("Screenshot exceeds limit")
        def jpeg():
            with Image.open(io.BytesIO(data)) as screen:
                buffer = io.BytesIO()
                screen.convert("RGB").save(buffer, format="JPEG", quality=75)
                return base64.b64encode(buffer.getvalue()).decode()
        return SimpleNamespace(screenshot=await asyncio.to_thread(jpeg))

    async def xdo(self, *args):
        await self.checked("DISPLAY=:0 xdotool " + shlex.join([str(arg) for arg in args]))

    async def click(self, x, y, button="left", double=False, **kwargs):
        await self.xdo("mousemove", x, y, "click", "--repeat", 2 if double else 1,
                       {"left": 1, "middle": 2, "right": 3}[button])

    async def move(self, x, y, **kwargs):
        await self.xdo("mousemove", x, y)

    async def drag(self, x, y, end_x, end_y, button="left", **kwargs):
        number = {"left": 1, "middle": 2, "right": 3}[button]
        await self.xdo("mousemove", x, y, "mousedown", number, "mousemove", end_x, end_y, "mouseup", number)

    async def scroll(self, x, y, direction, amount, **kwargs):
        await self.xdo("mousemove", x, y, "click", "--repeat", amount, 4 if direction == "up" else 5)

    async def type(self, text, **kwargs):
        await self.xdo("type", "--clearmodifiers", "--delay", 2, "--", text)

    async def press(self, key, modifiers=None, **kwargs):
        aliases = {"enter": "Return", "escape": "Escape", "esc": "Escape", "backspace": "BackSpace",
                   "tab": "Tab", "space": "space", "delete": "Delete", "page_down": "Next", "page_up": "Prior",
                   "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End"}
        keys = ["super" if m == "cmd" else m for m in modifiers or []]
        await self.xdo("key", "--clearmodifiers", "+".join([*keys, aliases.get(key.lower(), key)]))

    async def get_work_dir(self):
        return (await self.checked("pwd")).strip()

    async def write_file(self, content, path, **kwargs):
        sdk = await self.connection()
        timeout = kwargs.get("timeout", 30)
        # Atomic replacement also avoids envd's failure to reopen user-owned files in /tmp.
        temporary = posixpath.join(posixpath.dirname(path), f".band-write-{uuid4().hex}.tmp")
        try:
            await sdk.files.write(temporary, content, user="user", request_timeout=timeout)
            await sdk.files.rename(temporary, path, user="user", request_timeout=timeout)
        except BaseException:
            try:
                await sdk.files.remove(temporary, user="user", request_timeout=10)
            except Exception:
                pass
            raise

    async def list_files(self, path, **kwargs):
        entries = await (await self.connection()).files.list(path, user="user", request_timeout=30)
        return [SimpleNamespace(name=e.name, path=e.path, is_dir=e.type == FileType.DIR,
                                size=e.size, modified_at=e.modified_time.isoformat()) for e in entries]

    async def file_info(self, path, **kwargs):
        entry = await (await self.connection()).files.get_info(path, user="user", request_timeout=30)
        return SimpleNamespace(is_dir=entry.type == FileType.DIR, size=entry.size)

    async def read_file(self, path, timeout=120):
        # Stream with a hard cap, including when the file grows after stat().
        stream = await (await self.connection()).files.read(path, format="stream", user="user", request_timeout=timeout)
        content = bytearray()
        async with stream:
            async for chunk in stream:
                content.extend(chunk)
                if len(content) > 25 * 1024 * 1024:
                    raise RuntimeError("Download exceeds limit")
        return bytes(content)

    async def make_dir(self, path, mode="755", **kwargs):
        await (await self.connection()).files.make_dir(path, user="user", request_timeout=30)

    async def create_signed_preview_url(self, port, *, expires_in_seconds, task_id, user_id):
        from app.computer.viewer import signed_viewer_url
        sdk = await self.connection()
        # Ingress requires E2B's traffic token. Only the server-side viewer proxy receives it.
        if not sdk.traffic_access_token:
            raise RuntimeError("Desktop traffic authentication is unavailable")
        await self.checked("flock --close --wait 5 /tmp/band-viewer-setup.lock sh -c " + shlex.quote(
            "pgrep -f '^x11vnc .*rfbport 5900' >/dev/null || x11vnc -bg -display :0 -forever -shared -localhost -rfbport 5900 -nopw "
            ">/tmp/band-vnc.log 2>&1; "
            "curl --fail --silent --max-time 2 http://localhost:6080/vnc.html >/dev/null || "
            "(nohup /opt/noVNC/utils/websockify/run --web=/opt/noVNC "
            "6080 localhost:5900 >/tmp/band-novnc.log 2>&1 </dev/null &); "
            "pgrep -f '^x11vnc .*rfbport 5901' >/dev/null || "
            "x11vnc -bg -display :0 -forever -shared -localhost -rfbport 5901 -nopw -viewonly "
            ">/tmp/band-vnc-view.log 2>&1; "
            "curl --fail --silent --max-time 2 http://localhost:6081/vnc.html >/dev/null || "
            "(nohup /opt/noVNC/utils/websockify/run --web=/opt/noVNC "
            "6081 localhost:5901 >/tmp/band-novnc-view.log 2>&1 </dev/null &)"))
        for _ in range(40):
            if not (await self.exec("curl --fail --silent --max-time 2 http://localhost:6080/vnc.html "
                                   ">/dev/null && curl --fail --silent --max-time 2 "
                                   "http://localhost:6081/vnc.html >/dev/null", timeout=5)).exit_code:
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Desktop viewer did not start")
        await sdk.set_timeout(max(settings.e2b_timeout, expires_in_seconds), request_timeout=20)
        with db() as session:
            session.merge(ComputerConnection(sandbox_id=self.id, host=sdk.get_host(port),
                                             access_token_enc=encrypt_secret(sdk.traffic_access_token)))
            session.commit()
        return SimpleNamespace(url=signed_viewer_url(task_id, user_id, self.id))

    async def prepare_app_connection(self, *, seconds: int):
        """Keep private ingress credentials on the server, without starting a desktop viewer."""
        sdk = await self.connection()
        if not sdk.traffic_access_token:
            raise RuntimeError("App traffic authentication is unavailable")
        await sdk.set_timeout(max(settings.e2b_timeout, seconds), request_timeout=20)
        with db() as session:
            session.merge(ComputerConnection(sandbox_id=self.id, host=sdk.get_host(6080),
                                             access_token_enc=encrypt_secret(sdk.traffic_access_token)))
            session.commit()
