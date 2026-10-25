"""Published server apps and expiring, channel-scoped viewer capabilities."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.computer.ingress import endpoint
from app.config import settings
from app.persistence.models import (AgentProfile, AgentTask, AppPreview, AppPreviewLease, ChannelMember,
                                    DirectMessage, MessageComponent, User, db, utcnow)
from app.workspace.permissions import allowed

LEASE_SECONDS = 900


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 49151 or 5900 <= port <= 6100 or 10000 <= port <= 10129 or 20000 <= port <= 20129 or port in {2375, 2376, 9222, 9223}:
        raise ValueError("Use an app port from 1024–49151, excluding desktop, browser-debugging and infrastructure ports.")
    return port


def validate_path(path: str) -> str:
    if len(path) > 2000 or not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(ch) < 32 for ch in path):
        raise ValueError("Use an app path starting with one slash, such as / or /dashboard.")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        raise ValueError("App previews accept a registered sandbox port, not an external URL.")
    return path


def preview_base():
    parsed = urlsplit(settings.app_preview_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise HTTPException(503, "Configure APP_PREVIEW_BASE_URL with the dedicated preview domain.")
    if parsed.hostname in {"127.0.0.1", "::1"}:
        raise HTTPException(503, "Use localhost or a wildcard preview domain in APP_PREVIEW_BASE_URL.")
    return parsed


def preview_url(token: str, path: str = "/") -> str:
    base = preview_base()
    return urlunsplit((base.scheme, f"{token}.{base.netloc}", "", "", "")) + path


def host_token(host: str) -> str | None:
    base = preview_base()
    if not host.lower().endswith("." + base.netloc.lower()):
        return None
    token = host[:-(len(base.netloc) + 1)]
    return token.lower() if re.fullmatch(r"[a-fA-F0-9]{32}", token) else ""


def authorize(session, preview: AppPreview, user: User) -> AgentTask:
    from app.computer.desktop import computer_key
    task = session.get(AgentTask, preview.task_id)
    if task:
        from app.computer.access import require_computer_owner
        require_computer_owner(session, task, user.id)
    dm = session.get(DirectMessage, preview.channel_id)
    if not task or (dm and dm.user_id != user.id):
        raise HTTPException(404, "App preview not found")
    if not session.get(ChannelMember, (preview.channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to use its app previews")
    if not allowed(session, user.id, preview.channel_id, "computer_control"):
        raise HTTPException(403, "Only workspace admins can use interactive app previews in this channel")
    from app.computer.desktop import active_control
    control = active_control(preview.computer_key)
    if control and control.user_id != user.id:
        raise HTTPException(409, "Someone else has control of this computer. Wait until they hand it back.")
    if not session.get(AgentProfile, preview.agent_id) or computer_key(task) != preview.computer_key:
        raise HTTPException(409, "The agent's computer has changed. Publish this app again.")
    return task


def get_preview(session, preview_id: str, user: User) -> tuple[AppPreview, AgentTask]:
    preview = session.get(AppPreview, preview_id)
    if not preview:
        raise HTTPException(404, "App preview not found")
    return preview, authorize(session, preview, user)


def summary(preview: AppPreview) -> dict:
    from app.computer.desktop import sandbox_record
    record = sandbox_record(preview.computer_key)
    status = "unavailable" if not record or record.sandbox_id != preview.sandbox_id else {
        "running": "running", "starting": "starting", "stopped": "paused",
    }.get(record.status, "unavailable")
    return {"id": preview.id, "title": preview.title, "port": preview.port, "path": preview.path,
            "agent_id": preview.agent_id, "channel_id": preview.channel_id, "status": status}


async def prepare(computer, *, expected_sandbox: str | None = None, port: int = 6080):
    from app.computer.desktop import ComputerUnavailable, sandbox_record
    async with computer.lock:
        record = sandbox_record(computer.key)
        if expected_sandbox and (not record or record.sandbox_id != expected_sandbox):
            raise ComputerUnavailable("The app's computer has changed. Publish this app again.")
        sandbox = await computer.ensure(create=False)
        if expected_sandbox and sandbox.id != expected_sandbox:
            raise ComputerUnavailable("The app's computer has changed. Publish this app again.")
        if computer.provider == "tenki":
            await sandbox.prepare_app_connection(seconds=LEASE_SECONDS, port=port)
        else:
            await sandbox.prepare_app_connection(seconds=LEASE_SECONDS)
        return sandbox


async def publish(computer, *, port: int, title: str, path: str, call_id: str) -> dict:
    from app.computer.desktop import ComputerUnavailable
    validate_port(port)
    validate_path(path)
    if not title.strip() or len(title) > 100:
        raise ValueError("Give the app a title up to 100 characters.")
    task = computer.task
    if getattr(computer, "user_mode", False):
        with db() as session:
            if session.get(DirectMessage, task.channel_id) is None:
                raise ComputerUnavailable("Live apps on a private user computer can only be published in its owner's private conversation. Share exported output copies in group channels.")
    preview_id = hashlib.sha256(f"{task.id}:{call_id}".encode()).hexdigest()[:32]
    with db() as session:
        if prior := session.get(AppPreview, preview_id):
            return summary(prior)
        if len(session.exec(select(AppPreview.id).where(AppPreview.task_id == task.id)).all()) >= 6:
            raise ComputerUnavailable("A task can publish at most six app previews.")
    sandbox = await prepare(computer, port=port)
    host, _ = endpoint(computer.key, sandbox.id, port)
    with db() as session:
        request_host = f"localhost:{port}" if computer.provider == "tenki" or sandbox.labels.get("preview_host") == "localhost" else host
        preview = AppPreview(id=preview_id, task_id=task.id, channel_id=task.channel_id, agent_id=task.agent_id,
                             computer_key=computer.key, sandbox_id=sandbox.id, port=port, title=title.strip(),
                             path=path, request_host=request_host)
        saved = session.get(AgentTask, task.id)
        session.add(preview)
        session.add(MessageComponent(task_id=task.id, tool_call_id=call_id, kind="app",
                                     message_id=saved.reply_message_id if saved else None,
                                     props={"previewId": preview_id, "title": preview.title, "port": port}))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            if not session.get(AppPreview, preview_id):
                raise
    return {**summary(preview), "note": "Live app attached to your reply. Keep the server running in the background. Users connect privately from chat."}


def create_lease(preview: AppPreview, user: User, workspace_origin: str) -> dict:
    parsed = urlsplit(workspace_origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or any(ch in workspace_origin for ch in "\r\n; "):
        raise HTTPException(400, "Invalid workspace origin")
    token = secrets.token_hex(16)
    url = preview_url(token, preview.path)
    if urlsplit(url).netloc == parsed.netloc:
        raise HTTPException(503, "The preview must use a separate origin from the workspace")
    lease = AppPreviewLease(id=uuid4().hex, preview_id=preview.id, user_id=user.id,
                            token_hash=hashlib.sha256(token.encode()).hexdigest(),
                            workspace_origin=f"{parsed.scheme}://{parsed.netloc}",
                            until=utcnow() + timedelta(seconds=LEASE_SECONDS))
    with db() as session:
        session.add(lease)
        session.commit()
    return {"id": lease.id, "url": url, "expires_at": lease.until.isoformat()}


def resolve(token: str):
    from app.computer.desktop import sandbox_record
    with db() as session:
        lease = session.exec(select(AppPreviewLease).where(AppPreviewLease.token_hash == hashlib.sha256(token.encode()).hexdigest())).first()
        if not lease or aware(lease.until) <= utcnow():
            raise HTTPException(410, "Preview session expired. Reconnect from chat.")
        preview = session.get(AppPreview, lease.preview_id)
        user = session.get(User, lease.user_id)
        if not preview or not user:
            raise HTTPException(403, "Preview access is unavailable")
        authorize(session, preview, user)
        record = sandbox_record(preview.computer_key)
        if not record or record.sandbox_id != preview.sandbox_id:
            raise HTTPException(409, "The app's computer has changed. Publish this app again.")
        if record.status != "running":
            raise HTTPException(409, "The app's computer is paused. Reconnect from chat.")
        host, secret = endpoint(preview.computer_key, preview.sandbox_id, validate_port(preview.port))
        if preview.request_host not in {host, f"localhost:{preview.port}"}:
            raise HTTPException(502, "Invalid app routing configuration")
        return lease, preview, host, secret


def has_active_view(computer_key: str) -> bool:
    with db() as session:
        return bool(session.exec(select(AppPreviewLease.id).join(AppPreview).where(
            AppPreview.computer_key == computer_key, AppPreviewLease.until > utcnow())).first())
