"""Request guards and lookups shared by the API routers."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session

from app.auth import current_user
from app.persistence.models import AgentProfile, AgentTask, Channel, ChannelMember, DirectMessage, User, get_session
from app.workspace.access import channel_visible
from app.workspace.permissions import is_admin

NO_STORE = {"Cache-Control": "no-store"}


def require_admin(session: Session, user: User, detail: str) -> None:
    if not is_admin(session, user.id):
        raise HTTPException(403, detail)


def admin_user(detail: str) -> Callable[..., User]:
    """A dependency that returns the signed-in workspace admin and refuses everyone else with `detail`."""
    def dependency(user: User = Depends(current_user), session: Session = Depends(get_session)) -> User:
        require_admin(session, user, detail)
        return user
    return dependency


def require_member(session: Session, channel_id: int, user: User, detail: str) -> None:
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        raise HTTPException(403, detail)


def channel_or_404(session: Session, channel_id: int, user: User) -> Channel:
    channel = session.get(Channel, channel_id)
    if not channel or not channel_visible(session, channel_id, user.id):
        raise HTTPException(404, "Channel not found")
    return channel


def agent_or_404(session: Session, agent_id: int) -> AgentProfile:
    profile = session.get(AgentProfile, agent_id)
    if not profile:
        raise HTTPException(404, "Agent not found")
    return profile


def task_or_404(session: Session, task_id: str) -> AgentTask:
    task = session.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


def require_task_member(session: Session, task: AgentTask, user: User) -> None:
    dm = session.get(DirectMessage, task.channel_id)
    if dm and dm.user_id != user.id:
        raise HTTPException(404, "Task not found")
    require_member(session, task.channel_id, user, "Join the channel to control its tasks")


async def read_limited(request: Request, limit: int, detail: str) -> bytes:
    """Read a streamed request body, refusing it with 413 and `detail` once it exceeds `limit` bytes."""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, detail)
    return bytes(data)
