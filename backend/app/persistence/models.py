from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column, LargeBinary, UniqueConstraint, event
from sqlalchemy.orm import Session as SASession
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlmodel import Field, Session, SQLModel, create_engine, select

from app.config import is_postgres, settings, sqlalchemy_url


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Treat a naive stored timestamp as UTC; SQLite returns naive datetimes."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class Channel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    topic: str = ""
    band_room_id: str | None = Field(default=None, index=True)
    created_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)


class DirectMessage(SQLModel, table=True):
    """Marks a channel as one person's private conversation with one agent."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # Cleared when the agent is deleted: SQLite can reuse its row id for a future agent.
    agent_id: int | None = Field(default=None, index=True)


class ChannelMember(SQLModel, table=True):
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    member_type: str = Field(primary_key=True)  # "human" | "agent"
    member_id: int = Field(primary_key=True)


class UserBandAccount(SQLModel, table=True):
    """A user's encrypted Band credential for native agent management; local chat is independent."""
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    band_user_id: str | None = Field(default=None, unique=True)
    secret_enc: str = ""
    handle: str = ""
    name: str = ""


class UserBandConnection(SQLModel, table=True):
    """A person's Agent API credential, separate from local AI teammate profiles."""
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    band_agent_id: str = Field(unique=True, index=True)
    secret_enc: str
    name: str = ""
    handle: str = ""
    verified_at: datetime = Field(default_factory=utcnow)


class PrivateConversation(SQLModel, table=True):
    """New conversations use explicit membership; legacy workspace channels stay shared."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    owner_id: int = Field(foreign_key="user.id")
    kind: str = "channel"
    title: str = ""


class HumanDirectMessage(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("first_user_id", "second_user_id"),)
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    first_user_id: int = Field(foreign_key="user.id")
    second_user_id: int = Field(foreign_key="user.id")


class ChannelInvitation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    inviter_id: int = Field(foreign_key="user.id")
    recipient_id: int = Field(foreign_key="user.id", index=True)
    status: str = "pending"
    created_at: datetime = Field(default_factory=utcnow)


class AppContact(SQLModel, table=True):
    first_user_id: int = Field(foreign_key="user.id", primary_key=True)
    second_user_id: int = Field(foreign_key="user.id", primary_key=True)


class AppContactRequest(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("requester_id", "recipient_id"),)
    id: str = Field(primary_key=True)
    requester_id: int = Field(foreign_key="user.id", index=True)
    recipient_id: int = Field(foreign_key="user.id", index=True)
    message: str = ""
    status: str = "pending"
    created_at: datetime = Field(default_factory=utcnow)


class BandRoomAccount(SQLModel, table=True):
    """Pins existing rooms to the account that created them, even after a personal account is linked."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    band_user_id: str
    user_id: int | None = Field(default=None, foreign_key="user.id")


