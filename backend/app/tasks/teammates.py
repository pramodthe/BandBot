"""Agents grow their team: new visible teammates (registered by the API process) and outside Band agents."""
from __future__ import annotations

import asyncio
import logging

from band.client.rest import AsyncRestClient, ChatMessageRequest, ChatMessageRequestMentionsItem, aclose_rest_client
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.auth import decrypt_secret
from app.config import settings
from app.openai_provider import configuration
from app.persistence.models import (AgentProfile, AgentTask, DirectMessage, Message, OutsideAgent, OutsideHandoff,
                                    TaskHandoff, TeammateRequest, db)
from app.tasks.queue import MAX_HANDOFFS, handoff_root, handoffs_used
from app.workspace.audit import record_agent
from app.workspace.roles import ROLES, role_label
from app.workspace.access import channel_teammates

logger = logging.getLogger(__name__)

CREATE_WAIT = 30.0
REGISTER_WAIT = 60.0
POLL = 0.5
INTERRUPTED = "The app server restarted while registering it; try again"


def _role_key(role: str) -> str | None:
    wanted = role.strip().lower()
    return next((key for key, info in ROLES.items() if wanted in (key, info["label"].lower())), None)


def _created(name: str, role: str) -> dict:
    return {"status": "created", "teammate": name, "role": role_label(role),
            "note": f"{name} is now a visible teammate in this channel. Hand off with ask_teammate."}


def _request(task: AgentTask, *, name: str, role: str, instructions: str, call_id: str) -> dict | int:
    """A finished answer, or the id of the request to wait for."""
    name = name.strip().lstrip("@")
    key = _role_key(role)
    if key is None:
        return {"error": f"Unknown role {role!r}. Choose one of: {', '.join(ROLES)}."}
    if not 3 <= len(name) <= 40:
        return {"error": "Give the teammate a name of 3-40 characters."}
    source_key = f"{task.id}:{call_id}"
    with db() as session:
        if session.get(DirectMessage, task.channel_id):
            return {"error": "This is a private one-on-one conversation, so teammates can't join it."}
        if existing := session.exec(select(TeammateRequest).where(TeammateRequest.source_key == source_key)).first():
            return existing.id
        same = next((a for a in channel_teammates(session, task.channel_id) if a.name.lower() == name.lower()), None)
        if same:
            return {"status": "exists", "teammate": same.name, "role": role_label(same.role),
                    "note": f"{same.name} already exists; reuse it with ask_teammate instead of creating another."}
        if outsider := session.exec(select(OutsideAgent).where(OutsideAgent.channel_id == task.channel_id,
                func.lower(OutsideAgent.name) == name.lower())).first():
            return {"error": f"{outsider.name} is an outside Band agent. Hand off to it with ask_teammate, "
                             "or choose another name."}
        in_progress = session.exec(select(TeammateRequest).where(
            func.lower(TeammateRequest.name) == name.lower(),
            TeammateRequest.status.in_(["pending", "creating"]))).first()
        if in_progress:
            return in_progress.id
        made = session.exec(select(func.count()).select_from(AgentProfile).where(
            AgentProfile.created_by_agent != None)).one()  # noqa: E711
        waiting = session.exec(select(func.count()).select_from(TeammateRequest).where(
            TeammateRequest.status.in_(["pending", "creating"]))).one()
        if made + waiting >= settings.max_agent_teammates:
            return _limit_reached()
        requester = session.get(AgentProfile, task.agent_id)
        request = TeammateRequest(source_key=source_key, task_id=task.id, channel_id=task.channel_id,
                                  requested_by_agent_id=task.agent_id, name=name, role=key,
                                  instructions=instructions.strip()[:4000],
                                  model=requester.model if requester else configuration()[2])
        session.add(request)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return session.exec(select(TeammateRequest).where(TeammateRequest.source_key == source_key)).one().id
        return _settle_race(session, request)


