"""Durable jobs shared by the Band bridge, API, and standalone worker."""
from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.persistence.models import (AgentProfile, AgentTask, BandAgentLink, ChannelMember, DirectMessage, Message, OutsideHandoff,
                                    TaskApproval, TaskHandoff, db, utcnow)
from app.tasks.usage import over_budget
from app.workspace.audit import record, record_agent
from app.workspace.access import channel_teammates

# Teammate hand-offs (local or outside) allowed per original request; each also yields at most one report back.
MAX_HANDOFFS = 6


def serialize_task(task: AgentTask) -> dict:
    payload = task.model_dump(mode="json", exclude={"prompt", "context", "reply_to", "source_key", "result", "artifacts"})
    if task.status == "waiting":
        with db() as session:
            approval = session.exec(select(TaskApproval.id).where(TaskApproval.task_id == task.id,
                                                                  TaskApproval.status == "pending")).first()
        payload["waiting_for"] = "approval" if approval else "input"
    from app.providers.models import ProviderBlock, ProviderOperation
    with db() as session:
        blocked = session.get(ProviderBlock, task.id)
        restart = session.get(ProviderOperation, "restart:" + task.id)
    if restart:
        payload["restarted_from"] = restart.result.get("original_task_id")
    if blocked:
        payload["provider_block"] = {"service": blocked.service, "code": blocked.code, "detail": blocked.detail, "connection_id": blocked.connection_id}
    return payload


def enqueue(*, agent_id: int, channel_id: int, room_id: str, source_id: str,
            prompt: str, context: str = "", reply_to: str = "") -> AgentTask:
    source_key = f"{agent_id}:{room_id}:{source_id}"
    with db() as session:
        existing = session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).first()
        if existing:
            return existing
        task = AgentTask(id=uuid4().hex, source_key=source_key, agent_id=agent_id,
                         channel_id=channel_id, room_id=room_id, prompt=prompt,
                         context=context, reply_to=reply_to)
        session.add(task)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).one()
        session.refresh(task)
        return task


def update_task(task_id: str, **values) -> AgentTask:
    with db() as session:
        task = session.get(AgentTask, task_id)
        if task is None:
            raise LookupError(task_id)
        for key, value in values.items():
            setattr(task, key, value)
        task.updated_at = utcnow()
        session.add(task)
        session.commit()
        session.refresh(task)
        return task


def recover_tasks() -> None:
    """Call only while holding the exclusive worker lock."""
    from app.providers.verification import recover
    recover()
    with db() as session:
        for task in session.exec(select(AgentTask).where(AgentTask.status == "running")).all():
            task.status = "cancelled" if task.cancel_requested else "queued"
            task.progress = "Recovered after worker restart; resuming checkpoint"
            task.updated_at = utcnow()
            session.add(task)
        # An outside request interrupted mid-send may or may not have reached Band; it is never resent.
        session.exec(update(OutsideHandoff).where(OutsideHandoff.status == "sending").values(status="unknown"))
        session.commit()


def handoff_root(session, task_id: str) -> str:
    """The original request a task works on behalf of."""
    link = session.get(TaskHandoff, task_id)
    return link.root_task_id if link else task_id


def handoffs_used(session, root: str) -> int:
    """Local and outside hand-offs made for one original request, so neither kind can loop forever."""
    chain = [root, *session.exec(select(TaskHandoff.task_id).where(TaskHandoff.root_task_id == root)).all()]
    local = session.exec(select(TaskHandoff.task_id).where(
        TaskHandoff.root_task_id == root, TaskHandoff.kind == "handoff")).all()
    outside = session.exec(select(OutsideHandoff.source_key).where(OutsideHandoff.task_id.in_(chain))).all()
    return len(local) + len(outside)


