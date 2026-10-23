from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Sequence
from uuid import uuid4
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.band.listener import AgentManager, describe_error, fetch_identity
from app.auth import decrypt_secret, encrypt_secret
from app.band.org import BandOrg
from app.config import settings
from app.agent_connections import runtime_for
from app.persistence.models import AgentRuntime, AgentConnection
from app.openai_provider import configuration
from app.documents import attachments_for, link_attachments
from app.workspace.events import bus
from app.persistence.models import (AgentMemory, AgentPersona, AgentPluginAccess, AgentSkill, AgentUISkills, AgentTask, AgentProfile, Budget, Channel, ChannelMember, ClientMessage, DirectMessage, Message,
                                    MessageComponent, ComponentResponse, MessageDraft, OutsideAgent, Schedule, TaskApproval, TaskSandbox,
                                    ComputerConnection, ComputerDisplay, ComputerViewLease, TeammateRequest, User, db)
from app.computer import CONTROL_MINUTES, expire_controls
from app.persistence.models import (ComputerDisk, ComputerEndpoint, ComputerGateway, ComputerScope, ComputerControl,
                                    ComputerControlContext)
from app.workspace.audit import record
from app.workspace.permissions import policy_of, serialize_policy
from app.runtime.tools.composio import plugin_slugs, set_plugin_access
from app.workspace.roles import MASTER_ROLE, ROLES, role_label
from app.persistence.models import UserBandAccount, PrivateConversation, HumanDirectMessage, BandAgentOwner, BandRoomAccount, ContactBlock
from app.workspace.access import channel_audience, agent_event_visible, agent_owner
from app.workspace.components import current_components
from app.tasks import channel_context, display_names
from app.tasks.teammates import settle_interrupted

logger = logging.getLogger(__name__)

bus.audience = channel_audience
bus.agent_visible = agent_event_visible

# Drafts stream as appended text; every Nth update resends the whole draft so late or lagging clients catch up.
DRAFT_RESYNC = 10


def _js_length(text: str) -> int:
    """String length as the browser counts it (UTF-16 code units), so append offsets line up."""
    return len(text.encode("utf-16-le")) // 2


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def serialize_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "online": user.id in bus.online_user_ids,
    }


def serialize_message(message: Message, components: Sequence[dict[str, Any]] = (),
                      approval: TaskApproval | None = None, attachments=(), artifacts=(),
                      client_id: str | None = None) -> dict[str, Any]:
    payload = {
        "id": message.id,
        "channel_id": message.channel_id,
        "author_type": message.author_type,
        "author_id": message.author_id,
        "author_name": message.author_name,
        "content": message.content,
        "mentions": message.mentions,
        "to_humans": message.to_humans,
        "components": list(components),
        "attachments": list(attachments),
        "artifacts": list(artifacts),
        "approval": {"id": approval.id, "status": approval.status, "action": approval.action,
                     "details": approval.details, "decided_by": approval.decided_by,
                     "note": approval.note} if approval else None,
        "handoff": (message.band_message_id or "").startswith("handoff:"),
        "created_at": message.created_at.isoformat(),
    }
    if client_id:
        payload["client_id"] = client_id
    return payload


