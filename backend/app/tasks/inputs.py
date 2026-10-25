"""Publish checkpointed questions and resume their original graph with validated human input."""
from __future__ import annotations

import json

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.persistence.models import (AgentProfile, AgentTask, ComponentResponse, Message, MessageComponent,
                                    TaskApproval, TaskInput, utcnow)
from app.runtime.ui import validate_answer, validate_ui
from app.workspace.audit import record, record_agent


def pending_inputs(session, task_id: str) -> bool:
    return session.exec(select(TaskInput.id).where(TaskInput.task_id == task_id,
                                                   TaskInput.status == "pending")).first() is not None


def requeue_if_ready(session, task_id: str) -> None:
    pending_approval = session.exec(select(TaskApproval.id).where(TaskApproval.task_id == task_id,
                                                                 TaskApproval.status == "pending")).first()
    if not pending_approval and not pending_inputs(session, task_id):
        session.exec(update(AgentTask).where(AgentTask.id == task_id, AgentTask.status == "waiting",
                                             AgentTask.cancel_requested == False).values(
            status="queued", progress="Input received; resuming saved work", updated_at=utcnow()))


def publish_input(session, task: AgentTask, interrupt_id: str, value: dict) -> None:
    if session.get(TaskInput, interrupt_id):
        return
    agent = session.get(AgentProfile, task.agent_id)
    component = session.exec(select(MessageComponent).where(MessageComponent.task_id == task.id,
                              MessageComponent.tool_call_id == value["tool_call_id"])).first()
    if component is None:
        message = Message(channel_id=task.channel_id, author_type="agent", author_id=task.agent_id,
                          author_name=agent.name if agent else "Agent", to_humans=True,
                          content="I need your input to continue.", band_message_id=f"input:{interrupt_id}")
        session.add(message)
        session.flush()
        component = MessageComponent(task_id=task.id, tool_call_id=value["tool_call_id"],
                                     message_id=message.id, kind="input", props=value["props"])
        session.add(component)
        session.flush()
    session.add(TaskInput(id=interrupt_id, task_id=task.id, component_id=component.id))
    record_agent(task, "input_requested", session=session, ref=f"input:{interrupt_id}", component_id=component.id)


def input_state(session, request: TaskInput) -> dict:
    task = session.get(AgentTask, request.task_id)
    state = "answered" if request.status == "answered" else (
        "pending" if task and task.status in {"waiting", "queued", "running"} and not task.cancel_requested else "closed")
    return {"id": request.id, "taskId": request.task_id, "status": state}


def input_decision(session, request: TaskInput) -> dict | None:
    response = session.get(ComponentResponse, request.component_id)
    if request.status != "answered" or response is None:
        return None
    return {"values": response.values["answers"], "steering": response.values["steering"],
            "answered_by": response.user_name,
            "instruction": "Use these human inputs to continue this same task. They grant no external-action approval."}


def answer_input(session, channel_id: int, component_id: int, user, values: dict, steering: str):
    from app.workspace.generative import UIError, component_in_channel
    component = component_in_channel(session, channel_id, component_id, user)
    request = session.exec(select(TaskInput).where(TaskInput.component_id == component_id)).first()
    task = session.get(AgentTask, request.task_id) if request else None
    if component.kind != "input" or not task:
        raise UIError("Input request not found", 404)
    from app.computer.access import require_computer_owner
    require_computer_owner(session, task, user.id)
    try:
        clean = validate_answer(component.props["ui"], component.props["required"], values)
    except (ValueError, OverflowError) as exc:
        raise UIError(str(exc)) from exc
    payload = {"answers": clean, "steering": steering.strip()}
    existing = session.get(ComponentResponse, component_id)
    if existing:
        if existing.user_id == user.id and existing.values == payload:
            return component, existing, False
        raise UIError(f"{existing.user_name} already answered this request", 409)
    if request.status != "pending" or task.status != "waiting" or task.cancel_requested:
        raise UIError("This task is no longer waiting for input", 409)
    # CAS serializes answer, cancellation, and approval races before any message or receipt is written.
    claimed = session.exec(update(AgentTask).where(AgentTask.id == task.id, AgentTask.status == "waiting",
                                                   AgentTask.cancel_requested == False).values(updated_at=utcnow()))
    if not claimed.rowcount:
        raise UIError("This task is no longer waiting for input", 409)
    response = ComponentResponse(component_id=component_id, user_id=user.id, user_name=user.display_name,
                                 action_id="continue", label="Continue", values=payload)
    session.add(response)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return answer_input(session, channel_id, component_id, user, values, steering)
    labels = {node["name"]: node.get("label", node["name"]) for node in
              validate_ui(component.props["ui"], input_mode=True).values()}
    summary = "\n".join(f"- {labels[name]}: {json.dumps(value, ensure_ascii=False)}" for name, value in clean.items())
    message = Message(channel_id=channel_id, author_type="human", author_id=user.id, author_name=user.display_name,
                      content=f"Response to {component.props['title']}\n{summary}" + (f"\nSteering: {steering.strip()}" if steering.strip() else ""),
                      band_message_id=f"input-answer:{request.id}")
    session.add(message)
    session.flush()
    response.message_id = message.id
    request.status = "answered"
    session.add_all([request, response])
    session.flush()
    requeue_if_ready(session, task.id)
    record("input_answered", actor_type="human", actor_id=user.id, actor_name=user.display_name,
           channel_id=channel_id, task_id=task.id, session=session, detail={"component_id": component_id})
    session.commit()
    return component, response, True