def next_task() -> AgentTask | None:
    """Claim the oldest queued task whose agent is not already running in that channel.

    Keeping each agent sequential per channel preserves conversational order; other agents
    and channels run in parallel. A filesystem lock guarantees one worker for this deployment.
    """
    with db() as session:
        busy = {(t.agent_id, t.channel_id) for t in session.exec(
            select(AgentTask).where(AgentTask.status == "running")).all()}
        limits: dict[tuple[int, int], str | None] = {}
        task = None
        for queued in session.exec(select(AgentTask).where(AgentTask.status == "queued")
                                   .order_by(AgentTask.created_at)).all():
            from app.documents import processing
            if processing(queued):
                busy.add((queued.agent_id, queued.channel_id))
                queued.progress = "Waiting for document processing"
                session.add(queued)
                continue
            pair = (queued.agent_id, queued.channel_id)
            if pair in busy:
                continue
            if pair not in limits:
                limits[pair] = over_budget(session, *pair)
            if limits[pair] is None:
                task = queued
                break
            paused = f"Paused: {limits[pair]}. It starts after midnight UTC or when an admin raises the budget."
            if queued.progress != paused:
                queued.progress, queued.updated_at = paused, utcnow()
                session.add(queued)
        if task is None:
            session.commit()
            return None
        task.status = "running"
        task.progress = "Starting agent"
        task.attempts += 1
        task.updated_at = utcnow()
        session.add(task)
        session.commit()
        session.refresh(task)
        return task


def stop_for_budget(task: AgentTask, reason: str) -> None:
    """End a running task like a cancellation, and tell the channel why and how to resume it."""
    with db() as session:
        stopped = session.exec(update(AgentTask).where(AgentTask.id == task.id, AgentTask.status == "running")
                               .values(status="cancelled", progress=f"Stopped: {reason}"[:1000], updated_at=utcnow()))
        if stopped.rowcount:
            agent = session.get(AgentProfile, task.agent_id)
            session.add(Message(channel_id=task.channel_id, author_type="system", author_name="System",
                                content=f"{agent.name if agent else 'An agent'} stopped working: {reason}. "
                                        "When the budget allows, retry the task to resume from its saved progress.",
                                band_message_id=f"budget:{task.id}:{uuid4().hex[:8]}"))
            record("task_stopped", actor_type="system", actor_name="Budget", channel_id=task.channel_id,
                   task_id=task.id, detail={"reason": reason}, session=session)
        session.commit()


def display_names(session, text: str) -> str:
    """Replace agents' Band handles and raw mentions (@owner/master, @[[id]]) with display names, so agents reply with @Master."""
    for agent in session.exec(select(AgentProfile)).all():
        text = text.replace(f"@[[{agent.band_agent_id}]]", f"@{agent.name}")
        if agent.handle:
            text = re.sub(rf"@{re.escape(agent.handle)}(?![\w./-])", lambda _: f"@{agent.name}", text, flags=re.I)
    return text


def channel_context(session, channel_id: int) -> str:
    """Recent channel messages, bounded like Band turns; the harness treats them as untrusted."""
    recent = session.exec(select(Message).where(Message.channel_id == channel_id)
                          .order_by(Message.id.desc()).limit(30)).all()
    return display_names(session, "\n".join(f"{m.author_name}: {m.content}" for m in reversed(recent))[-24000:])


