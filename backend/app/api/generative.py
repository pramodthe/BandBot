"""Channel-scoped generated views and shared decision responses."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.api.deps import NO_STORE
from app.auth import current_user
from app.persistence.models import (AgentTask, ComponentResponse, Document, ImageGeneration, ImageGenerationDisplay,
                                    Message, MessageComponent, TaskInput, User, db, get_session)
from app.providers.models import ProviderOperation
from app.workspace.components import current_components
from app.tasks.inputs import answer_input, input_state
from app.workspace.events import bus
from app.workspace.generative import UIError, component_in_channel, member_channel, response_json, submit

router = APIRouter(prefix="/api/channels/{channel_id}", tags=["generated-ui"])


def check(operation, *args):
    try:
        return operation(*args)
    except UIError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/elements")
def elements(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    from app.runtime.elements import ELEMENTS, EXAMPLES
    from app.runtime.ui import CATALOG
    check(member_channel, session, channel_id, user)
    return {"elements": ELEMENTS, "examples": EXAMPLES, "components": CATALOG["components"]}


@router.get("/views")
def views(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    check(member_channel, session, channel_id, user)
    rows = session.exec(select(MessageComponent, Message).join(Message, Message.id == MessageComponent.message_id)
                        .where(Message.channel_id == channel_id, MessageComponent.kind.in_(["view", "ui", "sandbox"]), current_components())
                        .order_by(MessageComponent.id.desc()).limit(50)).all()
    return [{"id": component.id, "kind": component.kind, "props": component.props,
             "message_id": message.id, "author_name": message.author_name,
             "created_at": message.created_at.isoformat()} for component, message in rows]


@router.get("/image-generations")
def image_generations(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    check(member_channel, session, channel_id, user)
    result = []

    def append(identity, display, task, state, document_id, model):
        status = "generating" if state in {"pending", "running"} and task.status == "running" else "error"
        error = "Image generation stopped or paused. Check the task before trying again."
        if state == "completed":
            doc = session.get(Document, document_id)
            if doc and doc.channel_id == channel_id and doc.status not in {"deleting", "deleted"}:
                status, document_id = "ready", doc.id
            else:
                error = "The generated image is no longer available."
                document_id = None
        elif state in {"failed", "uncertain"}:
            error = "Image generation could not finish. Check the agent's response before trying again."
        result.append({"id": identity, "task_id": task.id, "agent_id": task.agent_id,
                       "title": display.title, "size": display.size, "status": status,
                       "document_id": document_id, "model": model,
                       "error": error if status == "error" else ""})

    # Retain display/recovery compatibility with images created by the local preview.
    legacy = session.exec(select(ImageGeneration, ImageGenerationDisplay, AgentTask)
        .join(ImageGenerationDisplay, ImageGenerationDisplay.id == ImageGeneration.id)
        .join(AgentTask, AgentTask.id == ImageGeneration.task_id)
        .where(AgentTask.channel_id == channel_id, AgentTask.reply_message_id == None,
               AgentTask.status.in_(["queued", "running", "waiting"]))
        .order_by(ImageGeneration.created_at).limit(20)).all()
    for receipt, display, task in legacy:
        append(receipt.id, display, task, receipt.status, receipt.document_id, receipt.model)
    rows = session.exec(select(ProviderOperation, ImageGenerationDisplay, AgentTask)
        .join(ImageGenerationDisplay, ImageGenerationDisplay.id == ProviderOperation.id)
        .join(AgentTask, AgentTask.id == ProviderOperation.task_id)
        .where(AgentTask.channel_id == channel_id, AgentTask.reply_message_id == None,
               AgentTask.status.in_(["queued", "running", "waiting"]), ProviderOperation.service == "images")
        .order_by(ProviderOperation.created_at).limit(20)).all()
    for receipt, display, task in rows:
        append(receipt.id, display, task, receipt.status, receipt.result.get("document_id"), receipt.binding.get("model", ""))
    return JSONResponse(result, headers=NO_STORE)


@router.get("/components/{component_id}")
def component_state(channel_id: int, component_id: int, user: User = Depends(current_user),
                    session: Session = Depends(get_session)):
    component = check(component_in_channel, session, channel_id, component_id, user)
    response = session.get(ComponentResponse, component_id)
    request = session.exec(select(TaskInput).where(TaskInput.component_id == component_id)).first()
    return {"id": component.id, "kind": component.kind, "props": component.props,
            "response": response_json(response) if response else None,
            "request": input_state(session, request) if request else None}


class InputBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict, max_length=24)
    steering: str = Field(default="", max_length=2000)


class SandboxPromptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")
    text: str = Field(min_length=1, max_length=4000)


@router.post("/components/{component_id}/followup")
async def sandbox_followup(channel_id: int, component_id: int, body: SandboxPromptBody, user: User = Depends(current_user)):
    from app.workspace.sandbox_ui import followup
    from app.workspace.service import serialize_messages, service
    with db() as session:
        result = check(followup, session, channel_id, component_id, user, body.request_id, body.text,
                       service.message_reply_to(channel_id))
        message = serialize_messages(session, [result.message])[0]
        task_id = result.receipt.task_id
    await bus.publish({"type": "message", "message": message})
    if result.created:
        service.relay_later(channel_id, result.message.id, user.display_name, result.message.content, [result.agent])
    return {"message": message, "task_id": task_id}


@router.post("/components/{component_id}/answer")
async def answer(channel_id: int, component_id: int, body: InputBody, user: User = Depends(current_user)):
    from app.workspace.service import serialize_messages
    with db() as session:
        component, response, _created = check(answer_input, session, channel_id, component_id, user,
                                             body.values, body.steering)
        source, message = serialize_messages(session, [session.get(Message, component.message_id),
                                                       session.get(Message, response.message_id)])
    await bus.publish({"type": "message_updated", "message": source})
    await bus.publish({"type": "message", "message": message})
    return {"response": response_json(response), "message": message}


class ResponseBody(BaseModel):
    action_id: str = Field(min_length=1, max_length=40)
    values: dict[str, Any] = Field(default_factory=dict, max_length=12)


@router.post("/components/{component_id}/respond")
async def respond(channel_id: int, component_id: int, body: ResponseBody, user: User = Depends(current_user)):
    # Imported here to keep the API router independent of service construction.
    from app.workspace.service import serialize_messages, service
    with db() as session:
        result = check(submit, session, channel_id, component_id, user, body.action_id, body.values,
                       service.message_reply_to(channel_id))
        source, message = serialize_messages(session, [result.source, result.message])
    # Re-publish even on an idempotent retry, to repair clients after a lost HTTP/socket response.
    await bus.publish({"type": "message_updated", "message": source})
    await bus.publish({"type": "message", "message": message})
    if result.created:
        await service.publish_channel(channel_id)
        service.relay_later(channel_id, result.message.id, user.display_name, result.message.content, [result.agent])
    return {"response": response_json(result.response), "message": message}