def _settle_race(session, request: TeammateRequest) -> dict | int:
    """Tasks run in parallel, so two requests can pass the checks at once; the earliest one wins."""
    active = TeammateRequest.status.in_(["pending", "creating"])
    earlier = session.exec(select(TeammateRequest).where(
        active, TeammateRequest.id < request.id, func.lower(TeammateRequest.name) == request.name.lower())).first()
    made = session.exec(select(func.count()).select_from(AgentProfile).where(
        AgentProfile.created_by_agent != None)).one()  # noqa: E711
    ahead = session.exec(select(func.count()).select_from(TeammateRequest).where(
        active, TeammateRequest.id < request.id)).one()
    if not earlier and made + ahead < settings.max_agent_teammates:
        return request.id
    withdrawn = session.exec(update(TeammateRequest).where(
        TeammateRequest.id == request.id, TeammateRequest.status == "pending").values(
        status="failed", error="Another request for this teammate came first" if earlier
        else "The workspace teammate limit was reached"))
    session.commit()
    if not withdrawn.rowcount:  # the app server already claimed it
        return request.id
    return earlier.id if earlier else _limit_reached()


def _limit_reached() -> dict:
    return {"error": f"Agents have already created {settings.max_agent_teammates} teammates, the workspace "
                     "limit. Reuse an existing teammate or ask a person to create one."}


async def create_teammate(task: AgentTask, *, name: str, role: str, instructions: str, call_id: str,
                          wait: float = CREATE_WAIT, register_wait: float = REGISTER_WAIT) -> dict:
    """Ask the API process to register a teammate, then wait for the outcome."""
    outcome = _request(task, name=name, role=role, instructions=instructions, call_id=call_id)
    if isinstance(outcome, dict):
        return outcome
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        with db() as session:
            request = session.get(TeammateRequest, outcome)
            agent = session.get(AgentProfile, request.agent_id) if request.agent_id else None
        if request.status == "created" and agent:
            return _created(agent.name, agent.role)
        if request.status == "failed":
            return {"error": f"Couldn't create {request.name}: {request.error or 'unknown error'}"}
        if asyncio.get_running_loop().time() >= deadline:
            with db() as session:
                # Unclaimed requests expire so the app server can't create the teammate after the agent moved on.
                expired = session.exec(update(TeammateRequest).where(
                    TeammateRequest.id == outcome, TeammateRequest.status == "pending").values(
                    status="failed", error="The app server didn't pick up the request; try again later"))
                session.commit()
            if expired.rowcount:
                return {"error": f"Couldn't create {request.name}: the app server didn't respond. Try again later."}
            if register_wait <= 0:
                return {"status": "creating", "teammate": request.name,
                        "note": "Band is still registering it. Try ask_teammate in a minute."}
            deadline, register_wait = deadline + register_wait, 0
        await asyncio.sleep(POLL)


def settle_interrupted(request_id: int | None = None) -> None:
    """Resolve requests left `creating` by an interrupted registration: created if the agent was saved, else failed."""
    with db() as session:
        query = select(TeammateRequest).where(TeammateRequest.status == "creating")
        if request_id is not None:
            query = query.where(TeammateRequest.id == request_id)
        for request in session.exec(query).all():
            agent = session.exec(select(AgentProfile).where(
                func.lower(AgentProfile.name) == request.name.lower(),
                AgentProfile.created_by_agent == request.requested_by_agent_id)).first()
            if agent:
                request.status, request.agent_id = "created", agent.id
            else:
                request.status, request.error = "failed", INTERRUPTED
            session.add(request)
        session.commit()


def _matches(value: str, *candidates: str) -> bool:
    wanted = value.strip().lstrip("@").lower()
    return any(wanted == c.lower() for c in candidates if c)


def outside_agent(session, task: AgentTask, teammate: str) -> OutsideAgent | None:
    """An outside agent in this channel with that name or handle, unless a local teammate has the name."""
    if any(_matches(teammate, a.name, a.handle) for a in channel_teammates(session, task.channel_id)):
        return None
    return next((o for o in session.exec(select(OutsideAgent).where(OutsideAgent.channel_id == task.channel_id)).all()
                 if _matches(teammate, o.name, o.handle, o.handle.rsplit("/", 1)[-1])), None)