def hand_off(task: AgentTask, *, teammate: str, request: str, report_back: bool, call_id: str) -> dict:
    """Post a visible request to a teammate and queue their task atomically.

    The tool call id makes a replayed call (after a worker crash) return the original hand-off.
    """
    request = request.strip()
    if not request or len(request) > 8000:
        return {"error": "Provide a request of 1-8000 characters."}
    with db() as session:
        if session.get(DirectMessage, task.channel_id):
            return {"error": "This is a private one-on-one conversation, so teammates can't join it. "
                             "Use internal helpers, or suggest the person ask the teammate in a shared channel."}
        sender = session.get(AgentProfile, task.agent_id)
        others = [a for a in channel_teammates(session, task.channel_id) if a.id != task.agent_id]
        wanted = teammate.strip().lstrip("@").lower()
        target = next((a for a in others if wanted in (a.name.lower(), (a.handle or "").lower())), None)
        if sender is None or target is None:
            names = ", ".join(a.name for a in others) or "none"
            return {"error": f"No teammate named {teammate!r}. Available teammates: {names}. "
                             "Use internal helpers if none fit."}
        source_key = f"{target.id}:{task.room_id}:handoff-{call_id}"
        existing = session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).first()
        if existing:
            return {"status": "handed_off", "teammate": target.name, "task_id": existing.id}
        root = handoff_root(session, task.id)
        if handoffs_used(session, root) >= MAX_HANDOFFS:
            return {"error": f"This request already used {MAX_HANDOFFS} teammate hand-offs. "
                             "Finish the work yourself and answer the humans."}
        child = AgentTask(id=uuid4().hex, source_key=source_key, channel_id=task.channel_id,
                          agent_id=target.id, room_id="" if session.get(BandAgentLink, target.id) else task.room_id, prompt=f"**{sender.name}:** {request}",
                          context=channel_context(session, task.channel_id),
                          # A report-back answer goes to the requester, so it stays local instead of pinging Band.
                          reply_to="" if report_back or session.get(BandAgentLink, target.id) else task.reply_to)
        session.add_all([
            Message(channel_id=task.channel_id, author_type="agent", author_id=sender.id,
                    author_name=sender.name, content=f"@{target.name} {request}", to_humans=False,
                    mentions=[{"type": "agent", "id": target.id, "name": target.name}],
                    band_message_id=f"handoff:{call_id}"),
            child,
            TaskHandoff(task_id=child.id, root_task_id=root, parent_task_id=task.id,
                        from_agent_id=sender.id, kind="handoff", request=request, report_back=report_back),
        ])
        if not session.get(ChannelMember, (task.channel_id, "agent", target.id)):
            session.add(ChannelMember(channel_id=task.channel_id, member_type="agent", member_id=target.id))
        record_agent(task, "handoff", session=session, ref=f"handoff:{call_id}", teammate=target.name,
                     request=request, report_back=report_back)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).one()
            return {"status": "handed_off", "teammate": target.name, "task_id": existing.id}
    return {"status": "handed_off", "teammate": target.name, "task_id": child.id,
            "note": (f"{target.name} runs after your turn ends. " + (
                "You will get a new task with their answer. End your turn now with a one-line status."
                if report_back else f"{target.name} will answer the humans directly."))}


def report_target(session, task: AgentTask) -> tuple[TaskHandoff, AgentProfile] | None:
    """The requester waiting for this task's answer, if it was a report-back hand-off."""
    link = session.get(TaskHandoff, task.id)
    if link is None or link.kind != "handoff" or not link.report_back:
        return None
    requester = session.get(AgentProfile, link.from_agent_id)
    return (link, requester) if requester else None


def queue_report(session, task: AgentTask, link: TaskHandoff, teammate_name: str, result: str) -> None:
    """Called inside the completion transaction so an answer is reported back exactly once."""
    source_key = f"{link.from_agent_id}:{task.room_id}:report-{task.id}"
    if session.exec(select(AgentTask.id).where(AgentTask.source_key == source_key)).first():
        return
    root = session.get(AgentTask, link.root_task_id)
    parent = session.get(AgentTask, link.parent_task_id)
    # Sibling hand-offs from the same turn may still be running in parallel.
    waiting = session.exec(select(AgentProfile.name).join(AgentTask, AgentTask.agent_id == AgentProfile.id)
                           .join(TaskHandoff, TaskHandoff.task_id == AgentTask.id).where(
        TaskHandoff.parent_task_id == link.parent_task_id, TaskHandoff.kind == "handoff",
        AgentTask.id != task.id, AgentTask.status.in_(["queued", "running"]))).all()
    prompt = (f"{teammate_name} finished the work you handed off.\n\nYou asked:\n{link.request}\n\n"
              f"{teammate_name} answered:\n{result[:20000]}\n\n"
              + (f"The original request was:\n{root.prompt}\n\n" if root else "")
              + (f"Still working on your other hand-offs: {', '.join(sorted(set(waiting)))}. If you need their "
                 "results, reply with a one-line status; it is shown as agent chatter, not sent to the humans. "
                 "Each answer arrives as a new task. Earlier answers are in the room context.\n\n" if waiting else "")
              + "Continue the original request: combine the results, hand off the next step, "
                "or give the humans the final answer.")
    # While siblings are still working, the requester's reply is an interim status: local chatter, not Band.
    follow_up = AgentTask(id=uuid4().hex, source_key=source_key, channel_id=task.channel_id,
                          agent_id=link.from_agent_id, room_id=task.room_id, prompt=prompt,
                          context=channel_context(session, task.channel_id),
                          reply_to="" if waiting or parent is None else parent.reply_to)
    session.add_all([follow_up, TaskHandoff(task_id=follow_up.id, root_task_id=link.root_task_id,
                                            parent_task_id=task.id, from_agent_id=task.agent_id,
                                            kind="interim" if waiting else "report", report_back=False)])