class BandPlatformRoom(SQLModel, table=True):
    """Pin a room to one user's Agent API identity, independently of Enterprise rooms."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    band_agent_id: str


class PlatformTeammate(SQLModel, table=True):
    """An app teammate using its owner's Band platform connection for transport."""
    agent_id: int = Field(foreign_key="agentprofile.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    band_agent_id: str


class BandAgentOwner(SQLModel, table=True):
    agent_id: int = Field(foreign_key="agentprofile.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    band_user_id: str


class LocalTeammate(SQLModel, table=True):
    """An app-owned teammate; no native Band registration or private SDK listener."""
    agent_id: int = Field(foreign_key="agentprofile.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)


class BandAgentLink(SQLModel, table=True):
    """An existing Band identity connected with its own key, never registered or deleted here."""
    agent_id: int = Field(foreign_key="agentprofile.id", primary_key=True)


class AgentProfile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    handle: str = ""
    role: str = "custom"
    instructions: str = ""
    model: str = settings.default_model
    band_agent_id: str = Field(index=True, unique=True)
    band_api_key_enc: str
    is_master: bool = False
    created_by: int | None = Field(default=None, foreign_key="user.id")
    created_by_agent: int | None = None
    created_at: datetime = Field(default_factory=utcnow)


class AgentPersona(SQLModel, table=True):
    """Optional identity details, separate so existing agent databases need no column migration."""
    agent_id: int = Field(foreign_key="agentprofile.id", primary_key=True)
    specialty: str = ""
    avatar: str | None = None


class AgentConnection(SQLModel, table=True):
    """A workspace runtime credential; never included in agent/bootstrap payloads."""
    id: str = Field(primary_key=True)
    provider: str
    label: str
    auth_method: str = "api_key"
    secret_enc: str
    models: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)


class AgentRuntime(SQLModel, table=True):
    """Separate table keeps existing agent databases migration-free."""
    agent_id: int = Field(foreign_key="agentprofile.id", primary_key=True)
    connection_id: str = Field(foreign_key="agentconnection.id", index=True)
    provider: str


class AgentOAuthAttempt(SQLModel, table=True):
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    state_hash: str = Field(unique=True, index=True)
    verifier_enc: str
    nonce: str
    redirect_uri: str
    status: str = "pending"
    connection_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class AgentHost(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    host_id: str


class SkillDefinition(SQLModel, table=True):
    """A reusable workspace skill. Supporting files stay in virtual storage."""
    id: str = Field(primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str
    instructions: str
    files: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    builtin: bool = False
    created_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AgentSkill(SQLModel, table=True):
    agent_id: int = Field(foreign_key="agentprofile.id", primary_key=True)
    skill_id: str = Field(foreign_key="skilldefinition.id", primary_key=True)
    enabled: bool = True


class AgentUISkills(SQLModel, table=True):
    """Legacy rollout marker, retained for existing databases; no longer installs skills."""
    agent_id: int = Field(foreign_key="agentprofile.id", primary_key=True)


class SandboxInteraction(SQLModel, table=True):
    """An explicitly confirmed widget follow-up and its atomically enqueued task."""
    key: str = Field(primary_key=True)
    component_id: int = Field(index=True)
    user_id: int
    text: str
    message_id: int
    task_id: str
    created_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    author_type: str  # "human" | "agent" | "system" | "external"
    author_id: int | None = None
    author_name: str
    content: str
    mentions: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    to_humans: bool = True
    band_message_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class ClientMessage(SQLModel, table=True):
    """Idempotency record for one human send, scoped to that sender and conversation.

    A new table keeps existing message rows unchanged. Clients that omit client_id keep the previous behavior.
    """

    __tablename__ = "client_message"
    __table_args__ = (UniqueConstraint("user_id", "channel_id", "client_id"),)
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    client_id: str
    message_id: int = Field(foreign_key="message.id", index=True)
    payload_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class SyncCursor(SQLModel, table=True):
    """One monotonic counter so a bootstrap snapshot can be ordered against live events."""

    __tablename__ = "sync_cursor"
    id: int = Field(primary_key=True)
    value: int = 0


class AgentTask(SQLModel, table=True):
    id: str = Field(primary_key=True)
    source_key: str = Field(unique=True, index=True)
    channel_id: int = Field(index=True)
    agent_id: int = Field(index=True)
    room_id: str
    prompt: str
    context: str = ""
    reply_to: str = ""
    status: str = Field(default="queued", index=True)
    progress: str = "Waiting for worker"
    result: str = ""
    artifacts: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    error: str | None = None
    attempts: int = 0
    cancel_requested: bool = False
    delivery_status: str = "pending"
    reply_message_id: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class MessageDraft(SQLModel, table=True):
    """The reply an agent is still writing; removed when the task's final message is committed."""
    task_id: str = Field(primary_key=True)
    channel_id: int = Field(index=True)
    agent_id: int
    content: str
    updated_at: datetime = Field(default_factory=utcnow)


class MessageComponent(SQLModel, table=True):
    """A native output or input card attached to a reply; linked when the reply is committed."""
    __table_args__ = (UniqueConstraint("task_id", "tool_call_id"),)
    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    tool_call_id: str
    # Keyed by task, not band_message_id: delivery replaces that with the Band message id.
    message_id: int | None = Field(default=None, index=True)
    kind: str  # "table" | "chart" | "spec" | "form" (legacy) | "view" | "ui" | "input"
    props: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class SandboxRenderCall(SQLModel, table=True):
    """Applied draft revisions; replaying an older call must not undo a newer one."""
    task_id: str = Field(primary_key=True)
    tool_call_id: str = Field(primary_key=True)
    component_id: int = 0


class ComponentResponse(SQLModel, table=True):
    """One shared response per input card, committed atomically with its human message and task transition."""
    component_id: int = Field(primary_key=True, foreign_key="messagecomponent.id")
    user_id: int = Field(foreign_key="user.id")
    user_name: str
    action_id: str
    label: str
    values: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    message_id: int | None = Field(default=None, foreign_key="message.id")
    created_at: datetime = Field(default_factory=utcnow)


class TaskInput(SQLModel, table=True):
    """A durable clarification, keyed by the LangGraph interrupt, linked to its shared UI card."""
    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    component_id: int = Field(unique=True, index=True)
    status: str = "pending"  # pending | answered


class ComposioSession(SQLModel, table=True):
    """The workspace's reusable Composio Tool Router session. The project key stays in the environment."""
    id: int | None = Field(default=None, primary_key=True)
    user_id: str
    session_id: str


class AgentPluginAccess(SQLModel, table=True):
    """Apps one agent may use. No row means every plugin connected in the workspace."""
    agent_id: int = Field(primary_key=True)
    slugs: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class AgentMemory(SQLModel, table=True):
    agent_id: int = Field(primary_key=True)
    channel_id: int = Field(primary_key=True)
    key: str = Field(primary_key=True)
    content: str
    source_task_id: str
    updated_at: datetime = Field(default_factory=utcnow)


class TaskSandbox(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    name: str = Field(unique=True)
    sandbox_id: str | None = None
    status: str = "provisioning"
    latest_image: str = ""
    frame_id: str = ""
    width: int = 0
    height: int = 0
    last_action: str = ""
    error: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class ComputerViewLease(SQLModel, table=True):
    """Keep a computer available while a human's short-lived viewer is open."""
    key: str = Field(primary_key=True)
    until: datetime


class ComputerConnection(SQLModel, table=True):
    """Private E2B ingress credentials used only by the authenticated desktop proxy."""
    sandbox_id: str = Field(primary_key=True)
    host: str
    access_token_enc: str


class ComputerScope(SQLModel, table=True):
    """The selected provider for future tasks; scope includes the immutable Band identity."""
    key: str = Field(primary_key=True)
    provider: str


class ComputerControlContext(SQLModel, table=True):
    """The channel where a workspace computer's human take-over was requested."""
    key: str = Field(primary_key=True)
    task_id: str


class TaskComputerBinding(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    provider: str
    computer_key: str = Field(index=True)


class ComputerDisk(SQLModel, table=True):
    """A durable Tenki data volume. Removing a VM never deletes this disk."""
    key: str = Field(primary_key=True)
    name: str = Field(unique=True)
    volume_id: str | None = None
    state: str = "new"


class UserDesktop(SQLModel, table=True):
    """One agent display inside its owner's VM; screens never share frame/input state."""
    __table_args__ = (UniqueConstraint("host_key", "display"),)
    key: str = Field(primary_key=True)
    host_key: str = Field(index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    agent_identity: str
    display: int


class TaskPrincipal(SQLModel, table=True):
    """Immutable originating app user. Missing identity never grants private computer access."""
    task_id: str = Field(primary_key=True)
    user_id: int | None = None


class ComputerPublishApproval(SQLModel, table=True):
    approval_id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id")


class ComputerGateway(SQLModel, table=True):
    key: str = Field(primary_key=True)
    sandbox_id: str
    secret_enc: str


class ComputerEndpoint(SQLModel, table=True):
    key: str = Field(primary_key=True)
    port: int = Field(primary_key=True)
    sandbox_id: str
    endpoint_enc: str
    expires_at: datetime


class AppPreview(SQLModel, table=True):
    """A published app port pinned to one owned computer; never an arbitrary URL."""
    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    channel_id: int = Field(index=True)
    agent_id: int
    computer_key: str = Field(index=True)
    sandbox_id: str
    port: int
    title: str
    path: str = "/"
    request_host: str
    created_at: datetime = Field(default_factory=utcnow)


class AppPreviewLease(SQLModel, table=True):
    """An expiring viewer capability. Only a hash of the hostname secret is stored."""
    id: str = Field(primary_key=True)
    preview_id: str = Field(foreign_key="apppreview.id", index=True)
    user_id: int
    token_hash: str = Field(unique=True, index=True)
    workspace_origin: str
    until: datetime


class ComputerDisplay(SQLModel, table=True):
    """Bounded display metadata kept separately so existing sandbox records need no migration."""
    key: str = Field(primary_key=True)
    url: str = ""
    network_warning: str = ""
    observed_at: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))


class TaskHandoff(SQLModel, table=True):
    """Links a task to the teammate hand-off chain that created it, without new AgentTask columns."""
    task_id: str = Field(primary_key=True)
    root_task_id: str = Field(index=True)
    parent_task_id: str
    from_agent_id: int
    kind: str  # "handoff": a teammate's assigned task; "report": the follow-up returned to the requester
    request: str = ""
    report_back: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class TeammateRequest(SQLModel, table=True):
    """An agent asked for a visible teammate; the API process owns its creation."""
    id: int | None = Field(default=None, primary_key=True)
    source_key: str = Field(unique=True, index=True)  # requesting task + tool call, for replay-safe creation
    task_id: str
    channel_id: int
    requested_by_agent_id: int
    name: str
    role: str
    instructions: str = ""
    model: str
    status: str = Field(default="pending", index=True)  # pending | creating | created | failed
    agent_id: int | None = None
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class OutsideAgent(SQLModel, table=True):
    """A Band agent this app doesn't run (e.g. Claude Code or Codex) that people invited into a channel."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    band_id: str = Field(primary_key=True)
    handle: str
    name: str
    description: str = ""
    added_by: int | None = None
    created_at: datetime = Field(default_factory=utcnow)


class OutsideHandoff(SQLModel, table=True):
    """A request an agent posted to an outside agent through Band; sent at most once."""
    source_key: str = Field(primary_key=True)  # requesting task + tool call
    task_id: str = Field(index=True)
    channel_id: int
    band_id: str
    request: str
    status: str = "sending"  # sending | sent | unknown
    band_message_id: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ComputerControl(SQLModel, table=True):
    """A person has taken over an agent's computer; agent input actions wait until it ends."""
    key: str = Field(primary_key=True)  # computer key, see computer.computer_key
    user_id: int
    user_name: str
    until: datetime


class TaskApproval(SQLModel, table=True):
    """An agent action paused for a human decision. The id is the LangGraph interrupt id."""
    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    channel_id: int = Field(index=True)
    agent_id: int
    action: str
    details: str = ""
    status: str = Field(default="pending", index=True)  # pending | approved | denied
    decided_by: str = ""
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None


class Schedule(SQLModel, table=True):
    """A recurring or one-off request that the worker turns into an agent task when due."""
    id: int | None = Field(default=None, primary_key=True)
    source_key: str = Field(unique=True, index=True)  # creating task + tool call, for replay-safe creation
    channel_id: int = Field(index=True)
    agent_id: int = Field(index=True)
    room_id: str
    reply_to: str = ""
    prompt: str
    cadence: str  # once | daily | weekdays | weekly | interval
    time_of_day: str = ""  # HH:MM in `timezone`
    weekday: int | None = None  # 0 = Monday, for weekly
    run_date: str = ""  # YYYY-MM-DD, for once
    interval_minutes: int = 0
    timezone: str = "UTC"
    next_run_at: datetime | None = Field(default=None, index=True)
    enabled: bool = Field(default=True, index=True)
    created_by: str = ""
    last_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class WorkspaceRole(SQLModel, table=True):
    """A person's workspace role; people without a row are members. The oldest account becomes the owner."""
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    role: str  # owner | admin
    updated_at: datetime = Field(default_factory=utcnow)


class ChannelPolicy(SQLModel, table=True):
    """Who may act for a channel's agents; channels without a row let any member act."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    approvers: str = "members"  # members | admins
    computer_control: str = "members"  # members | admins: who may watch, take over, or stop an agent's computer
    updated_by: int | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class AuditEvent(SQLModel, table=True):
    """One thing a person or agent did, in order. Details are short summaries and never hold secrets."""
    __table_args__ = (UniqueConstraint("task_id", "ref"),)
    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    channel_id: int | None = Field(default=None, index=True)  # None for workspace-wide events
    task_id: str | None = Field(default=None, index=True)
    actor_type: str  # human | agent | system
    actor_id: int | None = None
    actor_name: str
    action: str
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    ref: str | None = None  # e.g. the tool call id, so a replayed call is recorded once per task


class TaskUsage(SQLModel, table=True):
    """Tokens one model call used; keyed by message id so replayed or re-streamed messages count once."""
    __table_args__ = (UniqueConstraint("task_id", "message_id"),)
    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    channel_id: int = Field(index=True)
    agent_id: int = Field(index=True)
    message_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    at: datetime = Field(default_factory=utcnow, index=True)


class TaskCacheUsage(SQLModel, table=True):
    """The prompt-cache share of a TaskUsage row's input tokens, which is priced at the cache rates."""
    usage_id: int = Field(primary_key=True)  # TaskUsage.id
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class OpenAISettings(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    api_key_enc: str = ""
    models: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    default_model: str = ""


class TaskModel(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    model: str
    provider: str = "openai"


class DocumentCollection(SQLModel, table=True):
    channel_id: int = Field(primary_key=True)
    vector_store_id: str = ""


class DocumentBlob(SQLModel, table=True):
    """Original file bytes. Stored with the database so a redeploy does not drop uploads."""
    document_id: str = Field(primary_key=True)
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))


class Document(SQLModel, table=True):
    id: str = Field(primary_key=True)
    channel_id: int = Field(index=True)
    uploaded_by: int
    name: str
    media_type: str
    size: int
    status: str = Field(default="staged", index=True)
    error: str = ""
    file_id: str = ""
    index_file_id: str = ""
    units: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MessageAttachment(SQLModel, table=True):
    message_id: int = Field(primary_key=True, foreign_key="message.id")
    document_id: str = Field(primary_key=True, foreign_key="document.id")


class TaskDocument(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    document_id: str = Field(primary_key=True)


class TaskDocumentSource(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    document_id: str = Field(primary_key=True)


class ImageGeneration(SQLModel, table=True):
    """Claim paid image calls before dispatch; uncertain outcomes must never blindly regenerate."""
    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    status: str = "pending"
    document_id: str = ""
    model: str
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ImageGenerationDisplay(SQLModel, table=True):
    """Bounded display metadata for in-flight images, separate from paid-call receipts."""
    id: str = Field(primary_key=True)
    title: str
    size: str


class DocumentUsage(SQLModel, table=True):
    id: str = Field(primary_key=True)
    channel_id: int = Field(index=True)
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    at: datetime = Field(default_factory=utcnow, index=True)


class Budget(SQLModel, table=True):
    """A daily token limit (UTC day) for one agent or one channel."""
    scope: str = Field(primary_key=True)  # agent | channel
    scope_id: int = Field(primary_key=True)
    daily_tokens: int
    updated_by: int | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class AuthSession(SQLModel, table=True):
    """A revocable login. The browser token is useless once this row is revoked or expired."""
    __tablename__ = "auth_session"
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    expires_at: datetime
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class RecoveryAddress(SQLModel, table=True):
    """A verified email is the only recovery channel. Unverified addresses cannot reset a password."""
    __tablename__ = "recovery_address"
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    email: str = Field(index=True)
    verified_at: datetime | None = None
    verify_hash: str | None = None
    verify_expires_at: datetime | None = None


class PasswordReset(SQLModel, table=True):
    __tablename__ = "password_reset"
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    token_hash: str = Field(unique=True)
    expires_at: datetime
    used_at: datetime | None = None


class ContactBlock(SQLModel, table=True):
    """One direction. Shared-channel membership and message history are left alone."""
    __tablename__ = "contact_block"
    blocker_id: int = Field(foreign_key="user.id", primary_key=True)
    blocked_id: int = Field(foreign_key="user.id", primary_key=True)
    created_at: datetime = Field(default_factory=utcnow)


class WorkerHeartbeat(SQLModel, table=True):
    __tablename__ = "worker_heartbeat"
    id: int = Field(default=1, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow)
    beat_at: datetime = Field(default_factory=utcnow)
    status: str = "idle"


class ConnectionHealth(SQLModel, table=True):
    """Operator-facing provider state. Detail is a short code, never a raw provider body."""
    __tablename__ = "connection_health"
    connection_id: str = Field(foreign_key="agentconnection.id", primary_key=True)
    state: str = "ready"
    detail: str = ""


import app.providers.models  # noqa: F401  (register provider tables)


_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if _sqlite else {}
engine_kwargs: dict[str, Any] = {"connect_args": connect_args}
if is_postgres(settings.database_url):
    # Two processes share a small development database, so keep the pools narrow.
    engine_kwargs.update(pool_pre_ping=True, pool_size=2, max_overflow=2)
engine = create_engine(sqlalchemy_url(settings.database_url), **engine_kwargs)


if _sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def insert_ignore(session, model, index_elements: list[str], **values) -> None:
    """Insert a row unless one with the same key exists, even if another process inserts it first."""
    dialect = session.get_bind().dialect.name
    insert = {"sqlite": sqlite_insert, "postgresql": postgres_insert}.get(dialect)
    if insert is None:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
    session.connection().execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=index_elements))


def bind_computer(session, task: AgentTask, *, legacy: bool = False) -> None:
    if session.get(TaskComputerBinding, task.id):
        return
    profile = session.get(AgentProfile, task.agent_id)
    owner = profile.band_agent_id if profile else f"deleted-agent-{task.agent_id}"
    if profile and as_utc(task.created_at) < as_utc(profile.created_at):
        owner = f"retired-task-{task.id}"
    key = f"agent-{owner}-channel-{task.channel_id}"
    scope = session.get(ComputerScope, key)
    if scope is None:
        provider = "e2b" if legacy else settings.computer_provider
        if provider not in {"e2b", "tenki"}:
            raise ValueError("COMPUTER_PROVIDER must be e2b or tenki")
        insert_ignore(session, ComputerScope, ["key"], key=key, provider=provider)
        scope = session.get(ComputerScope, key)
    provider = "e2b" if legacy else scope.provider
    from app.providers.computers import identity_config
    managed = identity_config(session, task, settings) if not legacy else None
    computer_config = settings
    if managed:
        provider, computer_config = managed
    from app.computer.identity import selected_key
    computer_key = f"{provider}-{key}" if legacy else selected_key(session, owner, task.channel_id, provider, computer_config)
    values = {"task_id": task.id, "provider": provider, "computer_key": computer_key}
    if legacy:
        # API and worker may backfill the same legacy task during startup.
        # Keep the first immutable binding if another process inserted it.
        insert_ignore(session, TaskComputerBinding, ["task_id"], **values)
    else:
        session.add(TaskComputerBinding(**values))


@event.listens_for(SASession, "before_flush")
def _snapshot_task_computer(session, _context, _instances):
    pending = {row.task_id for row in session.new if isinstance(row, TaskComputerBinding)}
    for task in [row for row in session.new if isinstance(row, AgentTask)]:
        if task.id not in pending:
            bind_computer(session, task)


@event.listens_for(SASession, "before_flush")
def _snapshot_task_model(session, _context, _instances):
    """Every task creation path snapshots its model in the same transaction."""
    tasks = [row for row in session.new if isinstance(row, AgentTask)]
    if not tasks:
        return
    configured = session.get(OpenAISettings, 1)
    from app.config import MODELS
    models = configured.models if configured and configured.models else [m["id"] for m in MODELS]
    default = configured.default_model if configured and configured.default_model else settings.default_model
    if default not in models:
        default = models[0]
    existing = {row.task_id for row in session.new if isinstance(row, TaskModel)}
    for task in tasks:
        if task.id in existing or session.get(TaskModel, task.id):
            continue
        profile = session.get(AgentProfile, task.agent_id)
        runtime = session.get(AgentRuntime, task.agent_id)
        from app.providers.bindings import snapshot_task
        services = snapshot_task(session, task).bindings
        selection = services.get("llm", {})
        model = profile.model if profile and (runtime or profile.model in models) else default
        session.add(TaskModel(task_id=task.id, model=selection.get("model") or model,
                              provider=selection.get("provider") or (runtime.provider if runtime else "openai")))


def init_db() -> None:
    from app.persistence.migrate import upgrade_database
    upgrade_database()
    with Session(engine) as session:
        for task in session.exec(select(AgentTask).outerjoin(TaskComputerBinding,
                AgentTask.id == TaskComputerBinding.task_id).where(TaskComputerBinding.task_id == None)).all():
            bind_computer(session, task, legacy=True)
        session.commit()
    from app.providers.bootstrap import bootstrap
    bootstrap()


def db() -> Session:
    return Session(engine, expire_on_commit=False)


def dm_owner(channel_id: int) -> int | None:
    """The user who owns this direct-message channel, or None for a shared channel."""
    with db() as session:
        dm = session.get(DirectMessage, channel_id)
        return dm.user_id if dm else None


import app.providers.document_hooks  # noqa: F401  (register atomic document bindings)
import app.persistence.principals  # noqa: F401  (register immutable task principals)


def get_session():
    with db() as session:
        yield session
