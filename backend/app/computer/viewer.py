"""Short-lived, membership-checked WebSocket access to private E2B desktops."""
from __future__ import annotations

import asyncio
import re

from uuid import uuid4

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from itsdangerous import BadSignature, URLSafeTimedSerializer
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.auth import decrypt_secret
from app.config import settings
from app.persistence.models import AgentTask, ChannelMember, ComputerConnection, DirectMessage, User, db, utcnow
from app.workspace.permissions import allowed

router = APIRouter()
VIEW_SECONDS = 15 * 60
_signer = URLSafeTimedSerializer(settings.app_secret, salt="band-e2b-desktop")


def signed_viewer_url(task_id: str, user_id: int, sandbox_id: str) -> str:
    token = _signer.dumps({"task": task_id, "user": user_id, "sandbox": sandbox_id, "nonce": uuid4().hex})
    return f"/api/computers/view/{token}/websockify"


def resolve_viewer(token: str) -> tuple[str, str, float]:
    from app.computer.desktop import active_control, computer_key, sandbox_record
    try:
        data, issued_at = _signer.loads(token, max_age=VIEW_SECONDS, return_timestamp=True)
    except BadSignature:
        raise HTTPException(403, "Desktop link expired or invalid. Reconnect from the computer panel.") from None
    with db() as session:
        task = session.get(AgentTask, data.get("task"))
        user = session.get(User, data.get("user"))
        if task is None or user is None:
            raise HTTPException(403, "Desktop access unavailable")
        dm = session.get(DirectMessage, task.channel_id)
        if (dm and dm.user_id != user.id) or not session.get(ChannelMember, (task.channel_id, "human", user.id)):
            raise HTTPException(403, "Desktop access unavailable")
        if not allowed(session, user.id, task.channel_id, "computer_control"):
            raise HTTPException(403, "Desktop access unavailable")
        record = sandbox_record(computer_key(task))
        connection = session.get(ComputerConnection, data.get("sandbox"))
        if not record or record.sandbox_id != data.get("sandbox") or record.status != "running" or not connection:
            raise HTTPException(409, "Desktop is paused or has changed. Reconnect from the computer panel.")
        if not re.fullmatch(r"6080-[a-zA-Z0-9.-]+\.e2b\.app", connection.host):
            raise HTTPException(502, "Invalid desktop host")
        control = active_control(computer_key(task))
        interactive = control is not None and control.user_id == user.id
        # A separate VNC server enforces view-only access even for a modified browser client.
        host = connection.host if interactive else connection.host.replace("6080-", "6081-", 1)
        return host, decrypt_secret(connection.access_token_enc), max(0, VIEW_SECONDS - (utcnow() - issued_at).total_seconds())


@router.websocket("/api/computers/view/{token}/websockify")
async def desktop_socket(websocket: WebSocket, token: str):
    try:
        host, traffic_token, remaining = resolve_viewer(token)
    except HTTPException:
        await websocket.close(code=1008)
        return
    # A signed URL is a bearer credential; never forward the user's app token or log either credential.
    try:
        async with connect(f"wss://{host}/websockify", additional_headers={"e2b-traffic-access-token": traffic_token},
                           subprotocols=["binary"], open_timeout=20, max_size=8 * 1024 * 1024) as upstream:
            await websocket.accept(subprotocol="binary")
            async def send_upstream():
                while True:
                    event = await websocket.receive()
                    if event["type"] == "websocket.disconnect":
                        return
                    try:
                        if resolve_viewer(token)[0] != host:
                            return  # A take-over ended or changed owner; reconnect with the current permissions.
                    except HTTPException:
                        return
                    await upstream.send(event.get("bytes") if event.get("bytes") is not None else event.get("text", ""))
            async def send_downstream():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
            async def check_access():
                while True:
                    await asyncio.sleep(5)
                    try:
                        if resolve_viewer(token)[0] != host:
                            return
                    except HTTPException:
                        return
            jobs = [asyncio.create_task(send_upstream()), asyncio.create_task(send_downstream()),
                    asyncio.create_task(check_access())]
            try:
                await asyncio.wait(jobs, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
    except (OSError, TimeoutError, WebSocketException, WebSocketDisconnect):
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass
