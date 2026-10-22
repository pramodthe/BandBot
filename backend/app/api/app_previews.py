"""Authenticated app publication and viewer lifecycle; browser traffic uses the isolated gateway."""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.auth import current_user
from app.computer import ComputerUnavailable, TaskComputer, sandbox_record
from app.computer.app_previews import (LEASE_SECONDS, aware, create_lease, get_preview, prepare,
                                      preview_base, publish, summary)
from app.persistence.models import AgentTask, AppPreviewLease, ChannelMember, DirectMessage, User, get_session, utcnow
from app.workspace.permissions import allowed

router = APIRouter()
computer_factory = TaskComputer
logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store"}


class PublishApp(BaseModel):
    port: int = Field(ge=1024, le=49151, strict=True)
    title: str = Field(min_length=1, max_length=100)
    path: str = Field(default="/", max_length=2000)


async def run_on_computer(task, operation):
    computer = computer_factory(task)
    try:
        return await operation(computer)
    except ComputerUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("App preview connection failed: %s", type(exc).__name__)
        raise HTTPException(502, "Could not connect to the app computer. Try again.") from None
    finally:
        await computer.detach()


@router.get("/api/app-previews/{preview_id}")
def app_detail(preview_id: str, response: Response, user: User = Depends(current_user), session: Session = Depends(get_session)):
    response.headers.update(NO_STORE)
    preview, _ = get_preview(session, preview_id, user)
    return summary(preview)


@router.post("/api/tasks/{task_id}/app-previews")
async def publish_app(task_id: str, body: PublishApp, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = session.get(AgentTask, task_id)
    dm = session.get(DirectMessage, task.channel_id) if task else None
    if not task or (dm and dm.user_id != user.id):
        raise HTTPException(404, "Task not found")
    if not session.get(ChannelMember, (task.channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to publish an app preview")
    if not allowed(session, user.id, task.channel_id, "computer_control"):
        raise HTTPException(403, "Only workspace admins can publish app previews in this channel")
    preview_base()
    result = await run_on_computer(task, lambda computer: publish(computer, port=body.port, title=body.title,
                                    path=body.path, call_id=f"manual-app:{body.port}:{body.path}"))
    from app.workspace.service import service
    await service.publish_components([task.id])
    return result


@router.post("/api/app-previews/{preview_id}/sessions")
async def connect_app(preview_id: str, request: Request, response: Response, user: User = Depends(current_user), session: Session = Depends(get_session)):
    response.headers.update(NO_STORE)
    preview, task = get_preview(session, preview_id, user)
    origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
    # Register the lease before connecting so a finishing worker keeps the computer awake.
    lease = create_lease(preview, user, origin)
    try:
        await run_on_computer(task, lambda computer: prepare(computer, expected_sandbox=preview.sandbox_id, port=preview.port))
    except BaseException:
        with Session(session.bind) as cleanup:
            if row := cleanup.get(AppPreviewLease, lease["id"]):
                cleanup.delete(row)
                cleanup.commit()
        raise
    return {**lease, "preview": summary(preview)}


def viewer_lease(session, lease_id: str, user: User):
    lease = session.get(AppPreviewLease, lease_id)
    if not lease or lease.user_id != user.id:
        raise HTTPException(404, "Preview session not found")
    preview, task = get_preview(session, lease.preview_id, user)
    return lease, preview, task


@router.post("/api/app-preview-sessions/{lease_id}/renew")
async def renew_app(lease_id: str, response: Response, user: User = Depends(current_user), session: Session = Depends(get_session)):
    response.headers.update(NO_STORE)
    lease, preview, task = viewer_lease(session, lease_id, user)
    if aware(lease.until) <= utcnow():
        raise HTTPException(410, "Preview session expired. Reconnect from chat.")
    record = sandbox_record(preview.computer_key)
    if not record or record.sandbox_id != preview.sandbox_id or record.status != "running":
        raise HTTPException(409, "The computer is paused or has changed. Reconnect from chat.")
    await run_on_computer(task, lambda computer: prepare(computer, expected_sandbox=preview.sandbox_id, port=preview.port))
    lease.until = utcnow() + timedelta(seconds=LEASE_SECONDS)
    session.add(lease)
    session.commit()
    return {"expires_at": lease.until.isoformat()}


@router.delete("/api/app-preview-sessions/{lease_id}", status_code=204)
async def disconnect_app(lease_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    lease, preview, task = viewer_lease(session, lease_id, user)
    session.delete(lease)
    session.commit()
    running = session.exec(select(AgentTask.id).where(AgentTask.agent_id == preview.agent_id,
        AgentTask.channel_id == preview.channel_id, AgentTask.status == "running")).first()
    if not running:
        computer = computer_factory(task)
        try:
            await computer.close()
        finally:
            await computer.detach()
    return Response(status_code=204, headers=NO_STORE)
