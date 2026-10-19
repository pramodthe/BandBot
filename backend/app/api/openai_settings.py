from __future__ import annotations

import base64
import io
import re

from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from PIL import Image
from pydantic import BaseModel, Field
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
    api_key: str | None = Field(default=None, max_length=1000)
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
    png = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(png, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode()
    async with AsyncOpenAI(api_key=key, timeout=60, max_retries=0) as sdk:
        for model in models:
            try:
                result = await sdk.responses.create(model=model, store=False, max_output_tokens=2048,
                    input=[{"role": "user", "content": [
                        {"type": "input_text", "text": "Call check with the image's color."},
                        {"type": "input_image", "image_url": url}]}],
                    tools=[{"type": "function", "name": "check", "description": "Report color", "strict": True,
                            "parameters": {"type": "object", "properties": {"color": {"type": "string"}},
                                           "required": ["color"], "additionalProperties": False}},
                           {"type": "code_interpreter", "container": {"type": "auto"}}],
                    tool_choice={"type": "function", "name": "check"})
                if not any(item.type == "function_call" and item.name == "check" for item in result.output):
                    raise ValueError("Function call missing")
            except Exception:
                raise HTTPException(422, f"Could not validate {model}. Check model access, image/tool support, and API billing.") from None


@router.put("")
async def save_settings(body: SettingsBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_admin(session, user)
    models = list(dict.fromkeys(m.strip() for m in body.models))
    if any(not re.fullmatch(r"[a-zA-Z0-9._:-]{1,120}", m) for m in models) or body.default_model not in models:
        raise HTTPException(422, "Choose a default from the allowed OpenAI models")
    old_key, _, _ = configuration()
    key = body.api_key.strip() if body.api_key else old_key
    if not key:
        raise HTTPException(422, "An OpenAI API key is required")
    await validate_models(key, models)
    session.merge(OpenAISettings(id=1, api_key_enc=encrypt_secret(key), models=models, default_model=body.default_model))
    record("openai_settings_updated", actor_type="human", actor_id=user.id, actor_name=user.display_name,
           detail={"models": models, "default_model": body.default_model}, session=session)
    session.commit()
    await bus.publish({"type": "openai_settings", "models": model_catalog(), "configured": True})
    from app.workspace.service import service
    if service.org.online:
        for profile in session.exec(select(AgentProfile)).all():
            await service.start_agent(profile)
    return get_settings(user, session)
