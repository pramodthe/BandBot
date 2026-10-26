from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from sqlmodel import Session, select
from sqlalchemy import case, or_, update
from sqlalchemy.exc import IntegrityError

from app.auth import (authenticate, current_session_id, current_user, hash_password, issue_token,
                      require_production_secret, revoke_session, revoke_user_sessions, verify_password)
from app.auth_throttle import GENERIC_LOCKOUT, login_limiter, signup_limiter
from app.api.deps import (NO_STORE, agent_or_404, channel_or_404, read_limited, require_admin, require_member,
                          require_task_member, task_or_404)
from app.api.skills import router as skills_router
from app.api.generative import router as generative_router
from app.api.documents import router as documents_router
from app.api.openai_settings import router as openai_router
from app.api.providers import router as providers_router
from app.providers.display import configured as llm_configured, model_catalog as provider_model_catalog
from app.api.app_previews import router as app_previews_router
from app.computer.app_proxy import AppPreviewDispatch
from app.openai_provider import configuration, model_catalog, migrate_profiles
from app.config import FRONTEND_DIST, configure_logging, settings
from app.workspace.events import bus
from app.tasks.usage import channel_usage, task_usage
from app.persistence.models import (AgentConnection, AgentMemory, AgentPersona, AgentTask, AgentProfile, AuditEvent,
                                    Budget, Channel, ChannelMember, ChannelPolicy, DirectMessage, HumanDirectMessage,
                                    Message, PrivateConversation, TaskApproval, TaskComputerBinding, User,
                                    UserBandConnection, WorkspaceRole, db, get_session, init_db, utcnow)
from app.workspace.audit import record_human, serialize_event
from app.workspace.permissions import allowed, ensure_owner, is_admin, role_of, serialize_policy, workspace_roles
from app.workspace.roles import ROLES
from app.tasks.schedules import cancel_schedule, channel_schedules, describe, serialize_schedule
from app.tasks import decide_approval, serialize_task
from e2b import AuthenticationException, SandboxException, ServiceBusyException
from app.computer.providers import (ComputerProviderError, configured, dashboard, provider_for, scope_provider,
                                    scope_computer_key, running_on)
from app.computer.identity import is_shared
from app.providers.contracts import ProviderError
from app.providers.computers import config_for_task, scope_config
from app.computer import (TRANSFER_LIMIT, ComputerUnavailable, TaskComputer, active_control, computer_key,
                          release_control, sandbox_record, take_control)
from app.computer.display import display_state
from app.computer.activity import used_computer
from app.computer.viewer import router as computer_viewer_router
from app.runtime.tools.composio import (ComposioError, authorization_link, disconnect_plugin, list_plugins,
                                        normalize_slugs, set_plugin_access)
from app.workspace.service import ServiceError, serialize_messages, serialize_user, service
from app.api.agent_connections import router as agent_connections_router
from app.agent_connections import CONNECTED_PROVIDERS, MODEL_ID, runtime_available, runtime_for
from app.workspace.access import channel_visible, agent_visible, agent_owner
from app.api.people import incoming_contact_requests, pending_invitations, router as people_router
from app.workspace.health import service_status
from app.workspace.sync import current_cursor

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    require_production_secret()
    from app.workspace.health import mark_api_started
    mark_api_started()
    init_db()
    migrate_profiles()
    with db() as session:
        if not session.exec(select(Channel)).first():
            session.add(Channel(name="general", topic="Humans and agents, all together"))
            session.commit()
        ensure_owner(session)
    startup = asyncio.create_task(service.startup())
    from app.providers.notifications import watch_settings
    provider_watch = asyncio.create_task(watch_settings(), name="provider-settings")
    yield
    startup.cancel()
    provider_watch.cancel()
    await asyncio.gather(startup, provider_watch, return_exceptions=True)
    from app.codex_login import shutdown as close_codex_logins
    await close_codex_logins()
    await service.shutdown()


