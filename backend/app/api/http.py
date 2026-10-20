from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from sqlalchemy import case, or_, update

from app.auth import current_user, hash_password, issue_token, user_id_from_token, verify_password
from app.api.skills import router as skills_router
from app.api.generative import router as generative_router
from app.api.documents import router as documents_router
from app.api.openai_settings import router as openai_router
from app.api.app_previews import router as app_previews_router
from app.computer.app_proxy import AppPreviewDispatch
from app.openai_provider import configuration, model_catalog, migrate_profiles
from app.config import FRONTEND_DIST, settings
from app.workspace.events import bus
from app.tasks.usage import channel_usage, task_usage
from app.persistence.models import (AgentMemory, AgentPersona, AgentTask, AgentProfile, AuditEvent, Budget, Channel, ChannelMember,
                                    ChannelPolicy, DirectMessage, Message, TaskApproval, User, WorkspaceRole, db,
                                    get_session, init_db, utcnow)
from app.workspace.audit import record, serialize_event
from app.workspace.permissions import allowed, ensure_owner, is_admin, role_of, serialize_policy, workspace_roles
from app.workspace.roles import ROLES
from app.tasks.schedules import cancel_schedule, channel_schedules, describe, serialize_schedule
from app.tasks import decide_approval, serialize_task
from e2b import AuthenticationException, SandboxException, ServiceBusyException

from app.computer import (TRANSFER_LIMIT, ComputerUnavailable, TaskComputer, active_control, computer_key,
                          release_control, sandbox_record, take_control)
from app.computer.display import display_state
from app.computer.activity import used_computer
from app.computer.viewer import router as computer_viewer_router
from app.runtime.tools.composio import (ComposioError, authorization_link, disconnect_plugin, list_plugins,
                                        normalize_slugs, set_plugin_access)
from app.workspace.service import ServiceError, serialize_messages, serialize_user, service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpx2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    migrate_profiles()
    with db() as session:
        if not session.exec(select(Channel)).first():
            session.add(Channel(name="general", topic="Humans and agents, all together"))
            session.commit()
        ensure_owner(session)
    # Stay closed until Band setup finishes, so a health check cannot pass early.
    await service.startup()
    try:
        yield
    finally:
        await service.shutdown()


app = FastAPI(title="BAND_BOTS", lifespan=lifespan)
app.add_middleware(AppPreviewDispatch)
app.include_router(app_previews_router)
app.include_router(skills_router)
app.include_router(generative_router)
app.include_router(computer_viewer_router)
app.include_router(documents_router)
app.include_router(openai_router)