def is_interim(session, task: AgentTask) -> bool:
    link = session.get(TaskHandoff, task.id)
    return link is not None and link.kind == "interim"


def await_approval(task: AgentTask, requests: list[tuple[str, dict]]) -> None:
    """Record paused actions, tell the channel, and park the task until every request is decided."""
    with db() as session:
        from app.tasks.inputs import publish_input
        profile = session.get(AgentProfile, task.agent_id)
        for approval_id, value in requests:
            if value.get("kind") == "user_input":
                publish_input(session, task, approval_id, value)
                continue
            if session.get(TaskApproval, approval_id):
                continue
            action = str(value.get("action") or "Unspecified action")[:500]
            details = str(value.get("details") or "")[:4000]
            if value.get("kind") == "private_computer_export":
                from app.computer.access import computer_owner_id
                from app.persistence.models import ComputerPublishApproval
                owner = computer_owner_id(session, task)
                if owner is None:
                    raise ValueError("Private computer export has no owner")
                session.add(ComputerPublishApproval(approval_id=approval_id, user_id=owner))
            session.add(TaskApproval(id=approval_id, task_id=task.id, channel_id=task.channel_id,
                                     agent_id=task.agent_id, action=action, details=details))
            session.add(Message(channel_id=task.channel_id, author_type="agent", author_id=task.agent_id,
                                author_name=profile.name if profile else "Agent", to_humans=True,
                                content=f"**Approval needed:** {action}" + (f"\n\n{details}" if details else "")
                                        + "\n\nApprove or deny it on this message.",
                                band_message_id=f"approval:{approval_id}"))
            record_agent(task, "approval_requested", session=session, ref=f"approval:{approval_id}",
                         action=action, details=details)
        # A cancellation requested while the task ran wins over waiting.
        session.exec(update(AgentTask).where(AgentTask.id == task.id, AgentTask.status == "running",
                                             AgentTask.cancel_requested == True)  # noqa: E712
                     .values(status="cancelled", progress="Cancelled", updated_at=utcnow()))
        session.exec(update(AgentTask).where(AgentTask.id == task.id, AgentTask.status == "running")
                     .values(status="waiting", progress="Waiting for your input" if any(
                         v.get("kind") == "user_input" for _, v in requests) else "Waiting for approval", updated_at=utcnow()))
        session.commit()


def approval_decisions(interrupt_ids: list[str]) -> dict[str, dict] | None:
    """Resume values keyed by interrupt id, or None while any request is undecided."""
    with db() as session:
        from app.persistence.models import TaskInput
        from app.tasks.inputs import input_decision
        decisions = {}
        for interrupt_id in interrupt_ids:
            if request := session.get(TaskInput, interrupt_id):
                decision = input_decision(session, request)
                if decision is None:
                    return None
                decisions[interrupt_id] = decision
            else:
                approval = session.get(TaskApproval, interrupt_id)
                if approval is None or approval.status == "pending":
                    return None
                decisions[interrupt_id] = {"approved": approval.status == "approved",
                                           "decided_by": approval.decided_by, "note": approval.note}
        return decisions


def decide_approval(approval_id: str, *, approve: bool, decided_by: str, note: str = "") -> TaskApproval | None:
    """Apply a decision once; requeue the task when its last pending request is decided.

    Only waiting tasks accept decisions, so a cancelled task's request cannot be approved.
    """
    with db() as session:
        approval = session.get(TaskApproval, approval_id)
        task = session.get(AgentTask, approval.task_id) if approval else None
        if task is None or task.status != "waiting":
            return None
        claimed = session.exec(update(TaskApproval).where(
            TaskApproval.id == approval_id, TaskApproval.status == "pending").values(
            status="approved" if approve else "denied", decided_by=decided_by, note=note[:2000],
            decided_at=utcnow()))
        if not claimed.rowcount:
            session.rollback()
            return None
        approval = session.get(TaskApproval, approval_id)
        from app.tasks.inputs import requeue_if_ready
        requeue_if_ready(session, approval.task_id)
        session.commit()
        session.refresh(approval)
        return approval