async def ask_outside(task: AgentTask, outside: OutsideAgent, request: str, call_id: str) -> dict:
    """Post a request to an outside agent in the Band room, at most once even if the tool call replays."""
    request = request.strip()
    if not request or len(request) > 8000:
        return {"error": "Provide a request of 1-8000 characters."}
    if not task.room_id:
        return {"error": "This channel has no Band room, so outside agents can't be reached."}
    source_key = f"{task.id}:{call_id}"
    with db() as session:
        if existing := session.get(OutsideHandoff, source_key):
            return _sent(outside, existing.status)
        if handoffs_used(session, handoff_root(session, task.id)) >= MAX_HANDOFFS:
            return {"error": f"This request already used {MAX_HANDOFFS} hand-offs. "
                             "Finish the work yourself and answer the humans."}
        sender = session.get(AgentProfile, task.agent_id)
        session.add_all([
            OutsideHandoff(source_key=source_key, task_id=task.id, channel_id=task.channel_id,
                           band_id=outside.band_id, request=request),
            Message(channel_id=task.channel_id, author_type="agent", author_id=sender.id, author_name=sender.name,
                    content=f"@{outside.name} {request}", to_humans=False, band_message_id=f"handoff:{call_id}",
                    mentions=[{"type": "external", "id": None, "name": outside.name}]),
        ])
        record_agent(task, "handoff", session=session, ref=f"handoff:{call_id}", teammate=outside.name,
                     request=request, outside=True)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return _sent(outside, session.get(OutsideHandoff, source_key).status)
    content = (f"@{outside.name} {request}\n\n"
               f"(From {sender.name} in BAND_BOTS. Reply in this room and mention @{sender.name} so it gets your answer.)")
    client = AsyncRestClient(api_key=decrypt_secret(sender.band_api_key_enc), base_url=settings.band_rest_url)
    status, band_message_id = "unknown", ""
    try:
        response = await client.agent_api_messages.create_agent_chat_message(
            task.room_id, message=ChatMessageRequest(content=content, mentions=[
                ChatMessageRequestMentionsItem(id=outside.band_id)]),
            request_options={"max_retries": 0, "timeout_in_seconds": 30})
        status, band_message_id = "sent", response.data.id
    except Exception:
        logger.exception("Outside hand-off from task %s may not have reached Band", task.id)
    finally:
        # Also runs when the task is cancelled mid-send, so the row never stays "sending".
        with db() as session:
            session.exec(update(OutsideHandoff).where(OutsideHandoff.source_key == source_key).values(
                status=status, band_message_id=band_message_id))
            session.commit()
        await aclose_rest_client(client)
    return _sent(outside, status)


def outside_reply(session, agent_id: int, channel_id: int, sender_id: str, prompt: str) -> tuple[str, str | None]:
    """Frame an outside agent's mention as the answer to this agent's latest confirmed request to it.

    Also returns the requesting task's id, or None when the mention answers no request of this agent.
    """
    handoff = session.exec(select(OutsideHandoff).where(
        OutsideHandoff.channel_id == channel_id, OutsideHandoff.band_id == sender_id,
        OutsideHandoff.status == "sent").order_by(OutsideHandoff.created_at.desc())).first()
    origin = session.get(AgentTask, handoff.task_id) if handoff else None
    if origin is None or origin.agent_id != agent_id:
        return prompt, None
    outsider = session.get(OutsideAgent, (channel_id, sender_id))
    name = outsider.name if outsider else "The outside agent"
    return (f"{name} (an outside Band agent) answered the request you sent it: \"{handoff.request}\"\n"
            f"You sent that request while working on: \"{origin.prompt[:2000]}\"\n\n"
            f"{name}'s reply:\n{prompt}\n\n"
            "Use the reply to finish that work and answer the humans."), origin.id


def link_outside_reply(task: AgentTask, origin_id: str) -> None:
    """Chain an outside agent's answer to the request that asked for it, so they share one hand-off budget."""
    with db() as session:
        if session.get(TaskHandoff, task.id) or task.id == origin_id:
            return
        session.add(TaskHandoff(task_id=task.id, root_task_id=handoff_root(session, origin_id),
                                parent_task_id=origin_id, from_agent_id=task.agent_id, kind="report",
                                report_back=False))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()


def _sent(outside: OutsideAgent, status: str) -> dict:
    # "sending" on a replay means the first attempt was interrupted before Band confirmed it.
    if status in ("unknown", "sending"):
        return {"error": f"Couldn't confirm the request reached {outside.name} through Band. Don't resend it; "
                         "tell the humans it may not have arrived."}
    return {"status": "handed_off", "teammate": outside.name, "outside": True,
            "note": (f"{outside.name} runs outside this app. Its reply appears in the channel, and it comes back "
                     "to you as a new task only if it mentions you. End your turn with a one-line status.")}
