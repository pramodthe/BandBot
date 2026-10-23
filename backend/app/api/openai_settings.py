from __future__ import annotations

import base64
import io
import re

from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from PIL import Image
from pydantic import BaseModel, Field, SecretStr
from sqlmodel import Session, select

from app.auth import current_user, encrypt_secret
from app.config import MODELS
from app.openai_provider import configuration, model_catalog
from app.persistence.models import AgentProfile, OpenAISettings, User, get_session
from app.workspace.audit import record
from app.workspace.events import bus
from app.workspace.permissions import is_admin

router = APIRouter(prefix="/api/settings/openai")


class SettingsBody(BaseModel):
    api_key: SecretStr | None = Field(default=None, max_length=4000)
    models: list[str] = Field(min_length=1, max_length=12)
    default_model: str


def require_admin(session, user):
    if not is_admin(session, user.id):
        raise HTTPException(403, "Only workspace admins can configure OpenAI")


@router.get("")
def get_settings(user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_admin(session, user)
    key, models, default = configuration()
    return {"configured": bool(key), "models": models, "default_model": default, "suggested_models": MODELS}


async def validate_models(key: str, models: list[str]) -> None:
    """A small, explicit setup test verifies images and function calling, not just a model's name."""
    from app.providers.language_models import verify_models
    from app.providers.contracts import ResolvedConnection
    try:
        await verify_models(ResolvedConnection("compatibility-api", 1, "openai", {"models": models}, {"api_key": key}, {}), models)
    except Exception:
        raise HTTPException(422, "Could not verify image inputs, a complete tool round trip, usage, or model access. Check credentials and billing.") from None


@router.put("")
async def save_settings(body: SettingsBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_admin(session, user)
    models = list(dict.fromkeys(m.strip() for m in body.models))
    if any(not re.fullmatch(r"[a-zA-Z0-9._:-]{1,120}", m) for m in models) or body.default_model not in models:
        raise HTTPException(422, "Choose a default from the allowed OpenAI models")
    old_key, _, _ = configuration()
    key = body.api_key.get_secret_value().strip() if body.api_key else old_key
    if not key:
        raise HTTPException(422, "An OpenAI API key is required")
    from app.providers.models import ServiceSelection
    original_selection = session.get(ServiceSelection, "workspace")
    expected_version = original_selection.version if original_selection else 0
    await validate_models(key, models)
    from app.providers.management import create_connection, rotate, apply_selection
    from app.providers.models import ProviderConnection, ProviderRevision, ServiceSelection
    from app.providers.bindings import workspace_bindings
    bindings = workspace_bindings(session)
    identity = bindings.get("llm", {}).get("connection_id")
    row = session.get(ProviderConnection, identity) if identity else None
    if not row or row.provider != "openai" or row.status != "active":
        row = create_connection(session, provider="openai", label="OpenAI compatibility settings", key=key,
            config={"models": models}, user_id=user.id)
        number = 1
    else:
        number = rotate(session, row, expected_revision=row.current_revision, key=key, config={"models": models})
    revision = session.get(ProviderRevision, (row.id, number))
    revision.verification = {"passed": True, "models": {m: {"tools": True, "vision": True, "usage": True} for m in models}}
    session.add(revision)
    reference = {"connection_id": row.id, "revision": number, "provider": "openai", "model": body.default_model}
    # Only update services already following this legacy OpenAI connection.
    for service in ("llm", "document_analysis", "retrieval"):
        previous = bindings.get(service, {})
        if not previous or previous.get("connection_id") == row.id or previous.get("legacy"):
            bindings[service] = reference if service != "retrieval" else {k:v for k,v in reference.items() if k != "model"}
    current = session.get(ServiceSelection, "workspace")
    from app.providers.management import ConfigurationConflict, validate_bindings
    from app.providers.models import ServiceChange
    from app.persistence.models import Document
    try:
        if (current.version if current else 0) != expected_version:
            raise ConfigurationConflict("Settings changed during verification. Reload and try again.")
        validate_bindings(session, bindings)
        old_retrieval = current.bindings.get("retrieval", {}) if current else {}
        ready = session.exec(select(Document.id).where(Document.status == "ready")).first()
        if ready and old_retrieval != bindings.get("retrieval", {}):
            from uuid import uuid4
            session.add(ServiceChange(id=uuid4().hex, expected_version=expected_version, bindings=bindings))
        else:
            apply_selection(session, scope="workspace", expected_version=expected_version, bindings=bindings)
    except ValueError as error:
        session.rollback()
        raise HTTPException(409 if isinstance(error, ConfigurationConflict) else 422, str(error)) from None
    session.merge(OpenAISettings(id=1, api_key_enc=encrypt_secret(key), models=models, default_model=body.default_model))
    record("openai_settings_updated", actor_type="human", actor_id=user.id, actor_name=user.display_name,
           detail={"models": models, "default_model": body.default_model}, session=session)
    session.commit()
    await bus.publish({"type": "provider_settings_changed"})
    await bus.publish({"type": "openai_settings", "models": model_catalog(), "configured": True})
    from app.workspace.service import service
    if service.org.online:
        for profile in session.exec(select(AgentProfile)).all():
            await service.start_agent(profile)
    return get_settings(user, session)
