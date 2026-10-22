"""Authenticated skill library and agent skill management."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth import current_user
from app.persistence.models import AgentProfile, AgentSkill, SkillDefinition, User, get_session, utcnow
from app.workspace.audit import record
from app.workspace.events import bus
from app.workspace.permissions import is_admin
from app.workspace.access import agent_owner, agent_visible
from app.workspace.skills import (MAX_UPLOAD, SkillContent, SkillError, check_install, create_skill, export_skill,
                                  parse_bundle, parse_upload, seed_library, serialize_skill, skill_or_error)

router = APIRouter(prefix="/api", tags=["skills"])
NO_STORE = {"Cache-Control": "no-store"}


def manager(user: User = Depends(current_user), session: Session = Depends(get_session)) -> User:
    if not is_admin(session, user.id):
        raise HTTPException(403, "Only workspace admins can manage skills")
    return user


def get_skill(session: Session, skill_id: str) -> SkillDefinition:
    try:
        return skill_or_error(session, skill_id)
    except SkillError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def check_agent_owner(session: Session, agent_id: int | None, user: User) -> None:
    profile = session.get(AgentProfile, agent_id) if agent_id else None
    if profile and agent_owner(session, profile) not in {None, user.id}:
        raise HTTPException(403, "Only this agent's owner can change its skills")


def commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "A skill with that name already exists") from exc


def audit(session: Session, user: User, action: str, skill: SkillDefinition, **details) -> None:
    record(action, actor_type="human", actor_id=user.id, actor_name=user.display_name,
           session=session, detail={"skill": skill.name, **details})


async def changed() -> None:
    # No skill contents travel over the shared socket; clients reload their catalogue.
    await bus.publish({"type": "skills_changed"})


@router.get("/agents/{agent_id}/skills")
def agent_skills(agent_id: int, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    profile = session.get(AgentProfile, agent_id)
    if profile is None or not agent_visible(session, profile, _user.id):
        raise HTTPException(404, "Agent not found")
    seed_library(session)
    library = session.exec(select(SkillDefinition).order_by(SkillDefinition.name)).all()
    installed = session.exec(select(AgentSkill).where(AgentSkill.agent_id == agent_id)).all()
    return {"library": [serialize_skill(session, skill) for skill in library],
            "installed": [{"skill_id": row.skill_id, "enabled": row.enabled} for row in installed]}


@router.get("/skills/{skill_id}")
def skill_detail(skill_id: str, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    return serialize_skill(session, get_skill(session, skill_id), detail=True)


class CreateBody(SkillContent):
    agent_id: int | None = None


@router.post("/skills")
async def add_skill(body: CreateBody, user: User = Depends(manager), session: Session = Depends(get_session)):
    check_agent_owner(session, body.agent_id, user)
    try:
        skill = create_skill(session, SkillContent(**body.model_dump()), user.id, agent_id=body.agent_id)
        audit(session, user, "skill_created", skill, agent_id=body.agent_id)
        commit(session)
    except SkillError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await changed()
    return serialize_skill(session, skill, detail=True)


@router.post("/skills/import")
async def import_skill(request: Request, filename: str = "", agent_id: int | None = None,
                       user: User = Depends(manager), session: Session = Depends(get_session)):
    check_agent_owner(session, agent_id, user)
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_UPLOAD:
            raise HTTPException(413, "Skill uploads must be at most 2 MiB")
    try:
        if request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
            bundle = json.loads(payload)
            if not isinstance(bundle, dict) or not isinstance(bundle.get("files"), list):
                raise SkillError("A folder import needs a list of files")
            content, files = parse_bundle(bundle["files"])
        else:
            content, files = parse_upload(bytes(payload), filename)
        skill = create_skill(session, content, user.id, files=files, agent_id=agent_id)
        audit(session, user, "skill_imported", skill, agent_id=agent_id, files=len(files) + 1)
        commit(session)
    except SkillError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, "SKILL.md needs a valid name, description, and nonempty instructions") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise HTTPException(422, "The skill folder data is invalid") from exc
    await changed()
    return serialize_skill(session, skill, detail=True)


class UpdateBody(SkillContent):
    expected_updated_at: str | None = None


@router.put("/skills/{skill_id}")
async def edit_skill(skill_id: str, body: UpdateBody, user: User = Depends(manager),
                     session: Session = Depends(get_session)):
    skill = get_skill(session, skill_id)
    if skill.builtin:
        raise HTTPException(403, "Built-in skills are read-only; create a copy to customize one")
    if body.expected_updated_at is not None and body.expected_updated_at != skill.updated_at.isoformat():
        raise HTTPException(409, "This skill changed elsewhere. Reopen it before saving your changes")
    for key, value in body.model_dump(exclude={"expected_updated_at"}).items():
        setattr(skill, key, value)
    skill.updated_at = utcnow()
    session.add(skill)
    audit(session, user, "skill_updated", skill)
    commit(session)
    await changed()
    return serialize_skill(session, skill, detail=True)


@router.delete("/skills/{skill_id}")
async def delete_skill(skill_id: str, user: User = Depends(manager), session: Session = Depends(get_session)):
    skill = get_skill(session, skill_id)
    if skill.builtin:
        raise HTTPException(403, "Built-in skills stay in the library; remove them from an agent instead")
    for row in session.exec(select(AgentSkill).where(AgentSkill.skill_id == skill.id)).all():
        session.delete(row)
    audit(session, user, "skill_deleted", skill)
    session.delete(skill)
    session.commit()
    await changed()
    return {"ok": True}


@router.get("/skills/{skill_id}/export")
def download_skill(skill_id: str, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    skill = get_skill(session, skill_id)
    return Response(export_skill(skill), media_type="application/zip", headers={
        **NO_STORE, "Content-Disposition": f'attachment; filename="{skill.name}.zip"'})


class InstallBody(BaseModel):
    skill_id: str


@router.post("/agents/{agent_id}/skills")
async def install_skill(agent_id: int, body: InstallBody, user: User = Depends(manager),
                        session: Session = Depends(get_session)):
    check_agent_owner(session, agent_id, user)
    skill = get_skill(session, body.skill_id)
    try:
        check_install(session, agent_id, skill.id)
    except SkillError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    if session.get(AgentSkill, (agent_id, skill.id)) is None:
        session.add(AgentSkill(agent_id=agent_id, skill_id=skill.id))
        audit(session, user, "skill_installed", skill, agent_id=agent_id)
        commit(session)
        await changed()
    return {"skill_id": skill.id, "enabled": session.get(AgentSkill, (agent_id, skill.id)).enabled}


class EnableBody(BaseModel):
    enabled: bool


@router.patch("/agents/{agent_id}/skills/{skill_id}")
async def enable_skill(agent_id: int, skill_id: str, body: EnableBody, user: User = Depends(manager),
                       session: Session = Depends(get_session)):
    check_agent_owner(session, agent_id, user)
    skill = get_skill(session, skill_id)
    row = session.get(AgentSkill, (agent_id, skill.id))
    if row is None:
        raise HTTPException(404, "This skill is not installed on the agent")
    row.enabled = body.enabled
    session.add(row)
    audit(session, user, "skill_enabled" if body.enabled else "skill_disabled", skill, agent_id=agent_id)
    session.commit()
    await changed()
    return {"skill_id": skill.id, "enabled": row.enabled}


@router.delete("/agents/{agent_id}/skills/{skill_id}")
async def uninstall_skill(agent_id: int, skill_id: str, user: User = Depends(manager),
                          session: Session = Depends(get_session)):
    check_agent_owner(session, agent_id, user)
    skill = get_skill(session, skill_id)
    row = session.get(AgentSkill, (agent_id, skill.id))
    if row is None:
        raise HTTPException(404, "This skill is not installed on the agent")
    session.delete(row)
    audit(session, user, "skill_removed", skill, agent_id=agent_id)
    session.commit()
    await changed()
    return {"ok": True}