@app.exception_handler(ServiceError)
async def service_error(_request: Request, exc: ServiceError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


# --- auth ---


class SignupBody(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=6, max_length=128)


class LoginBody(BaseModel):
    username: str
    password: str


def _join_general(session: Session, user: User) -> None:
    general = session.exec(select(Channel).where(Channel.name == "general")).first()
    if general:
        session.add(ChannelMember(channel_id=general.id, member_type="human", member_id=user.id))


@app.post("/api/auth/signup")
async def signup(body: SignupBody, session: Session = Depends(get_session)):
    username = body.username.lower()
    if session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(409, "That username is taken")
    user = User(username=username, display_name=body.display_name.strip(), password_hash=hash_password(body.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    _join_general(session, user)
    session.commit()
    ensure_owner(session)
    await bus.publish({"type": "user", "user": serialize_user(user)})
    if role_of(session, user.id) != "member":
        await bus.publish({"type": "workspace_role", "user_id": user.id, "role": role_of(session, user.id)})
    general = session.exec(select(Channel).where(Channel.name == "general")).first()
    if general:
        await service.publish_channel(general.id)
    return {"token": issue_token(user.id), "user": serialize_user(user)}


@app.post("/api/auth/login")
async def login(body: LoginBody, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.username == body.username.lower())).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Wrong username or password")
    return {"token": issue_token(user.id), "user": serialize_user(user)}


@app.get("/api/bootstrap")
async def bootstrap(user: User = Depends(current_user), session: Session = Depends(get_session)):
    dms = {dm.channel_id: dm for dm in session.exec(select(DirectMessage)).all()}
    visible = [c for c in session.exec(select(Channel).order_by(Channel.name)).all()
               if c.id not in dms or (dms[c.id].user_id == user.id and dms[c.id].agent_id is not None)]
    return {
        "me": serialize_user(user),
        "users": [serialize_user(u) for u in session.exec(select(User)).all()],
        "channels": [service.serialize_channel(session, c) for c in visible],
        "agents": [service.serialize_agent(a) for a in session.exec(select(AgentProfile)).all()],
        "roles": [{"id": k, **v} for k, v in ROLES.items()],
        "workspace_roles": workspace_roles(session),
        "models": model_catalog(),
        "band": service.org.status(),
        "openai_configured": bool(configuration()[0]),
    }


# --- roles, policies, and the audit log ---


def _audit(user: User, action: str, /, *, channel_id: int | None = None, task_id: str | None = None,
           session: Session | None = None, **detail) -> None:
    record(action, actor_type="human", actor_id=user.id, actor_name=user.display_name, channel_id=channel_id,
           task_id=task_id, detail=detail, session=session)


def _require_admin(session: Session, user: User, detail: str) -> None:
    if not is_admin(session, user.id):
        raise HTTPException(403, detail)


class RoleBody(BaseModel):
    role: Literal["admin", "member"]


@app.put("/api/users/{user_id}/role")
async def set_role(user_id: int, body: RoleBody, user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    if role_of(session, user.id) != "owner":
        raise HTTPException(403, "Only the workspace owner can change roles")
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    current = role_of(session, user_id)
    if current == "owner":
        raise HTTPException(409, "The owner's role can't change")
    if current == body.role:
        return {"user_id": user_id, "role": body.role}
    row = session.get(WorkspaceRole, user_id)
    if body.role == "member" and row:
        session.delete(row)
    elif body.role == "admin":
        session.merge(WorkspaceRole(user_id=user_id, role="admin", updated_at=utcnow()))
    _audit(user, "role_changed", session=session, user=target.display_name, role=body.role)
    session.commit()
    await bus.publish({"type": "workspace_role", "user_id": user_id, "role": body.role})
    return {"user_id": user_id, "role": body.role}


class PolicyBody(BaseModel):
    approvers: Literal["members", "admins"] | None = None
    computer_control: Literal["members", "admins"] | None = None


POLICY_NOTICE = {"approvers": "approve agent actions", "computer_control": "watch or take over agent computers live"}


@app.put("/api/channels/{channel_id}/policy")
async def set_policy(channel_id: int, body: PolicyBody, user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages don't have channel policies")
    _require_admin(session, user, "Only workspace admins can change channel policies")
    policy = session.get(ChannelPolicy, channel_id) or ChannelPolicy(channel_id=channel_id)
    changes = {name: value for name, value in body.model_dump().items()
               if value is not None and value != getattr(policy, name)}
    if changes:
        for name, value in changes.items():
            setattr(policy, name, value)
        policy.updated_by, policy.updated_at = user.id, utcnow()
        session.add(policy)
        _audit(user, "policy_changed", channel_id=channel_id, session=session, **changes)
        session.commit()
        await service.publish_channel(channel_id)
        notice = "; ".join(f"{'only admins' if value == 'admins' else 'any member'} can {POLICY_NOTICE[name]}"
                           for name, value in changes.items())
        await service.system_message(channel_id, f"{user.display_name} changed this channel's policy: {notice}.")
    return serialize_policy(policy)


AUDIT_PAGE = 200


@app.get("/api/channels/{channel_id}/audit")
async def channel_audit(channel_id: int, before: int | None = None, after: int | None = None,
                        task_id: str | None = None, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    """The channel's activity, newest first; page back with `before` (an event id).

    With `after`, the events since that id come oldest first, so polling clients never skip any.
    Tool arguments are for channel members only.
    """
    _channel_or_404(session, channel_id, user)
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to see its activity")
    query = select(AuditEvent).where(AuditEvent.channel_id == channel_id)
    if task_id:
        query = query.where(AuditEvent.task_id == task_id)
    if after is not None:
        rows = session.exec(query.where(AuditEvent.id > after).order_by(AuditEvent.id).limit(AUDIT_PAGE)).all()
        return JSONResponse([serialize_event(event) for event in rows], headers=NO_STORE)
    if before:
        query = query.where(AuditEvent.id < before)
    rows = session.exec(query.order_by(AuditEvent.id.desc()).limit(AUDIT_PAGE)).all()
    return JSONResponse([serialize_event(event) for event in rows], headers=NO_STORE)


@app.get("/api/audit")
async def workspace_audit(before: int | None = None, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    """Every channel's activity for admins, except other people's direct messages."""
    _require_admin(session, user, "Only workspace admins can see the whole audit log")
    private = select(DirectMessage.channel_id).where(DirectMessage.user_id != user.id)
    query = select(AuditEvent).where(or_(AuditEvent.channel_id.is_(None), AuditEvent.channel_id.notin_(private)))
    if before:
        query = query.where(AuditEvent.id < before)
    rows = session.exec(query.order_by(AuditEvent.id.desc()).limit(AUDIT_PAGE)).all()
    return JSONResponse([serialize_event(event) for event in rows], headers=NO_STORE)


# --- channels ---


class ChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    topic: str = Field(default="", max_length=200)


def _channel_or_404(session: Session, channel_id: int, user: User) -> Channel:
    channel = session.get(Channel, channel_id)
    dm = session.get(DirectMessage, channel_id)
    if not channel or (dm and dm.user_id != user.id):
        raise HTTPException(404, "Channel not found")
    return channel


def _reject_dm(session: Session, channel_id: int, detail: str) -> None:
    if session.get(DirectMessage, channel_id):
        raise HTTPException(409, detail)


@app.post("/api/channels")
async def create_channel(body: ChannelBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    name = re.sub(r"[^a-z0-9_-]+", "-", body.name.strip().lower()).strip("-")
    if not name:
        raise HTTPException(422, "Channel name needs letters or numbers")
    return await service.create_channel(name, body.topic.strip(), user)


@app.post("/api/channels/{channel_id}/join")
async def join_channel(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
        await service.publish_channel(channel_id)
    return {"ok": True}


@app.post("/api/channels/{channel_id}/leave")
async def leave_channel(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "You can't leave a direct message")
    member = session.get(ChannelMember, (channel_id, "human", user.id))
    if member:
        session.delete(member)
        session.commit()
        await service.publish_channel(channel_id)
    return {"ok": True}


class ChannelAgentBody(BaseModel):
    agent_id: int


@app.post("/api/channels/{channel_id}/agents")
async def add_channel_agent(
    channel_id: int, body: ChannelAgentBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    profile = _agent_or_404(session, body.agent_id)
    await service.add_agent_to_channel(channel_id, profile)
    _audit(user, "agent_added", channel_id=channel_id, agent=profile.name)
    return {"ok": True}


@app.delete("/api/channels/{channel_id}/agents/{agent_id}")
async def remove_channel_agent(
    channel_id: int, agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    profile = _agent_or_404(session, agent_id)
    await service.remove_agent_from_channel(channel_id, profile)
    _audit(user, "agent_removed", channel_id=channel_id, agent=profile.name)
    return {"ok": True}


@app.get("/api/channels/{channel_id}/peers")
async def list_channel_peers(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    return await service.list_peers(channel_id)


class OutsideAgentBody(BaseModel):
    band_id: str = Field(min_length=1, max_length=100)


@app.post("/api/channels/{channel_id}/outside-agents")
async def invite_outside_agent(
    channel_id: int, body: OutsideAgentBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to invite outside agents")
    await service.invite_outside_agent(user, channel_id, body.band_id)
    _audit(user, "outside_agent_invited", channel_id=channel_id, band_id=body.band_id)
    return {"ok": True}


@app.delete("/api/channels/{channel_id}/outside-agents/{band_id}")
async def remove_outside_agent(
    channel_id: int, band_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to remove outside agents")
    await service.remove_outside_agent(channel_id, band_id)
    _audit(user, "outside_agent_removed", channel_id=channel_id, band_id=band_id)
    return {"ok": True}


@app.get("/api/channels/{channel_id}/messages")
async def list_messages(
    channel_id: int,
    before: int | None = None,
    limit: int = 100,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _channel_or_404(session, channel_id, user)
    query = select(Message).where(Message.channel_id == channel_id)
    if before:
        query = query.where(Message.id < before)
    rows = session.exec(query.order_by(Message.id.desc()).limit(min(limit, 200))).all()
    return serialize_messages(session, list(reversed(rows)))


class MessageBody(BaseModel):
    content: str = Field(default="", max_length=8000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)


@app.post("/api/channels/{channel_id}/messages")
async def post_message(
    channel_id: int, body: MessageBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id, user)
    if not body.content.strip() and not body.attachment_ids:
        raise HTTPException(422, "Write a message or attach a document")
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
        await service.publish_channel(channel_id)
    return await service.post_human_message(user, channel_id, body.content.strip(), body.attachment_ids)


# --- agents ---


AvatarStyle = Literal["teal", "sage", "blue", "plum", "terracotta", "violet"]


class AgentCreateBody(BaseModel):
    name: str = Field(min_length=3, max_length=40)
    role: str = "custom"
    instructions: str = Field(default="", max_length=6000)
    specialty: str = Field(default="", max_length=160)
    avatar: AvatarStyle | None = None
    model: str | None = None
    channel_id: int | None = None
    # None means every plugin connected in the workspace, including ones connected later.
    plugins: list[str] | None = None


class AgentUpdateBody(BaseModel):
    role: str | None = None
    instructions: str | None = Field(default=None, max_length=6000)
    specialty: str | None = Field(default=None, max_length=160)
    avatar: AvatarStyle | None = None
    model: str | None = None
    plugins: list[str] | None = None


def _plugins_or_422(slugs: list[str] | None) -> list[str] | None:
    if slugs is None:
        return None
    try:
        return normalize_slugs(slugs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _validate_role_and_model(role: str | None, model: str | None) -> None:
    if role is not None and role not in ROLES:
        raise HTTPException(422, f"Unknown role {role!r}")
    if model is not None and model not in {m["id"] for m in model_catalog()}:
        raise HTTPException(422, f"Unknown model {model!r}")


@app.post("/api/agents")
async def create_agent(body: AgentCreateBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    body.model = body.model or configuration()[2]
    _validate_role_and_model(body.role, body.model)
    channel_id = body.channel_id
    if channel_id is not None:
        _channel_or_404(session, channel_id, user)
        if session.get(DirectMessage, channel_id):
            channel_id = None
    profile = await service.create_agent(
        name=body.name,
        role=body.role,
        instructions=body.instructions,
        model=body.model,
        channel_id=channel_id,
        created_by=user.id,
        plugins=_plugins_or_422(body.plugins),
        specialty=body.specialty,
        avatar=body.avatar,
    )
    _audit(user, "agent_created", channel_id=channel_id, agent=profile.name, role=profile.role, model=profile.model)
    return service.serialize_agent(profile)


def _agent_or_404(session: Session, agent_id: int) -> AgentProfile:
    profile = session.get(AgentProfile, agent_id)
    if not profile:
        raise HTTPException(404, "Agent not found")
    return profile


@app.patch("/api/agents/{agent_id}")
async def update_agent(
    agent_id: int, body: AgentUpdateBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _validate_role_and_model(body.role, body.model)
    profile = _agent_or_404(session, agent_id)
    if body.model is not None and body.model != profile.model:
        _require_admin(session, user, "Only workspace admins can change an agent's model")
    changed = {name: value for name, value in (("role", body.role), ("model", body.model))
               if value is not None and value != getattr(profile, name)}
    if body.instructions is not None and body.instructions.strip() != profile.instructions:
        changed["instructions"] = "updated"
    if body.specialty is not None or "avatar" in body.model_fields_set:
        persona = session.get(AgentPersona, agent_id) or AgentPersona(agent_id=agent_id)
        if body.specialty is not None:
            persona.specialty = body.specialty.strip()
        if "avatar" in body.model_fields_set:
            persona.avatar = body.avatar
        session.add(persona)
        changed["persona"] = "updated"
    if "plugins" in body.model_fields_set:
        chosen = _plugins_or_422(body.plugins)
        set_plugin_access(profile.id, chosen)
        changed["plugins"] = "all" if chosen is None else ",".join(chosen) or "none"
    if changed:
        _audit(user, "agent_updated", session=session, agent=profile.name, **changed)
    if body.role is not None and not profile.is_master:
        profile.role = body.role
    if body.instructions is not None:
        profile.instructions = body.instructions.strip()
    if body.model is not None:
        profile.model = body.model
    session.add(profile)
    session.commit()
    await service.start_agent(profile)
    payload = service.serialize_agent(profile)
    await bus.publish({"type": "agent", "agent": payload})
    return payload


def _composio_error(exc: ComposioError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@app.get("/api/plugins")
async def plugins(q: str = "", _user: User = Depends(current_user)):
    if len(q) > 80:
        raise HTTPException(422, "Search is limited to 80 characters")
    try:
        return await list_plugins(q)
    except ComposioError as exc:
        raise _composio_error(exc) from exc


@app.post("/api/plugins/{slug}/connect")
async def connect_plugin(slug: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_admin(session, user, "Only workspace admins can connect plugins")
    try:
        url = await authorization_link(slug)
    except ComposioError as exc:
        raise _composio_error(exc) from exc
    _audit(user, "plugin_connected", slug=slug)
    return {"url": url}


@app.delete("/api/plugins/{slug}")
async def remove_plugin(slug: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_admin(session, user, "Only workspace admins can disconnect plugins")
    try:
        await disconnect_plugin(slug)
    except ComposioError as exc:
        raise _composio_error(exc) from exc
    _audit(user, "plugin_disconnected", slug=slug)
    return {"slug": slug, "connected": False}


@app.post("/api/agents/{agent_id}/restart")
async def restart_agent(agent_id: int, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    profile = _agent_or_404(session, agent_id)
    await service.start_agent(profile)
    return service.serialize_agent(profile)


@app.post("/api/agents/{agent_id}/dm")
async def open_dm(agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return await service.open_dm(user, _agent_or_404(session, agent_id))


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_admin(session, user, "Only workspace admins can delete agents")
    profile = _agent_or_404(session, agent_id)
    name = profile.name
    await service.delete_agent(profile)
    _audit(user, "agent_deleted", agent=name)
    return {"ok": True}


# --- durable task controls ---


@app.get("/api/channels/{channel_id}/tasks")
async def list_tasks(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """The 30 newest tasks plus every unfinished one, so long-running tasks keep their live drafts."""
    _channel_or_404(session, channel_id, user)
    newest = session.exec(select(AgentTask).where(AgentTask.channel_id == channel_id)
                          .order_by(AgentTask.created_at.desc()).limit(30)).all()
    unfinished = session.exec(select(AgentTask).where(AgentTask.channel_id == channel_id,
                              AgentTask.status.in_(["queued", "running", "waiting"]))).all()
    listed = {task.id for task in newest}
    rows = newest + [task for task in unfinished if task.id not in listed]
    usage = task_usage(session, [task.id for task in rows])
    return [{**serialize_task(task), "usage": usage.get(task.id)} for task in rows]


@app.get("/api/channels/{channel_id}/usage")
async def get_channel_usage(channel_id: int, user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    return JSONResponse(channel_usage(session, channel_id), headers=NO_STORE)


class BudgetBody(BaseModel):
    scope: Literal["agent", "channel"]
    scope_id: int
    daily_tokens: int | None = Field(default=None, ge=1000, le=1_000_000_000)  # None removes the budget


@app.put("/api/budgets")
async def set_budget(body: BudgetBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_admin(session, user, "Only workspace admins can set budgets")
    if body.scope == "channel":
        target, channel_id = f"#{_channel_or_404(session, body.scope_id, user).name}", body.scope_id
    else:
        target, channel_id = _agent_or_404(session, body.scope_id).name, None
    row = session.get(Budget, (body.scope, body.scope_id))
    result = {"scope": body.scope, "scope_id": body.scope_id, "daily_tokens": body.daily_tokens}
    if (row.daily_tokens if row else None) == body.daily_tokens:
        return result
    if body.daily_tokens is None and row:
        session.delete(row)
    elif body.daily_tokens is not None:
        session.merge(Budget(scope=body.scope, scope_id=body.scope_id, daily_tokens=body.daily_tokens,
                             updated_by=user.id, updated_at=utcnow()))
    _audit(user, "budget_changed", channel_id=channel_id, session=session, target=target,
           daily_tokens=body.daily_tokens or "no limit")
    session.commit()
    await bus.publish({"type": "budget", "scope": body.scope, "scope_id": body.scope_id,
                       "daily_tokens": body.daily_tokens})
    if channel_id is not None:
        limit = f"{body.daily_tokens:,} tokens a day" if body.daily_tokens else "no limit"
        await service.system_message(channel_id, f"{user.display_name} set this channel's token budget to {limit}.")
    return result


def _task_or_404(session: Session, task_id: str) -> AgentTask:
    task = session.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


def _require_task_member(session: Session, task: AgentTask, user: User) -> None:
    dm = session.get(DirectMessage, task.channel_id)
    if dm and dm.user_id != user.id:
        raise HTTPException(404, "Task not found")
    if not session.get(ChannelMember, (task.channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to control its tasks")


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    return {**serialize_task(task), "result": task.result, "artifacts": task.artifacts,
            "usage": task_usage(session, [task.id]).get(task.id)}


@app.get("/api/tasks/{task_id}/computer")
async def task_computer(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    # Computers persist per agent and channel; tasks from before that change kept their own.
    row = sandbox_record(computer_key(task)) or sandbox_record(task_id)
    display = display_state(computer_key(task))
    return JSONResponse({
        "control": _control_json(active_control(computer_key(task)), user),
        "configured": bool(settings.e2b_api_key),
        "status": row.status if row else "not_started",
        "sandbox_id": row.sandbox_id if row else None,
        "image": row.latest_image if row else "",
        "width": row.width if row else 0,
        "height": row.height if row else 0,
        "last_action": row.last_action if row else "",
        "steps": display.steps,
        "url": display.url,
        "network_warning": display.network_warning,
        "observed_at": display.observed_at or None,
        "updated_at": row.updated_at.isoformat() if row else None,
        "error": row.error if row else None,
        "dashboard_url": "https://e2b.dev/dashboard",
    }, headers={"Cache-Control": "no-store"})


NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/agents/{agent_id}/computers")
def agent_computers(agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Discover this agent's computers only in channels the caller belongs to. Never starts one."""
    _agent_or_404(session, agent_id)
    member_channels = select(ChannelMember.channel_id).where(
        ChannelMember.member_type == "human", ChannelMember.member_id == user.id)
    agent_channels = select(ChannelMember.channel_id).where(
        ChannelMember.member_type == "agent", ChannelMember.member_id == agent_id)
    task_channels = select(AgentTask.channel_id).where(AgentTask.agent_id == agent_id)
    channels = session.exec(select(Channel).outerjoin(DirectMessage).where(
        Channel.id.in_(member_channels),
        or_(DirectMessage.user_id == None, DirectMessage.user_id == user.id),
        or_(Channel.id.in_(agent_channels), Channel.id.in_(task_channels)),
    ).order_by(Channel.id)).all()
    scopes = []
    for channel in channels:
        task = session.exec(select(AgentTask).where(AgentTask.agent_id == agent_id, AgentTask.channel_id == channel.id)
                            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
        row = (sandbox_record(computer_key(task)) or sandbox_record(task.id)) if task else None
        working = session.exec(select(AgentTask.id).where(AgentTask.agent_id == agent_id,
            AgentTask.channel_id == channel.id, AgentTask.status == "running")).first()
        scopes.append({"channel_id": channel.id, "channel_name": channel.name,
                       "is_dm": session.get(DirectMessage, channel.id) is not None,
                       "task_id": task.id if task else None, "status": row.status if row else "not_started",
                       "updated_at": row.updated_at.isoformat() if row else None,
                       "agent_working": working is not None,
                       "can_control": allowed(session, user.id, channel.id, "computer_control")})
    return JSONResponse({"configured": bool(settings.e2b_api_key), "computers": scopes}, headers=NO_STORE)


@app.get("/api/channels/{channel_id}/computers")
async def channel_computers(channel_id: int, user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """Each agent's computer in this channel, without screenshots, so clients can poll it cheaply."""
    _channel_or_404(session, channel_id, user)
    computers = []
    for agent_id in session.exec(select(AgentTask.agent_id).where(AgentTask.channel_id == channel_id).distinct()).all():
        latest = session.exec(select(AgentTask).where(AgentTask.channel_id == channel_id, AgentTask.agent_id == agent_id)
                              .order_by(AgentTask.created_at.desc())).first()
        working = session.exec(select(AgentTask).where(AgentTask.channel_id == channel_id,
                               AgentTask.agent_id == agent_id, AgentTask.status == "running")
                               .order_by(AgentTask.created_at.desc())).first()
        computer_working = working is not None and used_computer(session, working.id)
        current = working if computer_working else latest
        row = sandbox_record(computer_key(current))
        if row is None:
            continue
        computers.append({"agent_id": agent_id, "task_id": current.id, "status": row.status,
                          "last_action": row.last_action, "updated_at": row.updated_at.isoformat(),
                          "agent_working": working is not None, "computer_working": computer_working})
    computers.sort(key=lambda c: c["updated_at"], reverse=True)
    return JSONResponse(computers, headers=NO_STORE)


def computer_factory(task: AgentTask) -> TaskComputer:
    """Tests replace this to use a fake E2B client."""
    return TaskComputer(task)


def _control_json(control, user: User) -> dict | None:
    if control is None:
        return None
    return {"user_name": control.user_name, "until": control.until.isoformat(), "mine": control.user_id == user.id}


def _member_task(session: Session, task_id: str, user: User) -> AgentTask:
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    return task


def _agent_name(session: Session, task: AgentTask) -> str:
    agent = session.get(AgentProfile, task.agent_id)
    return agent.name if agent else "the agent"


async def _on_computer(task: AgentTask, operation):
    """Run a person's request on the agent's computer without stopping it afterwards."""
    computer = computer_factory(task)
    try:
        return await operation(computer)
    except ComputerUnavailable as exc:
        raise HTTPException(409, str(exc))
    except (SandboxException, AuthenticationException, ServiceBusyException) as exc:
        # E2B errors can include request details; show only the type.
        logger.warning("E2B request for task %s failed: %s", task.id, type(exc).__name__)
        raise HTTPException(502, f"E2B request failed ({type(exc).__name__})")
    finally:
        await computer.detach()


class LiveBody(BaseModel):
    control: bool = False


def _require_computer_control(session: Session, task: AgentTask, user: User) -> None:
    if not allowed(session, user.id, task.channel_id, "computer_control"):
        raise HTTPException(403, "Only workspace admins can use the live desktop in this channel")


@app.post("/api/tasks/{task_id}/computer/live")
async def computer_live(task_id: str, body: LiveBody, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    """A short-lived noVNC link; input is enabled only for the active take-over owner."""
    task = _member_task(session, task_id, user)
    _require_computer_control(session, task, user)
    view = await _on_computer(task, lambda computer: computer.live_view(user_id=user.id))
    key = computer_key(task)
    if body.control:
        control = take_control(key, user_id=user.id, user_name=user.display_name)
        _audit(user, "computer_taken_over", channel_id=task.channel_id, task_id=task.id,
               agent=_agent_name(session, task))
        await service.system_message(task.channel_id, f"{user.display_name} took control of {_agent_name(session, task)}'s "
                                                      "computer. The agent waits until it is handed back.")
    else:
        control = active_control(key)
    return JSONResponse({**view, "control": _control_json(control, user)}, headers=NO_STORE)


@app.delete("/api/tasks/{task_id}/computer/control")
async def computer_release(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    _require_computer_control(session, task, user)
    if active_control(computer_key(task)):
        release_control(computer_key(task), user_name=user.display_name)
        _audit(user, "computer_handed_back", channel_id=task.channel_id, task_id=task.id,
               agent=_agent_name(session, task))
        await service.system_message(task.channel_id, f"{user.display_name} handed {_agent_name(session, task)}'s "
                                                      "computer back.")
    return {"ok": True}


@app.get("/api/tasks/{task_id}/computer/files")
async def computer_files(task_id: str, path: str = "", user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    entries = await _on_computer(task, lambda computer: computer.list_files(path))
    return JSONResponse(entries, headers=NO_STORE)


@app.get("/api/tasks/{task_id}/computer/files/download")
async def computer_download(task_id: str, path: str, user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    content = await _on_computer(task, lambda computer: computer.download(path))
    _audit(user, "computer_download", channel_id=task.channel_id, task_id=task.id, path=path, bytes=len(content))
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", path.rstrip("/").rsplit("/", 1)[-1]) or "download"
    return Response(content, media_type="application/octet-stream",
                    headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/tasks/{task_id}/computer/files")
async def computer_upload(task_id: str, name: str, request: Request, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > TRANSFER_LIMIT:
            raise HTTPException(413, "Uploads support files up to 25 MB")
    path = await _on_computer(task, lambda computer: computer.upload(name, bytes(content)))
    _audit(user, "computer_upload", channel_id=task.channel_id, task_id=task.id, path=path, bytes=len(content))
    # Agents read the channel, so this tells them where the file is.
    await service.system_message(task.channel_id, f"{user.display_name} uploaded {name} to "
                                                  f"{_agent_name(session, task)}'s computer at {path}.")
    return {"path": path}


@app.post("/api/tasks/{task_id}/computer/stop")
async def computer_stop(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    _require_computer_control(session, task, user)
    if session.exec(select(AgentTask.id).where(AgentTask.agent_id == task.agent_id,
                    AgentTask.channel_id == task.channel_id, AgentTask.status == "running")).first():
        raise HTTPException(409, "The agent is using this computer. Cancel its task first.")
    release_control(computer_key(task), user_name=user.display_name)
    computer = computer_factory(task)
    await computer.close(force=True)
    _audit(user, "computer_stopped", channel_id=task.channel_id, task_id=task.id, agent=_agent_name(session, task))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    if task.status not in {"queued", "running", "waiting"}:
        raise HTTPException(409, "Only queued, running, or waiting tasks can be cancelled")
    # Cancelling a waiting task denies its approval requests, so it follows the approvals policy.
    has_approval = session.exec(select(TaskApproval.id).where(TaskApproval.task_id == task_id,
                                                             TaskApproval.status == "pending")).first()
    if task.status == "waiting" and has_approval and not allowed(session, user.id, task.channel_id, "approvers"):
        raise HTTPException(403, "Only workspace admins can cancel a task waiting for approval in this channel")
    idle = AgentTask.status.in_(["queued", "waiting"])
    session.exec(update(AgentTask).where(AgentTask.id == task_id, AgentTask.status.in_(["queued", "running", "waiting"]))
                 .values(cancel_requested=True,
                         status=case((idle, "cancelled"), else_=AgentTask.status),
                         progress=case((idle, "Cancelled"), else_="Cancellation requested"),
                         updated_at=utcnow()))
    # Close the task's open approval requests so their cards disappear and cannot be approved later.
    closed = session.exec(select(TaskApproval.id).where(TaskApproval.task_id == task_id,
                                                        TaskApproval.status == "pending")).all()
    session.exec(update(TaskApproval).where(TaskApproval.task_id == task_id, TaskApproval.status == "pending")
                 .values(status="denied", note="Task cancelled", decided_by=user.display_name, decided_at=utcnow()))
    _audit(user, "task_cancelled", channel_id=task.channel_id, task_id=task_id, session=session,
           **({"approvals_denied": len(closed)} if closed else {}))
    session.commit()
    session.expire_all()
    if closed:
        await service.publish_approvals(closed)
    await service.publish_inputs(task_id)
    return serialize_task(session.get(AgentTask, task_id))


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    if task.status not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled tasks can be retried")
    from app.persistence.models import TaskModel, TaskDocument
    from uuid import uuid4
    pinned = session.get(TaskModel, task_id)
    if pinned and pinned.provider != "openai":
        replacement = AgentTask(id=uuid4().hex, source_key=f"openai-restart:{task_id}:{uuid4().hex}",
                                channel_id=task.channel_id, agent_id=task.agent_id, room_id=task.room_id,
                                prompt=task.prompt, context=task.context, reply_to=task.reply_to,
                                progress="Restarted under OpenAI with a new checkpoint")
        task.status, task.progress = "cancelled", "Restarted under OpenAI; original checkpoint preserved"
        session.add(task)
        session.add(replacement)
        for link in session.exec(select(TaskDocument).where(TaskDocument.task_id == task_id)).all():
            session.add(TaskDocument(task_id=replacement.id, document_id=link.document_id))
        _audit(user, "task_restarted", channel_id=task.channel_id, task_id=task_id, session=session)
        session.commit()
        return serialize_task(replacement)
    session.exec(update(AgentTask).where(AgentTask.id == task_id, AgentTask.status.in_(["failed", "cancelled"]))
                 .values(status="queued", cancel_requested=False, error=None,
                         progress="Queued to resume saved progress", updated_at=utcnow()))
    _audit(user, "task_retried", channel_id=task.channel_id, task_id=task_id, session=session)
    session.commit()
    session.expire_all()
    await service.publish_inputs(task_id)
    return serialize_task(session.get(AgentTask, task_id))


@app.get("/api/channels/{channel_id}/approvals")
async def list_approvals(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    return session.exec(select(TaskApproval).join(AgentTask, AgentTask.id == TaskApproval.task_id).where(
        TaskApproval.channel_id == channel_id, TaskApproval.status == "pending", AgentTask.status == "waiting")
        .order_by(TaskApproval.created_at)).all()


class DecisionBody(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=2000)


@app.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, body: DecisionBody, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    approval = session.get(TaskApproval, approval_id)
    if approval is None:
        raise HTTPException(404, "Approval request not found")
    if not session.get(ChannelMember, (approval.channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to decide its approvals")
    if not allowed(session, user.id, approval.channel_id, "approvers"):
        raise HTTPException(403, "Only workspace admins can decide approvals in this channel")
    decided = decide_approval(approval_id, approve=body.approve, decided_by=user.display_name, note=body.note)
    if decided is None:
        raise HTTPException(409, "This request was already decided or its task is no longer waiting")
    verdict = "approved" if body.approve else "denied"
    _audit(user, f"approval_{verdict}", channel_id=decided.channel_id, task_id=decided.task_id,
           action=decided.action, note=body.note)
    await service.publish_approvals([approval_id])
    await service.system_message(decided.channel_id, f"{user.display_name} {verdict}: {decided.action}"
                                 + (f" ({body.note})" if body.note else ""))
    return decided


@app.get("/api/channels/{channel_id}/schedules")
async def list_schedules(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    return [serialize_schedule(s) for s in channel_schedules(channel_id)]


@app.delete("/api/channels/{channel_id}/schedules/{schedule_id}")
async def delete_schedule(channel_id: int, schedule_id: int, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to manage its schedules")
    cancelled = cancel_schedule(channel_id, schedule_id)
    if cancelled is None:
        raise HTTPException(404, "Schedule not found")
    _audit(user, "schedule_cancelled", channel_id=channel_id, schedule=describe(cancelled), prompt=cancelled.prompt)
    await service.system_message(channel_id, f"{user.display_name} cancelled a schedule ({describe(cancelled)}): "
                                             f"{cancelled.prompt[:200]}")
    return {"ok": True}


@app.get("/api/channels/{channel_id}/agents/{agent_id}/memories")
async def list_memories(channel_id: int, agent_id: int, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    return session.exec(select(AgentMemory).where(AgentMemory.agent_id == agent_id,
                        AgentMemory.channel_id == channel_id)).all()


@app.delete("/api/channels/{channel_id}/agents/{agent_id}/memories/{key}")
async def forget_memory(channel_id: int, agent_id: int, key: str, user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to manage its memories")
    memory = session.get(AgentMemory, (agent_id, channel_id, key))
    if memory is None:
        raise HTTPException(404, "Memory not found")
    session.delete(memory)
    _audit(user, "memory_forgotten", channel_id=channel_id, session=session, agent_id=agent_id, key=key)
    session.commit()
    return {"ok": True}


# --- realtime ---


@app.websocket("/ws")
async def websocket(ws: WebSocket, token: str = ""):
    user_id = user_id_from_token(token)
    if user_id is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    await bus.connect(user_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await bus.disconnect(user_id, ws)


def health_status() -> tuple[int, dict]:
    band = service.org.status()
    # A configured Band account that failed to register is not a healthy deploy.
    ok = service.ready and not (band["configured"] and (band["error"] or not band["online"]))
    return (200 if ok else 503), {"ok": ok, "band": band}


@app.get("/api/health")
async def health():
    status, body = health_status()
    return JSONResponse(body, status_code=status)


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        candidate = FRONTEND_DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