def message_digest(content: str, attachment_ids: Sequence[str]) -> str:
    """Stable identity of a send. Attachment order does not change the digest."""
    payload = json.dumps(
        {"attachments": sorted(attachment_ids), "content": content},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def client_message_row(session: Session, user_id: int, channel_id: int, client_id: str) -> ClientMessage | None:
    return session.exec(select(ClientMessage).where(
        ClientMessage.user_id == user_id,
        ClientMessage.channel_id == channel_id,
        ClientMessage.client_id == client_id,
    )).first()


def replay_client_message(session: Session, row: ClientMessage, digest: str) -> dict[str, Any]:
    """Return the original message for this sender and conversation, or reject a conflicting payload.

    The lookup is already scoped to the authenticated user and channel. Callers must enforce visibility
    before this runs so a retried id cannot read another person's message or a channel they left.
    """
    if row.payload_hash != digest:
        raise ServiceError("This message was already sent with different content", 409)
    message = session.get(Message, row.message_id)
    if (message is None or message.channel_id != row.channel_id or message.author_id != row.user_id
            or message.author_type != "human"):
        raise ServiceError("Message not found", 404)
    return serialize_messages(session, [message])[0]


def serialize_messages(session: Session, messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Messages with their attached components and, for approval requests, the decision so far."""
    components = components_for(session, [m.id for m in messages])
    attachments = attachments_for(session, [m.id for m in messages])
    artifacts: dict[int, list[dict[str, Any]]] = {}
    if messages:
        # Deliveries may replace the Band message id; the durable reply link stays stable.
        tasks = session.exec(select(AgentTask.id, AgentTask.reply_message_id, AgentTask.artifacts)
                             .join(Message, (Message.id == AgentTask.reply_message_id)
                                   & (Message.channel_id == AgentTask.channel_id))
                             .where(AgentTask.reply_message_id.in_([m.id for m in messages]))).all()
        for task_id, message_id, files in tasks:
            artifacts.setdefault(message_id, []).extend({"task_id": task_id, "path": item["path"],
                                                        "size": len(item["content"].encode("utf-8"))}
                                                       for item in files)
    approval_ids = {m.id: m.band_message_id.removeprefix("approval:") for m in messages
                    if (m.band_message_id or "").startswith("approval:")}
    approvals = {a.id: a for a in session.exec(
        select(TaskApproval).where(TaskApproval.id.in_(approval_ids.values()))).all()} if approval_ids else {}
    client_ids = {row.message_id: row.client_id for row in session.exec(select(ClientMessage).where(
        ClientMessage.message_id.in_([m.id for m in messages]))).all()} if messages else {}
    return [serialize_message(m, components.get(m.id, ()), approvals.get(approval_ids.get(m.id, "")),
                              attachments.get(m.id, ()), artifacts.get(m.id, ()), client_ids.get(m.id))
            for m in messages]


def components_for(session: Session, message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Native reply components and shared form receipts, in the order the agent made them."""
    found: dict[int, list[dict[str, Any]]] = {}
    if message_ids:
        from app.workspace.generative import response_json
        components = session.exec(select(MessageComponent).where(MessageComponent.message_id.in_(message_ids), current_components())
                                  .order_by(MessageComponent.id)).all()
        responses = {r.component_id: response_json(r) for r in session.exec(select(ComponentResponse).where(
            ComponentResponse.component_id.in_([c.id for c in components if c.kind in {"form", "input"}]))).all()}
        from app.persistence.models import TaskInput
        from app.tasks.inputs import input_state
        requests = {r.component_id: input_state(session, r) for r in session.exec(select(TaskInput).where(
            TaskInput.component_id.in_([c.id for c in components if c.kind == "input"]))).all()}
        for c in components:
            item = {"id": c.id, "kind": c.kind, "props": c.props}
            if c.kind in {"form", "input"}:
                item["response"] = responses.get(c.id)
            if c.kind == "input":
                item["request"] = requests.get(c.id)
            found.setdefault(c.message_id, []).append(item)
    return found


def serialize_outside_agent(outsider: OutsideAgent) -> dict[str, Any]:
    return {
        "band_id": outsider.band_id,
        "handle": outsider.handle,
        "name": outsider.name,
        "description": outsider.description,
    }


def _mentioned(content: str, *names: str) -> bool:
    return any(
        name and re.search(rf"@{re.escape(name)}(?![\w-])", content, re.IGNORECASE)
        for name in names
    )


class AppService:
    """Keeps the app's channels, agents and messages in step with the org's Band account."""

    def __init__(self) -> None:
        self.org = BandOrg()
        self.agents = AgentManager(self)
        self._room_to_channel: dict[str, int] = {}
        self._poll_since: dict[str, datetime] = {}
        self._poller: asyncio.Task | None = None
        self._task_poller: asyncio.Task | None = None
        self._teammate_poller: asyncio.Task | None = None
        self._take_over_poller: asyncio.Task | None = None
        self._dm_lock = asyncio.Lock()
        self._register_lock = asyncio.Lock()
        self._user_orgs: dict[int, tuple[str, BandOrg]] = {}
        self._room_locks: dict[int, asyncio.Lock] = {}
        self._band_room_locks: dict[int, asyncio.Lock] = {}

    async def account_org(self, user_id: int) -> BandOrg:
        with db() as session:
            account = session.get(UserBandAccount, user_id)
        if account is None or not account.secret_enc:
            from app.config import uses_workspace_band_key
            if uses_workspace_band_key():
                raise ServiceError("Connect your own Band human API key before using Band.", 409)
            return self.org
        cached = self._user_orgs.get(user_id)
        if cached and cached[0] == account.secret_enc:
            return cached[1]
        if cached:
            await cached[1].close()
        org = BandOrg(decrypt_secret(account.secret_enc))
        await org.connect()
        if not org.online or not org.owner or org.owner.id != account.band_user_id:
            await org.close()
            # Browser tests have no live Band account. Human chat and the local fake agent still run.
            if os.getenv("E2E_FAKE_AGENT") == "1" and not settings.openai_api_key and not settings.band_user_api_key:
                return self.org
            raise ServiceError("Your Band connection needs to be reconnected", 409)
        self._user_orgs[user_id] = (account.secret_enc, org)
        return org

    async def room_org(self, channel_id: int) -> BandOrg:
        with db() as session:
            pinned = session.get(BandRoomAccount, channel_id)
            scope = session.get(PrivateConversation, channel_id)
        if pinned:
            org = await self.account_org(pinned.user_id) if pinned.user_id else self.org
            if org.owner and org.owner.id != pinned.band_user_id:
                raise ServiceError("The Band account for this room needs to be reconnected", 409)
            return org
        return await self.account_org(scope.owner_id) if scope else self.org

    async def agent_org(self, profile: AgentProfile) -> BandOrg:
        with db() as session:
            owner = session.get(BandAgentOwner, profile.id)
            account = session.get(UserBandAccount, owner.user_id) if owner else None
        if owner and account and account.secret_enc and account.band_user_id == owner.band_user_id:
            return await self.account_org(owner.user_id)
        if owner and self.org.owner and owner.band_user_id != self.org.owner.id:
            raise ServiceError("The Band account for this agent needs to be reconnected", 409)
        return self.org

    def human_band_id(self, user_id: int) -> str:
        with db() as session:
            account = session.get(UserBandAccount, user_id)
        return (account.band_user_id or "") if account else (self.org.owner.id if self.org.owner else "")

    def message_reply_to(self, user_id: int, channel_id: int) -> str:
        with db() as session:
            if session.get(HumanDirectMessage, channel_id):
                return ""
            pinned = session.get(BandRoomAccount, channel_id)
            scope = session.get(PrivateConversation, channel_id)
            if pinned:
                return pinned.band_user_id
            if scope:
                return self.human_band_id(scope.owner_id)
        return self.org.owner.id if self.org.owner else ""

    # --- lifecycle ---

    async def startup(self) -> None:
        with db() as session:
            latest = session.exec(select(Message.id).order_by(Message.id.desc()).limit(1)).first() or 0
        settle_interrupted()
        self._task_poller = asyncio.create_task(self._watch_task_messages(latest), name="task-messages")
        self._take_over_poller = asyncio.create_task(self._watch_take_overs(), name="take-overs")
        await self.org.connect()
        # Claimed before Band connects, a request would fail with "starting up" although Band is reachable.
        self._teammate_poller = asyncio.create_task(self._watch_teammate_requests(), name="teammate-requests")
        await self._publish_band_status()
        try:
            if self.org.online:
                await self._ensure_rooms()
                await self._ensure_master()
        except Exception as exc:
            logger.exception("Band setup failed")
            self.org.error = describe_error(exc)
            await self._publish_band_status()
        with db() as session:
            profiles = session.exec(select(AgentProfile)).all()
        for profile in profiles:
            if profile.id not in self.agents.status:
                try:
                    await self.start_agent(profile)
                except ServiceError:
                    await self.agent_status(profile.id, "error", "Reconnect the owner's Band account")
        self._poller = asyncio.create_task(self._poll_forever(), name="band-poller")

    async def shutdown(self) -> None:
        for poller in (self._task_poller, self._teammate_poller, self._take_over_poller):
            if poller is not None:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
        if self._poller is not None:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
        await self.agents.stop_all()
        for _, org in self._user_orgs.values():
            await org.close()
        self._user_orgs.clear()
        await self.org.close()

    async def end_expired_take_overs(self) -> None:
        """Announce take-overs that ran out of time, as a hand-back is announced."""
        for control in expire_controls():
            match = re.fullmatch(r"(?:(?:e2b|tenki)-)?agent-(.+)-channel-(\d+)", control.key)
            with db() as session:
                context = session.get(ComputerControlContext, control.key)
                task = session.get(AgentTask, context.task_id) if context else None
                if match:
                    channel_id = int(match.group(2))
                    agent = session.exec(select(AgentProfile).where(AgentProfile.band_agent_id == match.group(1))).first()
                elif task:
                    channel_id, agent = task.channel_id, session.get(AgentProfile, task.agent_id)
                else:
                    continue
            name = "the workspace" if control.key.startswith("tenki-workspace-") else agent.name if agent else "the agent"
            record("computer_control_expired", actor_type="system", actor_name="Computer", channel_id=channel_id,
                   detail={"user": control.user_name, "agent": name})
            await self.system_message(channel_id, f"{control.user_name}'s take-over of {name}'s computer ended "
                                                  f"after {CONTROL_MINUTES} minutes. The agent can use it again.")

    async def _watch_take_overs(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.end_expired_take_overs()
            except Exception:
                logger.exception("Could not end expired take-overs")

    async def _watch_task_messages(self, after_id: int) -> None:
        """Bridge worker commits into WebSocket updates: replies, hand-offs, approval requests, and task progress."""
        active: dict[str, dict[str, Any]] = {}
        drafts: dict[str, dict[str, Any]] = {}
        appends: dict[str, int] = {}
        while True:
            await asyncio.sleep(0.5)
            try:
                with db() as session:
                    # Read drafts first: a reply committed in between then shows up as a message, never as a gap.
                    written = {d.task_id: {"task_id": d.task_id, "channel_id": d.channel_id, "agent_id": d.agent_id,
                                           "content": d.content} for d in session.exec(select(MessageDraft)).all()}
                    messages = session.exec(select(Message).join(
                        AgentTask, AgentTask.reply_message_id == Message.id
                    ).where(Message.id > after_id)).all() + session.exec(select(Message).where(
                        Message.id > after_id, Message.band_message_id.startswith("handoff:")
                        | Message.band_message_id.startswith("approval:")
                        | Message.band_message_id.startswith("input:")
                        | Message.band_message_id.startswith("budget:"))).all()
                    working = session.exec(select(AgentTask).where(
                        AgentTask.status.in_(["queued", "running", "waiting"]))).all()
                    payloads = serialize_messages(session, sorted(messages, key=lambda m: m.id))
                # Updates go before replies and removals after, so a draft never reappears or leaves a gap.
                for task_id, draft in written.items():
                    old = drafts.get(task_id)
                    if old == draft:
                        continue
                    if old and draft["content"].startswith(old["content"]) and appends.get(task_id, 0) < DRAFT_RESYNC:
                        await bus.publish({"type": "draft_append", "task_id": task_id,
                                           "channel_id": draft["channel_id"], "agent_id": draft["agent_id"],
                                           "offset": _js_length(old["content"]),
                                           "text": draft["content"][len(old["content"]):]})
                        appends[task_id] = appends.get(task_id, 0) + 1
                    else:
                        await bus.publish({"type": "draft", **draft})
                        appends[task_id] = 0
                for payload in payloads:
                    await bus.publish({"type": "message", "message": payload})
                    after_id = payload["id"]
                # The worker adds hand-off teammates as channel members; refresh member lists once per channel.
                for channel_id in {m.channel_id for m in messages if (m.band_message_id or "").startswith("handoff:")}:
                    await self.publish_channel(channel_id)
                for task_id in drafts.keys() - written.keys():
                    await bus.publish({"type": "draft", **drafts[task_id], "content": ""})
                    appends.pop(task_id, None)
                drafts = written
                current = {t.id: {"id": t.id, "channel_id": t.channel_id, "agent_id": t.agent_id,
                                  "status": t.status, "progress": t.progress} for t in working}
                for task_id, info in current.items():
                    if active.get(task_id) != info:
                        await bus.publish({"type": "task_activity", "task": info})
                for task_id in active.keys() - current.keys():
                    await bus.publish({"type": "task_activity", "task": {**active[task_id], "status": "done"}})
                active = current
            except Exception:
                logger.exception("Could not publish worker updates")

    async def _watch_teammate_requests(self) -> None:
        """Register teammates that agents asked for; the worker can't, since Band registration lives here."""
        while True:
            await asyncio.sleep(0.5)
            try:
                with db() as session:
                    pending = session.exec(select(TeammateRequest.id).where(
                        TeammateRequest.status == "pending").order_by(TeammateRequest.id)).all()
                for request_id in pending:
                    await self.process_teammate_request(request_id)
            except Exception:
                logger.exception("Could not process teammate requests")

    async def process_teammate_request(self, request_id: int) -> None:
        with db() as session:
            # Claiming first stops the worker expiring a request whose Band registration is under way.
            claimed = session.exec(update(TeammateRequest).where(
                TeammateRequest.id == request_id, TeammateRequest.status == "pending").values(status="creating"))
            session.commit()
            request = session.get(TeammateRequest, request_id)
            requester = session.get(AgentProfile, request.requested_by_agent_id)
        if not claimed.rowcount:
            return
        status, agent_id, error = "failed", None, ""
        try:
            profile = await self.create_agent(name=request.name, role=request.role, instructions=request.instructions,
                                              model=request.model, channel_id=request.channel_id,
                                              created_by_agent=request.requested_by_agent_id)
            status, agent_id = "created", profile.id
        except asyncio.CancelledError:
            settle_interrupted(request_id)
            raise
        except ServiceError as exc:
            error = exc.message
        except Exception as exc:
            logger.exception("Creating teammate %s failed", request.name)
            error = type(exc).__name__
        with db() as session:
            session.exec(update(TeammateRequest).where(TeammateRequest.id == request_id).values(
                status=status, agent_id=agent_id, error=error[:500]))
            if status == "created":
                record("agent_created", actor_type="agent", actor_id=request.requested_by_agent_id,
                       actor_name=requester.name if requester else "Agent", channel_id=request.channel_id,
                       task_id=request.task_id, detail={"agent": profile.name, "role": profile.role,
                                                        "model": profile.model}, session=session)
            session.commit()
        if status == "created":
            await self.system_message(request.channel_id, f"{requester.name if requester else 'An agent'} created "
                                      f"**{profile.name}** ({role_label(profile.role)}) as a teammate in this channel.")

    def _require_band(self) -> None:
        if not self.org.online:
            raise ServiceError(f"Not connected to Band: {self.org.error or 'starting up'}", 503)

    async def _ensure_rooms(self) -> None:
        with db() as session:
            channels = [c for c in session.exec(select(Channel).where(Channel.band_room_id == None)).all()
                        if not session.get(PrivateConversation, c.id)]  # noqa: E711
        for channel in channels:
            room_id = await self.org.create_room(f"#{channel.name}")
            with db() as session:
                stored = session.get(Channel, channel.id)
                stored.band_room_id = room_id
                session.add(stored)
                session.commit()
            await self.publish_channel(channel.id)

    async def _ensure_master(self) -> None:
        with db() as session:
            master = session.exec(select(AgentProfile).where(AgentProfile.is_master == True)).first()  # noqa: E712
            private = set(session.exec(select(DirectMessage.channel_id)).all())
            private.update(session.exec(select(PrivateConversation.channel_id)).all())
            channel_ids = [c.id for c in session.exec(select(Channel)).all() if c.id not in private]
        if master is None:
            master = await self._register_agent(
                name=settings.master_agent_name,
                role="master",
                instructions="",
                model=configuration()[2],
                is_master=True,
            )
        for channel_id in channel_ids:
            await self.add_agent_to_channel(channel_id, master)

    def master(self) -> AgentProfile | None:
        with db() as session:
            return session.exec(select(AgentProfile).where(AgentProfile.is_master == True)).first()  # noqa: E712

    # --- serialization ---

    def serialize_agent(self, profile: AgentProfile) -> dict[str, Any]:
        status = self.agents.status.get(profile.id or 0, {"state": "stopped", "error": None})
        with db() as session:
            persona = session.get(AgentPersona, profile.id) if profile.id else None
            runtime = session.get(AgentRuntime, profile.id) if profile.id else None
            owner_id = agent_owner(session, profile)
        return {
            "id": profile.id,
            "name": profile.name,
            "handle": profile.handle,
            "role": profile.role,
            "role_label": persona.specialty if persona and persona.specialty else (
                {"codex": "Codex", "claude": "Claude Agent", "cursor": "Cursor", "openclaw": "OpenClaw"}.get(runtime.provider, "Connected agent")
                if runtime else role_label(profile.role)),
            "specialty": persona.specialty if persona else "",
            "avatar": persona.avatar if persona else None,
            "instructions": profile.instructions,
            "model": profile.model,
            "runtime": runtime.provider if runtime else "deepagents",
            "connection_id": runtime.connection_id if runtime else None,
            "band_agent_id": profile.band_agent_id,
            "is_master": profile.is_master,
            "status": status,
            "created_by": profile.created_by,
            "owner_id": owner_id,
            "created_by_agent": profile.created_by_agent,
            "created_at": profile.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "plugins": plugin_slugs(profile.id) if profile.id else None,
        }

    @staticmethod
    def serialize_channel(session: Session, channel: Channel) -> dict[str, Any]:
        members = session.exec(
            select(ChannelMember).where(ChannelMember.channel_id == channel.id)
        ).all()
        dm = session.get(DirectMessage, channel.id)
        human_dm = session.get(HumanDirectMessage, channel.id)
        scope = session.get(PrivateConversation, channel.id)
        outsiders = session.exec(select(OutsideAgent).where(OutsideAgent.channel_id == channel.id)
                                 .order_by(OutsideAgent.created_at)).all()
        return {
            "id": channel.id,
            "name": scope.title if scope and scope.title else channel.name,
            "topic": channel.topic,
            "band_room_id": channel.band_room_id,
            "human_ids": [m.member_id for m in members if m.member_type == "human"],
            "agent_ids": [m.member_id for m in members if m.member_type == "agent"],
            "outside_agents": [serialize_outside_agent(o) for o in outsiders],
            "is_dm": dm is not None or human_dm is not None,
            "dm_user_ids": [human_dm.first_user_id, human_dm.second_user_id] if human_dm else [],
            "owner_id": scope.owner_id if scope else channel.created_by,
            "private": scope is not None or dm is not None,
            "dm_agent_id": dm.agent_id if dm else None,
            "policy": serialize_policy(policy_of(session, channel.id)),
            "messaging_disabled": bool(human_dm and (
                session.get(ContactBlock, (human_dm.first_user_id, human_dm.second_user_id))
                or session.get(ContactBlock, (human_dm.second_user_id, human_dm.first_user_id)))),
        }

    async def publish_channel(self, channel_id: int) -> None:
        with db() as session:
            channel = session.get(Channel, channel_id)
            if channel:
                if session.get(PrivateConversation, channel_id):
                    audience = channel_audience(channel_id)
                    for agent_id in session.exec(select(ChannelMember.member_id).where(
                            ChannelMember.channel_id == channel_id, ChannelMember.member_type == "agent")).all():
                        profile = session.get(AgentProfile, agent_id)
                        if profile:
                            await bus.publish({"type": "agent", "agent": self.serialize_agent(profile)}, users=audience)
                await bus.publish({"type": "channel", "channel": self.serialize_channel(session, channel)})

    async def _publish_band_status(self) -> None:
        from app.config import uses_workspace_band_key
        if uses_workspace_band_key():
            return
        with db() as session:
            legacy = {u.id for u in session.exec(select(User)).all()
                      if not (session.get(UserBandAccount, u.id) and session.get(UserBandAccount, u.id).secret_enc)}
        await bus.publish({"type": "band_status", "band": self.org.status()}, users=legacy)

    # --- channels ---

    def _channel_for_room(self, session: Session, room_id: str) -> Channel | None:
        channel_id = self._room_to_channel.get(room_id)
        if channel_id is not None:
            return session.get(Channel, channel_id)
        channel = session.exec(select(Channel).where(Channel.band_room_id == room_id)).first()
        if channel:
            self._room_to_channel[room_id] = channel.id
        return channel

    async def create_channel(self, name: str, topic: str, creator: User) -> dict[str, Any]:
        try:
            org = await self.account_org(creator.id)
        except ServiceError:
            org = None
        with db() as session:
            scopes = session.exec(select(PrivateConversation).where(PrivateConversation.owner_id == creator.id,
                PrivateConversation.kind == "channel", PrivateConversation.title == name)).all()
            if scopes:
                raise ServiceError("You already have a channel with that name", 409)
        room_id = None
        if org and org.online:
            try:
                room_id = await org.create_room(f"#{name}")
            except Exception:
                # Human collaboration works independently of the optional Band transport.
                logger.warning("Created a local channel while Band was unavailable")
        with db() as session:
            channel = Channel(name=f"{name}-{uuid4().hex[:12]}", topic=topic, band_room_id=room_id, created_by=creator.id)
            session.add(channel)
            session.flush()
            session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=creator.id))
            session.add(PrivateConversation(channel_id=channel.id, owner_id=creator.id, title=name))
            if room_id:
                session.add(BandRoomAccount(channel_id=channel.id, band_user_id=org.owner.id,
                                           user_id=creator.id if org is not self.org else None))
            session.commit()
            channel_id = channel.id
        if room_id:
            self._room_to_channel[room_id] = channel_id
        await self.publish_channel(channel_id)
        with db() as session:
            return self.serialize_channel(session, session.get(Channel, channel_id))

    async def open_dm(self, user: User, profile: AgentProfile) -> dict[str, Any]:
        """The user's private channel with this agent, created with its own Band room on first use."""
        async with self._dm_lock:
            with db() as session:
                owner = agent_owner(session, profile)
                if owner is not None and owner != user.id:
                    raise ServiceError("Talk to this agent in an invited channel; only its owner can open a private DM", 403)
                existing = session.exec(select(DirectMessage).where(
                    DirectMessage.user_id == user.id, DirectMessage.agent_id == profile.id)).first()
                if existing:
                    return self.serialize_channel(session, session.get(Channel, existing.channel_id))
            org = await self.agent_org(profile)
            if org is self.org:
                self._require_band()
            try:
                room_id = await org.create_room(f"{user.display_name} + {profile.name}")
            except Exception as exc:
                raise ServiceError(f"Band couldn't create the room: {describe_error(exc)}", 502) from exc
            with db() as session:
                channel = Channel(name=f"dm-{uuid4().hex[:12]}", topic=f"Direct messages with {profile.name}",
                                  band_room_id=room_id, created_by=user.id)
                session.add(channel)
                session.flush()
                session.add_all([DirectMessage(channel_id=channel.id, user_id=user.id, agent_id=profile.id),
                                 ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id)])
                if org is not self.org:
                    session.add(PrivateConversation(channel_id=channel.id, owner_id=user.id, kind="agent_dm"))
                session.add(BandRoomAccount(channel_id=channel.id, band_user_id=org.owner.id,
                                           user_id=user.id if org is not self.org else None))
                session.commit()
                channel_id = channel.id
            self._room_to_channel[room_id] = channel_id
        await self.add_agent_to_channel(channel_id, profile)
        with db() as session:
            return self.serialize_channel(session, session.get(Channel, channel_id))

    async def _connect_channel_room(self, channel_id: int, org: BandOrg) -> str:
        async with self._band_room_locks.setdefault(channel_id, asyncio.Lock()):
            with db() as session:
                channel = session.get(Channel, channel_id)
                scope = session.get(PrivateConversation, channel_id)
                if channel.band_room_id:
                    return channel.band_room_id
            if not org.online:
                raise ServiceError("Configure a workspace or personal Band connection to add agents", 503)
            try:
                room_id = await org.create_room(f"#{scope.title or channel.name}")
            except Exception:
                raise ServiceError("Band could not connect this channel for agents", 502) from None
            with db() as session:
                stored = session.get(Channel, channel_id)
                stored.band_room_id = room_id
                session.add(stored)
                session.add(BandRoomAccount(channel_id=channel_id, band_user_id=org.owner.id,
                                           user_id=scope.owner_id if org is not self.org else None))
                session.commit()
            self._room_to_channel[room_id] = channel_id
            return room_id

    async def add_agent_to_channel(self, channel_id: int, profile: AgentProfile, *, publish: bool = True) -> None:
        org = await self.room_org(channel_id)
        with db() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise ServiceError("Channel not found", 404)
            room_id = channel.band_room_id
            scope = session.get(PrivateConversation, channel_id)
            if scope and scope.kind == "human_dm":
                raise ServiceError("Human direct messages cannot contain agents", 409)
        if scope and not room_id:
            # Browser tests have no Band transport. Membership is still recorded locally.
            if not (os.getenv("E2E_FAKE_AGENT") == "1" and not settings.openai_api_key and not settings.band_user_api_key and not org.online):
                room_id = await self._connect_channel_room(channel_id, org)
        if room_id and scope:
            try:
                with db() as session:
                    owned = session.get(BandAgentOwner, profile.id)
                inviter = await self.agent_org(profile) if owned else org
                await inviter.add_participant(room_id, profile.band_agent_id)
            except Exception:
                raise ServiceError("Band could not add this agent to the channel", 502) from None
        with db() as session:
            if not session.get(ChannelMember, (channel_id, "agent", profile.id)):
                session.add(ChannelMember(channel_id=channel_id, member_type="agent", member_id=profile.id))
                session.commit()
        if room_id and org.online and not scope:
            try:
                await org.add_participant(room_id, profile.band_agent_id)
            except Exception as exc:
                await self.system_message(channel_id, f"Couldn't add {profile.name} to the Band room: {describe_error(exc)}")
        if publish:
            await self.publish_channel(channel_id)

    async def remove_agent_from_channel(self, channel_id: int, profile: AgentProfile) -> None:
        org = await self.room_org(channel_id)
        with db() as session:
            channel = session.get(Channel, channel_id)
            scope = session.get(PrivateConversation, channel_id)
        if scope and channel.band_room_id:
            try:
                await org.remove_participant(channel.band_room_id, profile.band_agent_id)
            except Exception:
                raise ServiceError("Band could not remove this agent", 502) from None
            for account_org in [self.org, *[cached[1] for cached in self._user_orgs.values()]]:
                account_org.forget_participant(channel.band_room_id, profile.band_agent_id)
        with db() as session:
            member = session.get(ChannelMember, (channel_id, "agent", profile.id))
            channel = session.get(Channel, channel_id)
            room_id = channel.band_room_id if channel else None
            if member:
                session.delete(member)
                session.commit()
        await self.publish_channel(channel_id)
        if room_id and org.online and not scope:
            try:
                await org.remove_participant(room_id, profile.band_agent_id)
            except Exception:
                logger.exception("Couldn't remove %s from room %s", profile.name, room_id)

    async def list_peers(self, channel_id: int, user_id: int | None = None) -> list[dict[str, Any]]:
        """Band agents the caller can invite. Never the server key's agent list."""
        org = await self.room_org(channel_id)
        from app.config import uses_workspace_band_key
        if user_id is not None and org is self.org and uses_workspace_band_key():
            org = await self.account_org(user_id)
        if not org.online:
            self._require_band()
        with db() as session:
            channel = session.get(Channel, channel_id)
            if channel is None or not channel.band_room_id:
                raise ServiceError("Channel not found", 404)
            ours = set(session.exec(select(AgentProfile.band_agent_id)).all())
            invited = set(session.exec(select(OutsideAgent.band_id).where(OutsideAgent.channel_id == channel_id)).all())
        try:
            peers = await org.agent_peers(channel.band_room_id)
        except Exception as exc:
            raise ServiceError(f"Band couldn't list agents: {describe_error(exc)}", 502) from exc
        return [{
            "band_id": p.id, "handle": p.handle, "name": p.name, "description": p.description or "",
            "online": bool(p.online), "is_external": bool(p.is_external),
        } for p in peers if p.id not in ours and p.id not in invited]

    async def invite_outside_agent(self, user: User, channel_id: int, band_id: str) -> None:
        org = await self.room_org(channel_id)
        if not org.online:
            self._require_band()
        with db() as session:
            channel = session.get(Channel, channel_id)
            if channel is None or not channel.band_room_id:
                raise ServiceError("Channel not found", 404)
            if session.get(OutsideAgent, (channel_id, band_id)):
                return
            if session.exec(select(AgentProfile.id).where(AgentProfile.band_agent_id == band_id)).first():
                raise ServiceError("That agent belongs to this workspace; add it as a teammate instead", 409)
        peers = await self.list_peers(channel_id, user.id)
        if org is self.org:
            from app.config import uses_workspace_band_key
            if uses_workspace_band_key():
                org = await self.account_org(user.id)
        peer = next((p for p in peers if p["band_id"] == band_id), None)
        if peer is None:
            raise ServiceError("That Band agent isn't available to invite", 404)
        with db() as session:
            # Agents name teammates in ask_teammate, so a name must point at exactly one of them.
            if session.exec(select(AgentProfile.id).where(
                    func.lower(AgentProfile.name) == peer["name"].lower())).first():
                raise ServiceError(f"A teammate is already named {peer['name']!r}", 409)
        try:
            await org.add_participant(channel.band_room_id, band_id)
        except Exception as exc:
            raise ServiceError(f"Band couldn't add the agent: {describe_error(exc)}", 502) from exc
        with db() as session:
            session.add(OutsideAgent(channel_id=channel_id, band_id=band_id, handle=peer["handle"], name=peer["name"],
                                     description=peer["description"], added_by=user.id))
            session.commit()
        await self.system_message(channel_id, f"{user.display_name} invited the outside Band agent "
                                  f"**{peer['name']}** (@{peer['handle']}).")
        await self.publish_channel(channel_id)

    async def remove_outside_agent(self, channel_id: int, band_id: str) -> None:
        with db() as session:
            outsider = session.get(OutsideAgent, (channel_id, band_id))
            if outsider is None:
                raise ServiceError("That agent isn't in this channel", 404)
            channel = session.get(Channel, channel_id)
            name = outsider.name
        # Leave Band first: dropping only the local row would keep the agent reading the Band room.
        if channel.band_room_id:
            org = await self.room_org(channel_id)
            if not org.online:
                self._require_band()
            try:
                await org.remove_participant(channel.band_room_id, band_id)
            except Exception as exc:
                raise ServiceError(f"Band couldn't remove the agent: {describe_error(exc)}", 502) from exc
        with db() as session:
            if outsider := session.get(OutsideAgent, (channel_id, band_id)):
                session.delete(outsider)
                session.commit()
        await self.system_message(channel_id, f"**{name}** was removed from the channel.")
        await self.publish_channel(channel_id)

    # --- agents ---

    async def start_agent(self, profile: AgentProfile) -> None:
        assert profile.id is not None
        if not runtime_for(profile.id) and not configuration()[0]:
            self.agents.status[profile.id] = {"state": "error", "error": "Configure OpenAI in workspace settings"}
            await self.agent_status(profile.id, "error", "Configure OpenAI in workspace settings")
            return
        org = await self.agent_org(profile)
        if not org.online:
            self.agents.status[profile.id] = {"state": "error", "error": "Connect Band before starting this agent"}
            await self.agent_status(profile.id, "error", "Connect Band before starting this agent")
            return
        owner = org.owner
        await self.agents.start(
            profile,
            owner_id=owner.id if owner else "",
        )

    async def _register_agent(
        self,
        *,
        name: str,
        role: str,
        instructions: str,
        model: str,
        created_by: int | None = None,
        created_by_agent: int | None = None,
        is_master: bool = False,
        plugins: list[str] | None = None,
        specialty: str = "",
        avatar: str | None = None,
        runtime: str = "deepagents",
        connection_id: str | None = None,
    ) -> AgentProfile:
        owner_user_id = created_by
        if owner_user_id is None and created_by_agent:
            with db() as session:
                parent = session.get(AgentProfile, created_by_agent)
                owner_user_id = agent_owner(session, parent) if parent else None
        org = await self.account_org(owner_user_id) if owner_user_id else self.org
        if not org.online and os.getenv("E2E_FAKE_AGENT") == "1" and not settings.openai_api_key and not settings.band_user_api_key:
            return await self._register_local_agent(name=name, role=role, instructions=instructions, model=model,
                                                    created_by=created_by, created_by_agent=created_by_agent,
                                                    is_master=is_master, plugins=plugins, specialty=specialty, avatar=avatar)
        if not org.online:
            raise ServiceError("Not connected to Band; check the workspace integration or optional personal connection", 503)
        name = name.strip()
        if len(name) < 3:
            raise ServiceError("Agent name needs at least 3 characters", 422)
        # Registration awaits Band for seconds; one at a time keeps two requests from claiming the same name.
        async with self._register_lock:
            with db() as session:
                named = session.exec(select(AgentProfile).where(func.lower(AgentProfile.name) == name.lower())).all()
                if any(org is self.org or agent_owner(session, item) == owner_user_id for item in named):
                    raise ServiceError(f"An agent named {name!r} already exists", 409)
                # Local names win in ask_teammate, so a new agent must not hide an invited outside agent.
                if outsider := session.exec(select(OutsideAgent).where(
                        func.lower(OutsideAgent.name) == name.lower())).first():
                    raise ServiceError(f"The outside Band agent {outsider.name!r} already uses that name", 409)
            role_info = MASTER_ROLE if role == "master" else ROLES[role]
            try:
                band_id, api_key = await org.register_agent(
                    name, f"{role_info['label']} agent in BAND_BOTS. {role_info['description']}"
                )
                identity = await fetch_identity(api_key)
            except Exception as exc:
                raise ServiceError(f"Band couldn't register the agent: {describe_error(exc)}", 502) from exc
            profile = AgentProfile(
                name=identity.name or name,
                handle=identity.handle,
                role=role,
                instructions=instructions.strip(),
                model=model,
                band_agent_id=band_id,
                band_api_key_enc=encrypt_secret(api_key),
                is_master=is_master,
                created_by=created_by,
                created_by_agent=created_by_agent,
            )
            with db() as session:
                session.add(profile)
                session.flush()
                if owner_user_id:
                    session.add(BandAgentOwner(agent_id=profile.id, user_id=owner_user_id, band_user_id=org.owner.id))
                if runtime != "deepagents":
                    connection = session.get(AgentConnection, connection_id)
                    if connection is None or connection.provider != runtime:
                        raise ServiceError("Agent connection is no longer available", 409)
                    session.add(AgentRuntime(agent_id=profile.id, provider=runtime, connection_id=connection_id))
                if specialty.strip() or avatar is not None:
                    session.add(AgentPersona(agent_id=profile.id, specialty=specialty.strip(), avatar=avatar))
                session.commit()
                session.refresh(profile)
        if plugins is not None and profile.id is not None:
            set_plugin_access(profile.id, plugins)
        await self.start_agent(profile)
        await bus.publish({"type": "agent", "agent": self.serialize_agent(profile)})
        return profile

    async def _register_local_agent(
        self,
        *,
        name: str,
        role: str,
        instructions: str,
        model: str,
        created_by: int | None = None,
        created_by_agent: int | None = None,
        is_master: bool = False,
        plugins: list[str] | None = None,
        specialty: str = "",
        avatar: str | None = None,
    ) -> AgentProfile:
        """Deterministic teammate for browser tests. Never calls Band or a model provider."""
        name = name.strip()
        if len(name) < 3:
            raise ServiceError("Agent name needs at least 3 characters", 422)
        owner_user_id = created_by
        if owner_user_id is None and created_by_agent:
            with db() as session:
                parent = session.get(AgentProfile, created_by_agent)
                owner_user_id = agent_owner(session, parent) if parent else None
        profile = AgentProfile(
            name=name,
            handle=f"e2e-{uuid4().hex[:12]}",
            role=role,
            instructions=instructions.strip(),
            model=model,
            band_agent_id=f"e2e-{uuid4().hex}",
            band_api_key_enc=encrypt_secret("e2e"),
            is_master=is_master,
            created_by=created_by,
            created_by_agent=created_by_agent,
        )
        with db() as session:
            session.add(profile)
            session.flush()
            if owner_user_id:
                session.add(BandAgentOwner(agent_id=profile.id, user_id=owner_user_id, band_user_id="e2e-local"))
            if specialty.strip() or avatar is not None:
                session.add(AgentPersona(agent_id=profile.id, specialty=specialty.strip(), avatar=avatar))
            session.commit()
            session.refresh(profile)
        if plugins is not None and profile.id is not None:
            set_plugin_access(profile.id, plugins)
        assert profile.id is not None
        self.agents.status[profile.id] = {"state": "online", "error": None}
        await self.agent_status(profile.id, "online", None)
        await bus.publish({"type": "agent", "agent": self.serialize_agent(profile)})
        return profile

    async def create_agent(
        self,
        *,
        name: str,
        role: str,
        instructions: str,
        model: str,
        channel_id: int | None = None,
        created_by: int | None = None,
        created_by_agent: int | None = None,
        plugins: list[str] | None = None,
        specialty: str = "",
        avatar: str | None = None,
        runtime: str = "deepagents",
        connection_id: str | None = None,
    ) -> AgentProfile:
        if created_by is None and created_by_agent and channel_id is not None:
            with db() as session:
                parent = session.get(AgentProfile, created_by_agent)
                scope = session.get(PrivateConversation, channel_id)
                if parent and scope and agent_owner(session, parent) is None:
                    created_by = scope.owner_id
        profile = await self._register_agent(
            name=name,
            role=role,
            instructions=instructions,
            model=model,
            created_by=created_by,
            created_by_agent=created_by_agent,
            plugins=plugins,
            specialty=specialty,
            avatar=avatar,
            runtime=runtime,
            connection_id=connection_id,
        )
        if channel_id is not None:
            await self.add_agent_to_channel(channel_id, profile)
        return profile

    async def delete_agent(self, profile: AgentProfile) -> None:
        org = await self.agent_org(profile)
        if profile.is_master:
            raise ServiceError("The master agent can't be removed", 409)
        await self.agents.stop(profile.id)
        with db() as session:
            memberships = session.exec(
                select(ChannelMember).where(ChannelMember.member_type == "agent", ChannelMember.member_id == profile.id)
            ).all()
            channel_ids = [m.channel_id for m in memberships]
            for member in memberships:
                session.delete(member)
            # SQLite may reuse this row id for a future agent; nothing scoped to it may carry over.
            for memory in session.exec(select(AgentMemory).where(AgentMemory.agent_id == profile.id)).all():
                session.delete(memory)
            for skill in session.exec(select(AgentSkill).where(AgentSkill.agent_id == profile.id)).all():
                session.delete(skill)
            if defaults := session.get(AgentUISkills, profile.id):
                session.delete(defaults)
            for schedule in session.exec(select(Schedule).where(Schedule.agent_id == profile.id)).all():
                schedule.enabled = False
                session.add(schedule)
            for sandbox in session.exec(select(TaskSandbox).where(
                    or_(TaskSandbox.task_id.startswith(f"agent-{profile.band_agent_id}-"),
                        TaskSandbox.task_id.startswith(f"e2b-agent-{profile.band_agent_id}-"),
                        TaskSandbox.task_id.startswith(f"tenki-agent-{profile.band_agent_id}-")))).all():
                if sandbox.sandbox_id and (connection := session.get(ComputerConnection, sandbox.sandbox_id)):
                    session.delete(connection)
                if lease := session.get(ComputerViewLease, sandbox.task_id):
                    session.delete(lease)
                if display := session.get(ComputerDisplay, sandbox.task_id):
                    session.delete(display)
                for endpoint in session.exec(select(ComputerEndpoint).where(ComputerEndpoint.key == sandbox.task_id)).all():
                    session.delete(endpoint)
                if gateway := session.get(ComputerGateway, sandbox.task_id):
                    session.delete(gateway)
                if control := session.get(ComputerControl, sandbox.task_id):
                    session.delete(control)
                if sandbox.task_id.startswith("tenki-"):
                    sandbox.status, sandbox.latest_image, sandbox.frame_id = "retired", "", ""
                    sandbox.error = "Agent removed; cloud computer and saved disk require provider cleanup"
                    session.add(sandbox)
                else:
                    session.delete(sandbox)
            for scope in session.exec(select(ComputerScope).where(
                    ComputerScope.key.startswith(f"agent-{profile.band_agent_id}-channel-"))).all():
                session.delete(scope)
            for disk in session.exec(select(ComputerDisk).where(
                    ComputerDisk.key.contains(f":tenki-agent-{profile.band_agent_id}-channel-"))).all():
                disk.state = "retired"
                session.add(disk)
            if budget := session.get(Budget, ("agent", profile.id)):
                session.delete(budget)
            if access := session.get(AgentPluginAccess, profile.id):
                session.delete(access)
            if persona := session.get(AgentPersona, profile.id):
                session.delete(persona)
            if runtime := session.get(AgentRuntime, profile.id):
                session.delete(runtime)
            if owner := session.get(BandAgentOwner, profile.id):
                session.delete(owner)
            for dm in session.exec(select(DirectMessage).where(DirectMessage.agent_id == profile.id)).all():
                dm.agent_id = None
                session.add(dm)
            session.delete(session.get(AgentProfile, profile.id))
            session.commit()
            rooms = [c.band_room_id for c in session.exec(select(Channel)).all() if c.band_room_id]
        for room_id in rooms:
            self.org.forget_participant(room_id, profile.band_agent_id)
        if org.online:
            try:
                await org.delete_agent(profile.band_agent_id)
            except Exception:
                logger.exception("Couldn't delete %s on Band", profile.name)
        for channel_id in channel_ids:
            await self.publish_channel(channel_id)
        await bus.publish({"type": "agent_deleted", "agent_id": profile.id})

    # --- messages ---

    async def _store(self, message: Message) -> dict[str, Any] | None:
        """Stores and broadcasts a message; Band messages already stored are skipped."""
        with db() as session:
            if message.band_message_id and session.exec(
                select(Message.id).where(Message.band_message_id == message.band_message_id)
            ).first():
                return None
            session.add(message)
            session.commit()
            session.refresh(message)
            payload = serialize_message(message)
        await bus.publish({"type": "message", "message": payload})
        return payload

    async def publish_components(self, task_ids: Sequence[str]) -> None:
        with db() as session:
            messages = session.exec(select(Message).join(MessageComponent, MessageComponent.message_id == Message.id)
                                    .where(MessageComponent.task_id.in_(task_ids)).distinct()).all()
            payloads = serialize_messages(session, messages)
        for message in payloads:
            await bus.publish({"type": "message_updated", "message": message})

    async def publish_inputs(self, task_id: str) -> None:
        with db() as session:
            messages = session.exec(select(Message).join(MessageComponent, MessageComponent.message_id == Message.id)
                .where(MessageComponent.task_id == task_id, MessageComponent.kind == "input")).all()
            payloads = serialize_messages(session, messages)
        for message in payloads:
            await bus.publish({"type": "message_updated", "message": message})

    async def publish_approvals(self, approval_ids: Sequence[str]) -> None:
        """Re-send approval request messages so open clients swap the buttons for the decision."""
        with db() as session:
            messages = session.exec(select(Message).where(
                Message.band_message_id.in_([f"approval:{a}" for a in approval_ids]))).all()
            payloads = serialize_messages(session, messages)
        for payload in payloads:
            await bus.publish({"type": "message_updated", "message": payload})

    async def system_message(self, channel_id: int, content: str) -> None:
        await self._store(
            Message(channel_id=channel_id, author_type="system", author_name="System", content=content)
        )

    def existing_human_message(self, user: User, channel_id: int, client_id: str, content: str,
                               attachment_ids: Sequence[str] | None = None) -> dict[str, Any] | None:
        """The original message when this send was already committed. None when the id is new."""
        digest = message_digest(content, list(attachment_ids or []))
        with db() as session:
            existing = client_message_row(session, user.id, channel_id, client_id)
            if existing is None:
                return None
            return replay_client_message(session, existing, digest)

    async def post_human_message(self, user: User, channel_id: int, content: str, attachment_ids: list[str] | None = None,
                                 client_id: str | None = None) -> dict[str, Any]:
        attachment_ids = list(attachment_ids or [])
        digest = message_digest(content, attachment_ids)
        if client_id:
            replayed = self.existing_human_message(user, channel_id, client_id, content, attachment_ids)
            if replayed is not None:
                return replayed
        with db() as session:
            agents = session.exec(select(AgentProfile)).all()
            users = session.exec(select(User)).all()
            dm = session.get(DirectMessage, channel_id)
            human_dm = session.get(HumanDirectMessage, channel_id)
            private = session.get(PrivateConversation, channel_id)
            allowed_agents = set(session.exec(select(ChannelMember.member_id).where(
                ChannelMember.channel_id == channel_id, ChannelMember.member_type == "agent")).all())
            agents = [a for a in agents if agent_owner(session, a) in {None, user.id} or a.id in allowed_agents]
            reply_to = self.message_reply_to(user.id, channel_id)
        if human_dm:
            mentioned_agents = []
        elif dm:
            # A direct message stays one-on-one: everything goes to its agent, mentioned or not.
            mentioned_agents = [a for a in agents if a.id == dm.agent_id]
            users = []
        else:
            mentioned_agents = [a for a in agents if _mentioned(content, a.name, a.handle)]
            if private:
                mentioned_agents = [a for a in mentioned_agents if a.id in allowed_agents]
        mentions: list[dict[str, Any]] = [
            {"type": "agent", "id": a.id, "name": a.name} for a in mentioned_agents
        ] + [
            {"type": "human", "id": u.id, "name": u.display_name}
            for u in users
            if u.id != user.id and _mentioned(content, u.display_name, u.username)
        ]
        message = Message(
            channel_id=channel_id,
            author_type="human",
            author_id=user.id,
            author_name=user.display_name,
            content=content,
            mentions=mentions,
        )
        # Accept the message and its work atomically; Band relay cannot lose an accepted task.
        # A retried client_id that loses the race returns the committed message and does not enqueue again.
        with db() as session:
            try:
                channel = session.get(Channel, channel_id)
                context = channel_context(session, channel_id) if mentioned_agents else ""
                session.add(message)
                session.flush()
                task_ids = []
                for agent in mentioned_agents:
                    room_id = channel.band_room_id or ""
                    task_id = uuid4().hex
                    task_ids.append(task_id)
                    session.add(AgentTask(id=task_id,
                        source_key=f"{agent.id}:{room_id}:app-message-{message.id}",
                        channel_id=channel_id, agent_id=agent.id, room_id=room_id,
                        prompt=f"**{user.display_name}:** {content or 'Documents uploaded. Briefly acknowledge receipt; wait for analysis instructions.'}", context=context,
                        reply_to=reply_to))
                    if not session.get(ChannelMember, (channel_id, "agent", agent.id)):
                        session.add(ChannelMember(channel_id=channel_id, member_type="agent", member_id=agent.id))
                link_attachments(session, message, attachment_ids, task_ids)
                if client_id:
                    session.add(ClientMessage(user_id=user.id, channel_id=channel_id, client_id=client_id,
                                              message_id=message.id, payload_hash=digest))
                session.commit()
                session.refresh(message)
                payload = serialize_messages(session, [message])[0]
            except IntegrityError:
                session.rollback()
                if not client_id:
                    raise
                existing = client_message_row(session, user.id, channel_id, client_id)
                if existing is None:
                    raise
                return replay_client_message(session, existing, digest)
        await bus.publish({"type": "message", "message": payload})
        if mentioned_agents:
            await self.publish_channel(channel_id)
        asyncio.create_task(self._relay(channel_id, message.id, user.display_name, content, mentioned_agents, user.id))
        return payload

    async def _relay(
        self, channel_id: int, message_id: int, author: str, content: str, mentioned: list[AgentProfile], user_id: int | None = None
    ) -> None:
        # A poll must not mirror an app send while its Band receipt is still being saved.
        async with self._room_locks.setdefault(channel_id, asyncio.Lock()):
            await self._relay_unlocked(channel_id, message_id, author, content, mentioned, user_id)

    async def _relay_unlocked(
        self, channel_id: int, message_id: int, author: str, content: str, mentioned: list[AgentProfile], user_id: int | None
    ) -> None:
        """Posts through the room's owning Band connection with the app author's name."""
        with db() as session:
            if session.get(HumanDirectMessage, channel_id):
                return
        try:
            org = await self.room_org(channel_id)
        except ServiceError:
            return
        if not org.online:
            if mentioned:
                await self.system_message(channel_id, "Band is offline; your agent task is saved locally.")
            return
        with db() as session:
            channel = session.get(Channel, channel_id)
            room_id = channel.band_room_id if channel else None
        if not room_id:
            return
        for profile in mentioned:
            await self.add_agent_to_channel(channel_id, profile)
        try:
            recipients = [(a.band_agent_id, a.handle) for a in mentioned]
            band_id = await org.send(room_id, f"**{author}:** {content}", recipients)
        except Exception as exc:
            if mentioned:
                logger.exception("Relay to Band failed")
                await self.system_message(channel_id, f"Couldn't sync the message to Band: {describe_error(exc)}. Agent tasks remain saved locally.")
            else:
                logger.info("Band didn't accept an unmentioned message: %s", describe_error(exc))
            return
        with db() as session:
            stored = session.get(Message, message_id)
            if stored:
                stored.band_message_id = band_id
                session.add(stored)
                session.commit()

    def _strip_owner_mentions(self, content: str) -> str:
        owner = self.org.owner
        if owner:
            for ref in {owner.handle, owner.name}:
                if ref:
                    content = re.sub(rf"@{re.escape(ref)}(?![\w-])[,:]?\s*", "", content, flags=re.IGNORECASE)
        return content.strip()

    def _resolve_agent_mentions(self, refs: list[str]) -> tuple[list[dict[str, Any]], bool]:
        with db() as session:
            agents = session.exec(select(AgentProfile)).all()
        resolved: list[dict[str, Any]] = []
        to_humans = False
        for ref in refs:
            if self.org.is_owner_reference(ref):
                to_humans = True
                continue
            key = ref.strip().lstrip("@").lower()
            for agent in agents:
                if key in {agent.band_agent_id.lower(), agent.handle.lower(), agent.name.lower()}:
                    resolved.append({"type": "agent", "id": agent.id, "name": agent.name})
                    break
        return resolved, to_humans

    async def _store_agent_message(
        self, channel_id: int, profile: AgentProfile, content: str, refs: list[str], band_message_id: str | None
    ) -> None:
        with db() as session:
            is_member = session.get(ChannelMember, (channel_id, "agent", profile.id)) is not None
            if not is_member and session.get(PrivateConversation, channel_id):
                return
            if not is_member:
                session.add(ChannelMember(channel_id=channel_id, member_type="agent", member_id=profile.id))
                session.commit()
        if not is_member:
            await self.publish_channel(channel_id)
        resolved, to_humans = self._resolve_agent_mentions(refs)
        await self._store(
            Message(
                channel_id=channel_id,
                author_type="agent",
                author_id=profile.id,
                author_name=profile.name,
                content=self._strip_owner_mentions(content) if to_humans else content,
                mentions=resolved,
                to_humans=to_humans,
                band_message_id=band_message_id,
            )
        )

    # --- AgentSink ---

    async def agent_message(
        self,
        profile_id: int,
        room_id: str,
        content: str,
        mentions: list[str],
        band_message_id: str | None,
    ) -> None:
        with db() as session:
            channel = self._channel_for_room(session, room_id)
            profile = session.get(AgentProfile, profile_id)
        if channel is None or profile is None:
            logger.info("Ignoring agent message for unmapped room %s", room_id)
            return
        await self._store_agent_message(channel.id, profile, content, mentions, band_message_id)

    async def agent_typing(self, profile_id: int, room_id: str, active: bool) -> None:
        with db() as session:
            channel = self._channel_for_room(session, room_id)
        if channel is not None:
            await bus.publish(
                {"type": "typing", "channel_id": channel.id, "agent_id": profile_id, "active": active}
            )

    async def agent_status(self, profile_id: int, state: str, error: str | None) -> None:
        await bus.publish(
            {"type": "agent_status", "agent_id": profile_id, "status": {"state": state, "error": error}}
        )

    # --- Band -> app sync ---

    async def _poll_forever(self) -> None:
        started = datetime.now(timezone.utc)
        while True:
            await asyncio.sleep(settings.poll_seconds)
            with db() as session:
                rooms = [(c.id, c.band_room_id) for c in session.exec(select(Channel)).all() if c.band_room_id]
            for channel_id, room_id in rooms:
                try:
                    await self.sync_room(channel_id, room_id, self._poll_since.get(room_id, started))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Polling room %s failed: %s", room_id, describe_error(exc))

    async def sync_room(self, channel_id: int, room_id: str, since: datetime) -> None:
        async with self._room_locks.setdefault(channel_id, asyncio.Lock()):
            await self._sync_room(channel_id, room_id, since)

    async def _sync_room(self, channel_id: int, room_id: str, since: datetime) -> None:
        """Brings in room messages posted outside this app (Band UI, external agents, SDK fallbacks)."""
        org = await self.room_org(channel_id)
        owner_id = org.owner.id if org.owner else None
        with db() as session:
            agents_by_band_id = {a.band_agent_id: a for a in session.exec(select(AgentProfile)).all()}
        for item in await org.messages_since(room_id, since):
            if item.inserted_at and item.inserted_at > self._poll_since.get(room_id, since):
                self._poll_since[room_id] = item.inserted_at
            with db() as session:
                if session.exec(select(Message.id).where(Message.band_message_id == item.id)).first():
                    continue
                local_human = session.exec(select(UserBandAccount).where(UserBandAccount.band_user_id == item.sender_id)).first()
                local_user = session.get(User, local_human.user_id) if local_human else None
                if session.get(PrivateConversation, channel_id):
                    sender_agent = agents_by_band_id.get(item.sender_id)
                    human_member = local_user and session.get(ChannelMember, (channel_id, "human", local_user.id))
                    agent_member = sender_agent and session.get(ChannelMember, (channel_id, "agent", sender_agent.id))
                    bridge = session.get(BandRoomAccount, channel_id)
                    bridge_owner = bridge and bridge.band_user_id == item.sender_id
                    if not human_member and not agent_member and not bridge_owner and not session.get(OutsideAgent, (channel_id, item.sender_id)):
                        continue
            if item.sender_id == owner_id and org is self.org:
                continue
            profile = agents_by_band_id.get(item.sender_id)
            if profile is not None:
                # Managed agents publish their answer locally before the worker sends
                # the Band copy. Mirroring that copy would race its send receipt.
                if profile.band_agent_id in self.agents.local_band_ids:
                    continue
                refs = re.findall(r"@([\w./-]+)", item.content)
                await self._store_agent_message(channel_id, profile, item.content, refs, item.id)
                continue
            with db() as session:
                content = display_names(session, item.content)
            await self._store(
                Message(
                    channel_id=channel_id,
                    author_type="human" if local_user else "external",
                    author_id=local_user.id if local_user else None,
                    author_name=local_user.display_name if local_user else item.sender_name or "Band user",
                    content=self._strip_owner_mentions(content),
                    band_message_id=item.id,
                )
            )


service = AppService()