app = FastAPI(title="BAND_BOTS", lifespan=lifespan)
app.add_middleware(AppPreviewDispatch)
app.include_router(app_previews_router)
app.include_router(skills_router)
app.include_router(generative_router)
app.include_router(computer_viewer_router)
app.include_router(documents_router)
app.include_router(openai_router)
app.include_router(providers_router)
app.include_router(agent_connections_router)
app.include_router(people_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # FastAPI's default validation response includes rejected input values, including pasted credentials.
    if request.url.path == "/api/auth/signup":
        if any(error["loc"][-1] == "band_api_key" for error in exc.errors()):
            return JSONResponse({"detail": "Enter a complete Band API key, or leave it blank and connect later in People.", "field": "band_api_key"}, status_code=422)
        return JSONResponse({"detail": "Invalid account details. Check your username and password length."}, status_code=422)
    if request.url.path.startswith(("/api/agents", "/api/agent-connections", "/api/band/connection", "/api/band/account", "/api/settings/connections", "/api/settings/openai")):
        return JSONResponse({"detail": "Invalid connection details. Check the provider, name and credential."}, status_code=422)
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(ProviderError)
async def provider_error(_request: Request, exc: ProviderError):
    return JSONResponse({"detail": exc.detail, "service": exc.service, "code": exc.code}, status_code=409)


@app.exception_handler(ServiceError)
async def service_error(_request: Request, exc: ServiceError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


# --- auth ---


class SignupBody(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=6, max_length=128)
    band_api_key: SecretStr | None = Field(default=None, max_length=2000)


class LoginBody(BaseModel):
    username: str
    password: str


def _join_general(session: Session, user: User) -> None:
    general = session.exec(select(Channel).where(Channel.name == "general")).first()
    if general:
        session.add(ChannelMember(channel_id=general.id, member_type="human", member_id=user.id))


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.post("/api/auth/signup")
async def signup(body: SignupBody, request: Request, session: Session = Depends(get_session)):
    host = _client_host(request)
    if signup_limiter.too_many(host):
        raise HTTPException(429, GENERIC_LOCKOUT)
    signup_limiter.record_failure(host)
    username = body.username.lower()
    if session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(409, "That username is taken")
    from app.band.connections import validate_key, save_connection
    from app.band.accounts import validate_account, save_account
    key = body.band_api_key.get_secret_value().strip() if body.band_api_key else ""
    identity = None
    try:
        if key:
            identity = await validate_account(key) if key.startswith("band_u_") else await validate_key(key)
    except HTTPException as error:
        return JSONResponse({"detail": error.detail, "field": "band_api_key"}, status_code=error.status_code)
    user = User(username=username, display_name=body.display_name.strip(), password_hash=hash_password(body.password))
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "That username is taken") from None
    try:
        if identity:
            if key.startswith("band_u_"):
                save_account(session, user.id, key, identity)
            else:
                save_connection(session, user.id, key, identity)
    except HTTPException as error:
        session.rollback()
        return JSONResponse({"detail": error.detail, "field": "band_api_key"}, status_code=error.status_code)
    _join_general(session, user)
    from app.workspace.masters import ensure_user_master
    ensure_user_master(session, user)
    session.commit()
    session.refresh(user)
    ensure_owner(session)
    await bus.publish({"type": "user", "user": serialize_user(user)})
    if role_of(session, user.id) != "member":
        await bus.publish({"type": "workspace_role", "user_id": user.id, "role": role_of(session, user.id)})
    general = session.exec(select(Channel).where(Channel.name == "general")).first()
    if general:
        await service.publish_channel(general.id)
    return {"token": issue_token(user.id), "user": serialize_user(user)}


@app.post("/api/auth/login")
async def login(body: LoginBody, request: Request, session: Session = Depends(get_session)):
    key = f"{_client_host(request)}:{body.username.strip().lower()}"
    if login_limiter.too_many(key):
        raise HTTPException(429, GENERIC_LOCKOUT)
    user = session.exec(select(User).where(User.username == body.username.lower())).first()
    if not user or not verify_password(body.password, user.password_hash):
        login_limiter.record_failure(key)
        if login_limiter.too_many(key):
            raise HTTPException(429, GENERIC_LOCKOUT)
        raise HTTPException(401, "Wrong username or password")
    return {"token": issue_token(user.id), "user": serialize_user(user)}


@app.post("/api/auth/logout")
def logout(session_id: str = Depends(current_session_id)):
    revoke_session(session_id)
    return {"ok": True}


@app.post("/api/auth/logout-all")
def logout_all(user: User = Depends(current_user)):
    revoke_user_sessions(user.id)
    return {"ok": True}


class RecoveryEmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class RecoveryRequestBody(BaseModel):
    username: str = Field(min_length=1, max_length=32)


class RecoveryTokenBody(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class PasswordResetBody(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=6, max_length=128)


@app.post("/api/account/recovery-email")
def set_recovery_email(body: RecoveryEmailBody, user: User = Depends(current_user)):
    from app.recovery import start_verification
    try:
        start_verification(user, body.email)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"ok": True}


@app.post("/api/auth/recovery/verify")
def verify_recovery(body: RecoveryTokenBody):
    from app.recovery import confirm_verification
    if not confirm_verification(body.token):
        raise HTTPException(400, "That verification code is invalid or expired")
    return {"ok": True}


@app.post("/api/auth/recovery")
def request_recovery(body: RecoveryRequestBody):
    from app.recovery import GENERIC, request_reset
    request_reset(body.username)
    return {"ok": True, "message": GENERIC}


@app.post("/api/auth/recovery/reset")
def recover_password(body: PasswordResetBody):
    from app.recovery import reset_password
    if not reset_password(body.token, body.password):
        raise HTTPException(400, "That reset code is invalid or expired")
    return {"ok": True}


@app.get("/api/bootstrap")
async def bootstrap(user: User = Depends(current_user), session: Session = Depends(get_session)):
    # Repair existing accounts as well as provisioning new accounts at signup.
    from app.workspace.masters import ensure_user_master
    master, created = ensure_user_master(session, user)
    session.commit()
    if created or master.id not in service.agents.status:
        await service.start_agent(master)
    # Read the cursor before the snapshot so a change committed during this request has a newer cursor
    # and is replayed from the live socket instead of being dropped as already included.
    cursor = current_cursor(session)
    dms = {dm.channel_id: dm for dm in session.exec(select(DirectMessage)).all()}
    visible = [c for c in session.exec(select(Channel).order_by(Channel.name)).all()
               if channel_visible(session, c.id, user.id) and (c.id not in dms or dms[c.id].agent_id is not None)]
    band = await service.band_status(user.id)
    return {
        "me": serialize_user(user),
        "users": [serialize_user(u) for u in session.exec(select(User)).all()],
        "channels": [service.serialize_channel(session, c) for c in visible],
        "agents": [service.serialize_agent(a) for a in session.exec(select(AgentProfile)).all() if agent_visible(session, a, user.id)],
        "roles": [{"id": k, **v} for k, v in ROLES.items()],
        "workspace_roles": workspace_roles(session),
        "models": provider_model_catalog(),
        "band": band,
        "openai_configured": bool(configuration()[0]),
        "llm_configured": llm_configured(),
        "cursor": cursor,
        "invitations": pending_invitations(session, user.id),
        "contact_requests": incoming_contact_requests(session, user.id),
        "service": service_status(user),
    }


# --- roles, policies, and the audit log ---


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
    record_human(user, "role_changed", session=session, user=target.display_name, role=body.role)
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
    channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages don't have channel policies")
    require_admin(session, user, "Only workspace admins can change channel policies")
    policy = session.get(ChannelPolicy, channel_id) or ChannelPolicy(channel_id=channel_id)
    changes = {name: value for name, value in body.model_dump().items()
               if value is not None and value != getattr(policy, name)}
    if changes:
        for name, value in changes.items():
            setattr(policy, name, value)
        policy.updated_by, policy.updated_at = user.id, utcnow()
        session.add(policy)
        record_human(user, "policy_changed", channel_id=channel_id, session=session, **changes)
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
    channel_or_404(session, channel_id, user)
    require_member(session, channel_id, user, "Join the channel to see its activity")
    query = select(AuditEvent).where(AuditEvent.channel_id == channel_id)
    if task_id:
        query = query.where(AuditEvent.task_id == task_id)
    if after is not None:
        rows = session.exec(query.where(AuditEvent.id > after).order_by(AuditEvent.id).limit(AUDIT_PAGE)).all()
        return JSONResponse([_private_event(session, event, user) for event in rows], headers=NO_STORE)
    if before:
        query = query.where(AuditEvent.id < before)
    rows = session.exec(query.order_by(AuditEvent.id.desc()).limit(AUDIT_PAGE)).all()
    return JSONResponse([_private_event(session, event, user) for event in rows], headers=NO_STORE)


@app.get("/api/audit")
async def workspace_audit(before: int | None = None, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    """Every channel's activity for admins, except other people's direct messages."""
    require_admin(session, user, "Only workspace admins can see the whole audit log")
    private = [c.id for c in session.exec(select(Channel)).all() if not channel_visible(session, c.id, user.id)]
    query = select(AuditEvent).where(or_(AuditEvent.channel_id.is_(None), AuditEvent.channel_id.notin_(private)))
    if before:
        query = query.where(AuditEvent.id < before)
    rows = session.exec(query.order_by(AuditEvent.id.desc()).limit(AUDIT_PAGE)).all()
    return JSONResponse([_private_event(session, event, user) for event in rows], headers=NO_STORE)


def _private_event(session, event, user):
    payload = serialize_event(event)
    if event.task_id:
        from app.computer.identity import is_user_desktop
        from app.computer.access import computer_owner_id
        task = session.get(AgentTask, event.task_id)
        if task and is_user_desktop(computer_key(task)) and computer_owner_id(session, task) != user.id:
            payload["detail"] = {"notice": "Private agent activity"}
    return payload


# --- channels ---


class ChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    topic: str = Field(default="", max_length=200)


def _reject_dm(session: Session, channel_id: int, detail: str) -> None:
    if session.get(DirectMessage, channel_id) or session.get(HumanDirectMessage, channel_id):
        raise HTTPException(409, detail)


def _require_channel_owner(session: Session, channel_id: int, user: User) -> None:
    scope = session.get(PrivateConversation, channel_id)
    if scope and scope.owner_id != user.id:
        raise HTTPException(403, "Only the channel owner can manage outside Band participants")


@app.post("/api/channels")
async def create_channel(body: ChannelBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    name = re.sub(r"[^a-z0-9_-]+", "-", body.name.strip().lower()).strip("-")
    if not name:
        raise HTTPException(422, "Channel name needs letters or numbers")
    return await service.create_channel(name, body.topic.strip(), user)


@app.post("/api/channels/{channel_id}/join")
async def join_channel(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    channel_or_404(session, channel_id, user)
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
        await service.publish_channel(channel_id)
    return {"ok": True}


@app.post("/api/channels/{channel_id}/leave")
async def leave_channel(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "You can't leave a direct message")
    scope = session.get(PrivateConversation, channel_id)
    if scope:
        if scope.owner_id == user.id:
            raise HTTPException(409, "The channel owner cannot leave their channel")
    member = session.get(ChannelMember, (channel_id, "human", user.id))
    if member:
        session.delete(member)
        session.commit()
        await service.publish_channel(channel_id)
        if scope:
            await bus.publish({"type": "channel_removed", "channel_id": channel_id}, users={user.id})
    return {"ok": True}


class ChannelAgentBody(BaseModel):
    agent_id: int


@app.post("/api/channels/{channel_id}/agents")
async def add_channel_agent(
    channel_id: int, body: ChannelAgentBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    profile = agent_or_404(session, body.agent_id)
    _require_agent_owner(session, profile, user)
    await service.add_agent_to_channel(channel_id, profile)
    record_human(user, "agent_added", channel_id=channel_id, agent=profile.name)
    return {"ok": True}


@app.delete("/api/channels/{channel_id}/agents/{agent_id}")
async def remove_channel_agent(
    channel_id: int, agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    profile = agent_or_404(session, agent_id)
    scope = session.get(PrivateConversation, channel_id)
    if not scope or scope.owner_id != user.id:
        _require_agent_owner(session, profile, user)
    await service.remove_agent_from_channel(channel_id, profile)
    record_human(user, "agent_removed", channel_id=channel_id, agent=profile.name)
    return {"ok": True}


@app.get("/api/channels/{channel_id}/peers")
async def list_channel_peers(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    channel_or_404(session, channel_id, user)
    _require_channel_owner(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    return await service.list_peers(channel_id, user.id)


class OutsideAgentBody(BaseModel):
    band_id: str = Field(min_length=1, max_length=100)


@app.post("/api/channels/{channel_id}/outside-agents")
async def invite_outside_agent(
    channel_id: int, body: OutsideAgentBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    channel_or_404(session, channel_id, user)
    _require_channel_owner(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    require_member(session, channel_id, user, "Join the channel to invite outside agents")
    await service.invite_outside_agent(user, channel_id, body.band_id)
    record_human(user, "outside_agent_invited", channel_id=channel_id, band_id=body.band_id)
    return {"ok": True}


@app.delete("/api/channels/{channel_id}/outside-agents/{band_id}")
async def remove_outside_agent(
    channel_id: int, band_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    channel_or_404(session, channel_id, user)
    _require_channel_owner(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    require_member(session, channel_id, user, "Join the channel to remove outside agents")
    await service.remove_outside_agent(channel_id, band_id)
    record_human(user, "outside_agent_removed", channel_id=channel_id, band_id=band_id)
    return {"ok": True}


@app.get("/api/channels/{channel_id}/messages")
async def list_messages(
    channel_id: int,
    before: int | None = None,
    after: int | None = None,
    limit: int = 100,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    channel_or_404(session, channel_id, user)
    query = select(Message).where(Message.channel_id == channel_id)
    if before is not None and after is not None:
        raise HTTPException(422, "Use either before or after")
    if after is not None:
        rows = session.exec(query.where(Message.id > after).order_by(Message.id.asc()).limit(min(limit, 200))).all()
        return serialize_messages(session, rows)
    if before:
        query = query.where(Message.id < before)
    rows = session.exec(query.order_by(Message.id.desc()).limit(min(limit, 200))).all()
    return serialize_messages(session, list(reversed(rows)))


CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


class MessageBody(BaseModel):
    content: str = Field(default="", max_length=8000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    # Optional so existing clients keep creating one message per request.
    client_id: str | None = Field(default=None, max_length=80)


@app.post("/api/channels/{channel_id}/messages")
async def post_message(
    channel_id: int, body: MessageBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    channel_or_404(session, channel_id, user)
    if body.client_id is not None and not CLIENT_ID.fullmatch(body.client_id):
        raise HTTPException(422, "client_id must be 8-80 letters, numbers, underscores, or hyphens")
    if not body.content.strip() and not body.attachment_ids:
        raise HTTPException(422, "Write a message or attach a document")
    # Membership is checked before a retry can return a stored message.
    if body.client_id:
        existing = service.existing_human_message(user, channel_id, body.client_id, body.content.strip(), body.attachment_ids)
        if existing is not None:
            return existing
    from app.api.people import messaging_blocked
    if messaging_blocked(session, channel_id, user.id):
        raise HTTPException(403, "Messaging isn't available for this conversation")
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
        await service.publish_channel(channel_id)
    return await service.post_human_message(user, channel_id, body.content.strip(), body.attachment_ids, body.client_id)


# --- agents ---


AvatarStyle = Literal["teal", "sage", "blue", "plum", "terracotta", "violet"]


class AgentCreateBody(BaseModel):
    name: str = Field(min_length=3, max_length=40)
    band_agent_api_key: SecretStr | None = Field(default=None, min_length=10, max_length=2000)
    role: str = "custom"
    instructions: str = Field(default="", max_length=6000)
    specialty: str = Field(default="", max_length=160)
    avatar: AvatarStyle | None = None
    model: str | None = None
    model_ref: dict | None = None
    runtime: Literal["deepagents", "codex", "claude", "cursor", "openclaw"] = "deepagents"
    connection_id: str | None = None
    channel_id: int | None = None
    # None means every plugin connected in the workspace, including ones connected later.
    plugins: list[str] | None = None


class AgentUpdateBody(BaseModel):
    role: str | None = None
    instructions: str | None = Field(default=None, max_length=6000)
    specialty: str | None = Field(default=None, max_length=160)
    avatar: AvatarStyle | None = None
    model: str | None = None
    model_ref: dict | None = None
    plugins: list[str] | None = None


def _plugins_or_422(slugs: list[str] | None) -> list[str] | None:
    if slugs is None:
        return None
    try:
        return normalize_slugs(slugs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _check_runtime_model(provider: str, model: str | None, *, invalid: str, claude: str) -> None:
    """Connected runtimes take an explicit vendor model ID."""
    if provider == "cursor" and model == "auto-smart":
        raise HTTPException(422, "Choose a specific Cursor model; Router parameters are not supported yet")
    if not model or not MODEL_ID.fullmatch(model):
        raise HTTPException(422, invalid)
    if provider == "claude" and not model.startswith("claude-"):
        raise HTTPException(422, claude)


def _validate_role_and_model(role: str | None, model: str | None) -> None:
    if role is not None and role not in ROLES:
        raise HTTPException(422, f"Unknown role {role!r}")
    if model is not None and model not in {m["id"] for m in model_catalog()}:
        raise HTTPException(422, f"Unknown model {model!r}")


@app.post("/api/agents")
async def create_agent(body: AgentCreateBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if body.runtime != "deepagents":
        if not runtime_available(body.runtime):
            raise HTTPException(409, f"The {body.runtime.title()} runtime is not installed on the server. Ask an admin to complete runtime setup.")
        connection = session.get(AgentConnection, body.connection_id) if body.connection_id else None
        if connection and connection.created_by != user.id:
            raise HTTPException(403, "Choose your own provider connection")
        if connection is None or connection.provider != body.runtime:
            raise HTTPException(422, "Choose an authenticated connection for this agent")
        _check_runtime_model(body.runtime, body.model, invalid="Enter a valid model ID for this provider",
                             claude="Choose a Claude model for the Claude runtime")
        _validate_role_and_model(body.role, None)
    else:
        if body.connection_id:
            raise HTTPException(422, "Use model_ref to select a workspace provider connection")
        from app.providers.bindings import selected
        from app.providers.management import validate_bindings
        ref = body.model_ref or selected(session).get("llm")
        if body.model_ref:
            require_admin(session, user, "Only admins can select workspace provider connections")
            try:
                validate_bindings(session, {"llm": body.model_ref})
            except (ValueError, RuntimeError) as error:
                raise HTTPException(422, str(error)) from None
        if ref and not ref.get("disabled"):
            from app.providers.models import ProviderRevision
            revision = session.get(ProviderRevision, (ref.get("connection_id"), ref.get("revision")))
            chosen = body.model or ref.get("model")
            if not revision or chosen not in revision.config.get("models", []):
                raise HTTPException(422, "Choose an allowed model from the selected provider")
            body.model = chosen
            if chosen != ref.get("model"):
                require_admin(session, user, "Only admins can override the default provider model")
                body.model_ref = {**ref, "model": chosen}
            _validate_role_and_model(body.role, None)
        else:
            body.model = body.model or configuration()[2]
            _validate_role_and_model(body.role, body.model)
    channel_id = body.channel_id
    if channel_id is not None:
        channel_or_404(session, channel_id, user)
        if session.get(DirectMessage, channel_id) or session.get(HumanDirectMessage, channel_id):
            channel_id = None
    if not is_admin(session, user.id) and body.plugins:
        raise HTTPException(403, "Only admins can grant access to workspace apps")
    profile = await service.create_agent(
        name=body.name,
        band_agent_api_key=body.band_agent_api_key.get_secret_value() if body.band_agent_api_key else None,
        role=body.role,
        instructions=body.instructions,
        model=body.model,
        channel_id=channel_id,
        created_by=user.id,
        plugins=_plugins_or_422(body.plugins) if is_admin(session, user.id) else [],
        specialty=body.specialty,
        avatar=body.avatar,
        runtime=body.runtime,
        connection_id=body.connection_id,
    )
    if body.model_ref:
        from app.providers.display import set_agent_model
        try:
            set_agent_model(session, profile.id, body.model_ref, user)
            session.commit()
        except Exception as error:
            session.rollback()
            try:
                await service.delete_agent(profile)
            except Exception:
                logger.exception("Couldn't remove %s after model selection failed", profile.name)
            if isinstance(error, ValueError):
                raise HTTPException(422, str(error)) from None
            raise
    record_human(user, "agent_created", channel_id=channel_id, agent=profile.name, role=profile.role, model=profile.model)
    return service.serialize_agent(profile)


def _require_agent_owner(session: Session, profile: AgentProfile, user: User) -> None:
    owner = agent_owner(session, profile)
    if owner is not None and owner != user.id:
        raise HTTPException(403, "Only this agent's owner can manage or share it")


@app.patch("/api/agents/{agent_id}")
async def update_agent(
    agent_id: int, body: AgentUpdateBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    profile = agent_or_404(session, agent_id)
    _require_agent_owner(session, profile, user)
    runtime = runtime_for(agent_id)
    if runtime and body.model is not None:
        _check_runtime_model(runtime.provider, body.model, invalid="Enter a valid model ID for this agent runtime",
                             claude="Enter a valid model ID for this agent runtime")
    from app.providers.bindings import selected
    from app.providers.display import set_agent_model
    from app.providers.models import ProviderRevision
    if "model_ref" in body.model_fields_set:
        if runtime:
            raise HTTPException(422, "Connected agents retain their personal runtime connection")
        try:
            set_agent_model(session, profile.id, body.model_ref, user)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    managed_ref = body.model_ref or selected(session, profile.id).get("llm", {})
    if body.model is not None and not runtime and managed_ref.get("connection_id"):
        revision = session.get(ProviderRevision, (managed_ref["connection_id"], managed_ref["revision"]))
        allowed = (revision.config or {}).get("models", []) if revision else []
        if revision is None or body.model not in allowed:
            raise HTTPException(422, "Choose an allowed provider model")
        try:
            set_agent_model(session, profile.id, {**managed_ref, "model": body.model}, user)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    _validate_role_and_model(body.role, None if runtime or managed_ref.get("connection_id") else body.model)
    if body.model is not None and body.model != profile.model and agent_owner(session, profile) != user.id:
        require_admin(session, user, "Only workspace admins can change an agent's model")
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
        if agent_owner(session, profile) is not None:
            require_admin(session, user, "Only workspace admins can change access to workspace apps")
        chosen = _plugins_or_422(body.plugins)
        set_plugin_access(profile.id, chosen)
        changed["plugins"] = "all" if chosen is None else ",".join(chosen) or "none"
    if changed:
        record_human(user, "agent_updated", session=session, agent=profile.name, **changed)
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
    require_admin(session, user, "Only workspace admins can connect plugins")
    try:
        url = await authorization_link(slug)
    except ComposioError as exc:
        raise _composio_error(exc) from exc
    record_human(user, "plugin_connected", slug=slug)
    return {"url": url}


@app.delete("/api/plugins/{slug}")
async def remove_plugin(slug: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_admin(session, user, "Only workspace admins can disconnect plugins")
    try:
        await disconnect_plugin(slug)
    except ComposioError as exc:
        raise _composio_error(exc) from exc
    record_human(user, "plugin_disconnected", slug=slug)
    return {"slug": slug, "connected": False}


@app.post("/api/agents/{agent_id}/restart")
async def restart_agent(agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    profile = agent_or_404(session, agent_id)
    _require_agent_owner(session, profile, user)
    await service.start_agent(profile)
    return service.serialize_agent(profile)


@app.post("/api/agents/{agent_id}/dm")
async def open_dm(agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return await service.open_dm(user, agent_or_404(session, agent_id))


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    profile = agent_or_404(session, agent_id)
    _require_agent_owner(session, profile, user)
    if agent_owner(session, profile) is None:
        require_admin(session, user, "Only workspace admins can delete agents")
    name = profile.name
    await service.delete_agent(profile)
    record_human(user, "agent_deleted", agent=name)
    return {"ok": True}


# --- durable task controls ---


@app.get("/api/channels/{channel_id}/tasks")
async def list_tasks(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """The 30 newest tasks plus every unfinished one, so long-running tasks keep their live drafts."""
    channel_or_404(session, channel_id, user)
    newest = session.exec(select(AgentTask).where(AgentTask.channel_id == channel_id,
                         ~AgentTask.source_key.startswith("operator-computer:"))
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
    channel_or_404(session, channel_id, user)
    return JSONResponse(channel_usage(session, channel_id), headers=NO_STORE)


class BudgetBody(BaseModel):
    scope: Literal["agent", "channel"]
    scope_id: int
    daily_tokens: int | None = Field(default=None, ge=1000, le=1_000_000_000)  # None removes the budget


@app.put("/api/budgets")
async def set_budget(body: BudgetBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_admin(session, user, "Only workspace admins can set budgets")
    if body.scope == "channel":
        target, channel_id = f"#{channel_or_404(session, body.scope_id, user).name}", body.scope_id
    else:
        target, channel_id = agent_or_404(session, body.scope_id).name, None
    row = session.get(Budget, (body.scope, body.scope_id))
    result = {"scope": body.scope, "scope_id": body.scope_id, "daily_tokens": body.daily_tokens}
    if (row.daily_tokens if row else None) == body.daily_tokens:
        return result
    if body.daily_tokens is None and row:
        session.delete(row)
    elif body.daily_tokens is not None:
        session.merge(Budget(scope=body.scope, scope_id=body.scope_id, daily_tokens=body.daily_tokens,
                             updated_by=user.id, updated_at=utcnow()))
    record_human(user, "budget_changed", channel_id=channel_id, session=session, target=target,
           daily_tokens=body.daily_tokens or "no limit")
    session.commit()
    await bus.publish({"type": "budget", "scope": body.scope, "scope_id": body.scope_id,
                       "daily_tokens": body.daily_tokens})
    if channel_id is not None:
        limit = f"{body.daily_tokens:,} tokens a day" if body.daily_tokens else "no limit"
        await service.system_message(channel_id, f"{user.display_name} set this channel's token budget to {limit}.")
    return result


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = task_or_404(session, task_id)
    require_task_member(session, task, user)
    return {**serialize_task(task), "result": task.result, "artifacts": task.artifacts,
            "usage": task_usage(session, [task.id]).get(task.id)}


@app.get("/api/tasks/{task_id}/computer")
async def task_computer(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = task_or_404(session, task_id)
    require_task_member(session, task, user)
    from app.computer.access import require_computer_owner
    require_computer_owner(session, task, user.id)
    # Task bindings preserve historical machines when the workspace sharing mode changes.
    row = sandbox_record(computer_key(task)) or sandbox_record(task_id)
    display = display_state(computer_key(task))
    return JSONResponse({
        "control": _control_json(active_control(computer_key(task)), user),
        "configured": configured(provider_for(task), config_for_task(task, settings, safe=True)),
        "provider": provider_for(task),
        "shared": is_shared(computer_key(task)),
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
        "dashboard_url": dashboard(provider_for(task)),
    }, headers=NO_STORE)


@app.get("/api/agents/{agent_id}/computers")
def agent_computers(agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Discover this agent's computers only in channels the caller belongs to. Never starts one."""
    profile = agent_or_404(session, agent_id)
    if agent_owner(session, profile) not in {None, user.id}:
        raise HTTPException(403, "Only this agent's owner can access its private computer")
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
        provider = scope_provider(session, profile, channel.id)
        key = scope_computer_key(session, profile, channel.id, settings)
        # Historical tasks keep their old machine; the profile shows the selected computer.
        task = session.exec(select(AgentTask).join(TaskComputerBinding, TaskComputerBinding.task_id == AgentTask.id)
            .where(AgentTask.agent_id == agent_id, AgentTask.channel_id == channel.id,
                   TaskComputerBinding.provider == provider, TaskComputerBinding.computer_key == key)
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
        row = (sandbox_record(computer_key(task)) or sandbox_record(task.id)) if task else None
        working = session.exec(select(AgentTask.id).where(AgentTask.agent_id == agent_id,
            AgentTask.channel_id == channel.id, AgentTask.status == "running")).first()
        scopes.append({"channel_id": channel.id, "channel_name": channel.name,
                       "is_dm": session.get(DirectMessage, channel.id) is not None,
                       "provider": provider, "configured": configured(provider, scope_config(session, profile, channel.id, settings, safe=True)),
                       "shared": is_shared(key), "computer_key": key,
                       "task_id": task.id if task else None, "status": row.status if row else "not_started",
                       "updated_at": row.updated_at.isoformat() if row else None,
                       "agent_working": running_on(key) if is_shared(key) else working is not None,
                       "can_control": allowed(session, user.id, channel.id, "computer_control")})
    return JSONResponse({"configured": any(scope["configured"] for scope in scopes)
        if scopes else configured(settings.computer_provider, settings), "computers": scopes}, headers=NO_STORE)


@app.get("/api/channels/{channel_id}/computers")
async def channel_computers(channel_id: int, user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """Each agent's computer in this channel, without screenshots, so clients can poll it cheaply."""
    channel_or_404(session, channel_id, user)
    computers = []
    for agent_id in session.exec(select(AgentTask.agent_id).where(AgentTask.channel_id == channel_id).distinct()).all():
        profile = session.get(AgentProfile, agent_id)
        if not profile:
            continue
        key = scope_computer_key(session, profile, channel_id, settings)
        from app.computer.identity import is_user_desktop
        if is_user_desktop(key) and session.get(DirectMessage, channel_id) is None:
            continue  # User computers are accessed privately from the owner's agent profile.
        if agent_owner(session, profile) not in {None, user.id}:
            continue
        latest = session.exec(select(AgentTask).join(TaskComputerBinding, TaskComputerBinding.task_id == AgentTask.id)
                              .where(AgentTask.channel_id == channel_id, AgentTask.agent_id == agent_id,
                                     TaskComputerBinding.computer_key == key)
                              .order_by(AgentTask.created_at.desc())).first()
        working = session.exec(select(AgentTask).where(AgentTask.channel_id == channel_id,
                               AgentTask.agent_id == agent_id, AgentTask.status == "running")
                               .order_by(AgentTask.created_at.desc())).first()
        computer_working = working is not None and used_computer(session, working.id)
        current = working if computer_working else latest
        if current is None:
            continue
        row = sandbox_record(computer_key(current))
        if row is None:
            continue
        computers.append({"agent_id": agent_id, "task_id": current.id, "computer_key": computer_key(current), "status": row.status,
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
    task = task_or_404(session, task_id)
    require_task_member(session, task, user)
    from app.computer.access import require_computer_owner
    require_computer_owner(session, task, user.id)
    return task


def _agent_name(session: Session, task: AgentTask) -> str:
    agent = session.get(AgentProfile, task.agent_id)
    return agent.name if agent else "the agent"


def _computer_name(session: Session, task: AgentTask) -> str:
    return "the workspace computer" if is_shared(computer_key(task)) else f"{_agent_name(session, task)}'s computer"


def _computer_notice_visible(session, task):
    from app.computer.identity import is_user_desktop
    return not is_user_desktop(computer_key(task)) or session.get(DirectMessage, task.channel_id) is not None


async def _on_computer(task: AgentTask, operation):
    """Run a person's request on the agent's computer without stopping it afterwards."""
    computer = computer_factory(task)
    try:
        return await operation(computer)
    except (ComputerUnavailable, ProviderError) as exc:
        raise HTTPException(409, str(exc))
    except (SandboxException, AuthenticationException, ServiceBusyException, ComputerProviderError) as exc:
        # E2B errors can include request details; show only the type.
        logger.warning("Computer request for task %s failed: %s", task.id, type(exc).__name__)
        raise HTTPException(502, f"Computer request failed ({type(exc).__name__})")
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
    key = computer_key(task)
    async def connect(computer):
        view = await computer.live_view(user_id=user.id)
        if body.control:
            # Wait for any in-flight agent command before granting interactive VNC.
            async with computer.lock:
                take_control(key, user_id=user.id, user_name=user.display_name, task_id=task.id)
        return view
    view = await _on_computer(task, connect)
    if body.control:
        control = active_control(key)
        record_human(user, "computer_taken_over", channel_id=task.channel_id, task_id=task.id,
               agent=_agent_name(session, task))
        waiting = "Agents wait" if is_shared(key) else "The agent waits"
        if _computer_notice_visible(session, task):
            await service.system_message(task.channel_id, f"{user.display_name} took control of {_computer_name(session, task)}. "
                                                          f"{waiting} until it is handed back.")
    else:
        control = active_control(key)
    return JSONResponse({**view, "control": _control_json(control, user)}, headers=NO_STORE)


@app.delete("/api/tasks/{task_id}/computer/control")
async def computer_release(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    _require_computer_control(session, task, user)
    if active_control(computer_key(task)):
        release_control(computer_key(task), user_name=user.display_name)
        record_human(user, "computer_handed_back", channel_id=task.channel_id, task_id=task.id,
               agent=_agent_name(session, task))
        if _computer_notice_visible(session, task):
            await service.system_message(task.channel_id, f"{user.display_name} handed {_computer_name(session, task)} back.")
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
    record_human(user, "computer_download", channel_id=task.channel_id, task_id=task.id, path=path, bytes=len(content))
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", path.rstrip("/").rsplit("/", 1)[-1]) or "download"
    return Response(content, media_type="application/octet-stream",
                    headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/tasks/{task_id}/computer/files")
async def computer_upload(task_id: str, name: str, request: Request, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    content = await read_limited(request, TRANSFER_LIMIT, "Uploads support files up to 25 MB")
    path = await _on_computer(task, lambda computer: computer.upload(name, content))
    record_human(user, "computer_upload", channel_id=task.channel_id, task_id=task.id, path=path, bytes=len(content))
    # Agents read the channel, so this tells them where the file is.
    if _computer_notice_visible(session, task):
        await service.system_message(task.channel_id, f"{user.display_name} uploaded {name} to "
                                                      f"{_computer_name(session, task)} at {path}.")
    return {"path": path}


@app.post("/api/tasks/{task_id}/computer/stop")
async def computer_stop(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    _require_computer_control(session, task, user)
    if running_on(computer_key(task)):
        raise HTTPException(409, "The agent is using this computer. Cancel its task first.")
    computer = computer_factory(task)
    try:
        await computer.close(force=True)
    except (ComputerUnavailable, ProviderError) as exc:
        raise HTTPException(409, str(exc)) from None
    release_control(computer_key(task), user_name=user.display_name)
    record_human(user, "computer_stopped", channel_id=task.channel_id, task_id=task.id, agent=_agent_name(session, task))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = task_or_404(session, task_id)
    require_task_member(session, task, user)
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
    record_human(user, "task_cancelled", channel_id=task.channel_id, task_id=task_id, session=session,
           **({"approvals_denied": len(closed)} if closed else {}))
    session.commit()
    session.expire_all()
    if closed:
        await service.publish_approvals(closed)
    await service.publish_inputs(task_id)
    return serialize_task(session.get(AgentTask, task_id))


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = task_or_404(session, task_id)
    require_task_member(session, task, user)
    if task.status not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled tasks can be retried")
    from app.persistence.models import TaskModel
    pinned = session.get(TaskModel, task_id)
    runtime = runtime_for(task.agent_id)
    from app.providers.models import TaskServiceBinding, ProviderBlock
    saved_services = session.get(TaskServiceBinding, task.id)
    saved_connected = saved_services and saved_services.bindings.get("llm", {}).get("connected")
    if not saved_connected and pinned and pinned.provider in CONNECTED_PROVIDERS and (not runtime or runtime.provider != pinned.provider):
        raise HTTPException(409, "This task's original runtime is no longer available")
    services = session.get(TaskServiceBinding, task_id)
    managed = services and services.bindings.get("llm", {}).get("connection_id") and not services.bindings.get("llm", {}).get("connected")
    if (services and services.bindings.get("llm", {}).get("unresolved")) or (pinned and pinned.provider not in {"openai", *CONNECTED_PROVIDERS} and not managed):
        raise HTTPException(409, "The original provider binding is ambiguous. Its checkpoint is preserved. Explicitly start a linked new task with current providers.")
    session.exec(update(AgentTask).where(AgentTask.id == task_id, AgentTask.status.in_(["failed", "cancelled"]))
                 .values(status="queued", cancel_requested=False, error=None,
                         progress="Queued to resume saved progress", updated_at=utcnow()))
    if blocked := session.get(ProviderBlock, task_id):
        session.delete(blocked)
    record_human(user, "task_retried", channel_id=task.channel_id, task_id=task_id, session=session)
    session.commit()
    session.expire_all()
    await service.publish_inputs(task_id)
    return serialize_task(session.get(AgentTask, task_id))


@app.get("/api/channels/{channel_id}/approvals")
async def list_approvals(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    channel_or_404(session, channel_id, user)
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
    from app.persistence.models import ComputerPublishApproval
    private = session.get(ComputerPublishApproval, approval_id)
    if private and private.user_id != user.id:
        raise HTTPException(403, "Only the computer owner can approve publishing a private file")
    require_member(session, approval.channel_id, user, "Join the channel to decide its approvals")
    from app.computer.access import require_computer_owner
    task = session.get(AgentTask, approval.task_id)
    if task is not None:
        require_computer_owner(session, task, user.id)
    # A private export names its owner. Channel approver policy still gates every other request.
    if not private and not allowed(session, user.id, approval.channel_id, "approvers"):
        raise HTTPException(403, "Only workspace admins can decide approvals in this channel")
    decided = decide_approval(approval_id, approve=body.approve, decided_by=user.display_name, note=body.note)
    if decided is None:
        raise HTTPException(409, "This request was already decided or its task is no longer waiting")
    verdict = "approved" if body.approve else "denied"
    record_human(user, f"approval_{verdict}", channel_id=decided.channel_id, task_id=decided.task_id,
           action=decided.action, note=body.note)
    await service.publish_approvals([approval_id])
    await service.system_message(decided.channel_id, f"{user.display_name} {verdict}: {decided.action}"
                                 + (f" ({body.note})" if body.note else ""))
    return decided


@app.get("/api/channels/{channel_id}/schedules")
async def list_schedules(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    channel_or_404(session, channel_id, user)
    return [serialize_schedule(s) for s in channel_schedules(channel_id)]


@app.delete("/api/channels/{channel_id}/schedules/{schedule_id}")
async def delete_schedule(channel_id: int, schedule_id: int, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    require_member(session, channel_id, user, "Join the channel to manage its schedules")
    cancelled = cancel_schedule(channel_id, schedule_id)
    if cancelled is None:
        raise HTTPException(404, "Schedule not found")
    record_human(user, "schedule_cancelled", channel_id=channel_id, schedule=describe(cancelled), prompt=cancelled.prompt)
    await service.system_message(channel_id, f"{user.display_name} cancelled a schedule ({describe(cancelled)}): "
                                             f"{cancelled.prompt[:200]}")
    return {"ok": True}


@app.get("/api/channels/{channel_id}/agents/{agent_id}/memories")
async def list_memories(channel_id: int, agent_id: int, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    channel_or_404(session, channel_id, user)
    return session.exec(select(AgentMemory).where(AgentMemory.agent_id == agent_id,
                        AgentMemory.channel_id == channel_id)).all()


@app.delete("/api/channels/{channel_id}/agents/{agent_id}/memories/{key}")
async def forget_memory(channel_id: int, agent_id: int, key: str, user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    require_member(session, channel_id, user, "Join the channel to manage its memories")
    memory = session.get(AgentMemory, (agent_id, channel_id, key))
    if memory is None:
        raise HTTPException(404, "Memory not found")
    session.delete(memory)
    record_human(user, "memory_forgotten", channel_id=channel_id, session=session, agent_id=agent_id, key=key)
    session.commit()
    return {"ok": True}


# --- realtime ---


@app.websocket("/ws")
async def websocket(ws: WebSocket, token: str = ""):
    with db() as session:
        principal = authenticate(token, session) if token else None
    if principal is None:
        await ws.close(code=4401)
        return
    user, _session_id = principal
    await ws.accept()
    await bus.connect(user.id, ws)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=5)
            except asyncio.TimeoutError:
                with db() as session:
                    if authenticate(token, session) is None:
                        await ws.close(code=4401)
                        return
    except WebSocketDisconnect:
        pass
    finally:
        await bus.disconnect(user.id, ws)


@app.get("/api/health")
async def health():
    # Personal Band failures do not make the shared API unhealthy.
    # Authenticated bootstrap reports the requesting user's connection state.
    with db() as session:
        configured = session.exec(select(UserBandConnection.user_id)).first() is not None
    online = service.platform_online()
    return {"ok": True, "band": {"configured": configured, "online": online}}


@app.get("/api/status")
def workspace_status(user: User = Depends(current_user)):
    return service_status(user)


def mount_frontend(application: FastAPI, directory: Path) -> None:
    directory = directory.resolve()
    application.mount("/assets", StaticFiles(directory=directory / "assets"), name="assets")

    @application.get("/{path:path}")
    async def spa(path: str):
        if path == "api" or path.startswith("api/") or path == "auth" or path.startswith("auth/"):
            raise HTTPException(404, "API endpoint not found. Check that the API and frontend versions match.")
        candidate = (directory / path).resolve()
        if not candidate.is_relative_to(directory):
            raise HTTPException(404, "File not found")
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(directory / "index.html", headers={"Cache-Control": "no-cache"})


if FRONTEND_DIST.exists():
    mount_frontend(app, FRONTEND_DIST)
