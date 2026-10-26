"""Provider authentication, encrypted storage, and replay-stable runtime selection."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from datetime import timedelta
from uuid import uuid4

import httpx

from app.auth import decrypt_secret, encrypt_secret
from app.config import settings
from app.persistence.models import AgentConnection, AgentRuntime, AgentTask, TaskModel, as_utc, db, utcnow

TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
MODEL_ID = re.compile(r"[a-zA-Z0-9._:/-]{1,120}")
CONNECTED_PROVIDERS = {"codex", "claude", "cursor", "openclaw"}
_refresh_lock = asyncio.Lock()


def runtime_for(agent_id: int) -> AgentRuntime | None:
    with db() as session:
        return session.get(AgentRuntime, agent_id)


def runtime_available(provider: str) -> bool:
    if provider == "codex":
        return bool(shutil.which(settings.codex_executable))
    if provider == "openclaw":
        from app.openclaw_gateway import gateway_urls
        return bool(gateway_urls())
    if provider == "cursor":
        try:
            from cursor_sdk._vendor import resolve_bridge_path
            from cursor_sdk.errors import CursorSDKError
        except ImportError:
            return False
        try:
            resolve_bridge_path()
            return True
        except (CursorSDKError, OSError):
            return False
    from importlib.util import find_spec
    return provider == "claude" and find_spec("claude_agent_sdk") is not None


def public_connection(row: AgentConnection) -> dict:
    return {"id": row.id, "provider": row.provider, "label": row.label, "auth_method": row.auth_method,
            "models": row.models}


async def validate_key(provider: str, key: str) -> list[str]:
    """Read-only authentication probe; no inference or automatic retries."""
    if provider == "cursor":
        from app.runtime.connected import run_step
        result = await run_step("cursor", "", key, {"operation": "catalog"})
        return sorted(item for item in result["models"] if MODEL_ID.fullmatch(item) and item != "auto-smart")
    if provider not in {"codex", "claude"}:
        raise ValueError("Unsupported key provider")
    url = "https://api.openai.com/v1/models" if provider == "codex" else "https://api.anthropic.com/v1/models"
    headers = ({"Authorization": f"Bearer {key}"} if provider == "codex" else
               {"x-api-key": key, "anthropic-version": "2023-06-01"})
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return sorted(item["id"] for item in response.json().get("data", [])
                      if MODEL_ID.fullmatch(item.get("id", "")))


def save_connection(provider: str, label: str, secret: dict, user_id: int, method: str = "api_key",
                    models: list[str] | None = None) -> AgentConnection:
    row = AgentConnection(id=uuid4().hex, provider=provider, label=label.strip(), auth_method=method,
                          secret_enc=encrypt_secret(json.dumps(secret)), created_by=user_id, models=models or [])
    with db() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


async def credential(connection_id: str) -> tuple[AgentConnection, str]:
    if connection_id.startswith("task:"):
        from app.providers.models import TaskServiceBinding
        from app.providers.contracts import ProviderError
        with db() as session:
            binding = session.get(TaskServiceBinding, connection_id[5:])
            selection = binding.bindings.get("llm", {}) if binding else {}
            row = session.get(AgentConnection, selection.get("connection_id")) if selection else None
        if row is None or row.provider != selection.get("provider"):
            raise ProviderError("llm", "missing", connection_id=selection.get("connection_id", ""))
        if row.auth_method in {"api_key", "gateway_token"}:
            secret = json.loads(decrypt_secret(binding.connected_secret_enc))
            return row.model_copy(update={"secret_enc": binding.connected_secret_enc}), secret["api_key"]
        return await credential(row.id)  # OAuth token refresh stays serialized on its original connection.
    # A single worker owns model execution. Serialize refreshes so rotating refresh tokens aren't reused.
    async with _refresh_lock:
        with db() as session:
            row = session.get(AgentConnection, connection_id)
        if row is None:
            raise ValueError("Agent connection was disconnected")
        secret = json.loads(decrypt_secret(row.secret_enc))
        if row.auth_method == "codex_chatgpt":
            return row, json.dumps(secret["auth_json"])
        if row.auth_method in {"api_key", "gateway_token"}:
            return row, secret["api_key"]
        if float(secret.get("expires_at", 0)) <= utcnow().timestamp() + 120:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(TOKEN_URL, data={"grant_type": "refresh_token",
                    "client_id": secret["client_id"], "refresh_token": secret["refresh_token"], "resource": RESOURCE})
                response.raise_for_status()
                refreshed = response.json()
            if "chatgpt.tokens.use.direct" not in refreshed.get("scope", secret.get("scope", "")).split():
                raise ValueError("ChatGPT plan usage permission is missing; reconnect Codex")
            secret.update({k: v for k, v in refreshed.items() if k in {"access_token", "refresh_token", "scope"}})
            secret["expires_at"] = utcnow().timestamp() + int(refreshed.get("expires_in", 3600))
            with db() as session:
                current = session.get(AgentConnection, connection_id)
                if current is None:
                    raise ValueError("Agent connection was disconnected")
                current.secret_enc = encrypt_secret(json.dumps(secret))
                session.add(current)
                session.commit()
        return row, secret["access_token"]


def pin_runtime(task: AgentTask, profile, runtime: AgentRuntime) -> str:
    with db() as session:
        pinned = session.get(TaskModel, task.id)
        if pinned and pinned.provider != runtime.provider:
            raise ValueError("The saved task uses a different runtime; restart it explicitly")
        if pinned is None:
            pinned = TaskModel(task_id=task.id, provider=runtime.provider, model=profile.model)
            session.add(pinned)
            session.commit()
        return pinned.model


def unexpired(at) -> bool:
    return as_utc(at) > utcnow() - timedelta(minutes=10)
