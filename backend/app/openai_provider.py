"""One OpenAI connection, shared by the API and the worker. Secrets never leave the server."""
from __future__ import annotations

from openai import AsyncOpenAI
from sqlmodel import select

from app.auth import decrypt_secret
from app.config import MODELS, settings
from app.persistence.models import AgentProfile, AgentTask, OpenAISettings, TaskModel, db
from app.agent_connections import runtime_for


def configuration() -> tuple[str, list[str], str]:
    with db() as session:
        row = session.get(OpenAISettings, 1)
    key = decrypt_secret(row.api_key_enc) if row and row.api_key_enc else settings.openai_api_key
    models = row.models if row and row.models else [m["id"] for m in MODELS]
    default = row.default_model if row and row.default_model else settings.default_model
    if default not in models:
        default = models[0]
    return key, models, default


def model_catalog() -> list[dict]:
    labels = {m["id"]: m["label"] for m in MODELS}
    _, models, default = configuration()
    return [{"id": m, "label": labels.get(m, m), "default": m == default} for m in models]


def client() -> AsyncOpenAI:
    key, _, _ = configuration()
    if not key:
        raise ValueError("Configure OpenAI in workspace settings first")
    return AsyncOpenAI(api_key=key, timeout=90, max_retries=2)


def pin_task(task: AgentTask, profile: AgentProfile) -> str:
    with db() as session:
        pinned = session.get(TaskModel, task.id)
        if pinned and pinned.provider != "openai":
            raise LegacyCheckpoint("This Claude task needs an explicit restart under OpenAI")
        if not pinned:
            _, allowed, default = configuration()
            model = profile.model if profile.model in allowed else default
            pinned = TaskModel(task_id=task.id, model=model)
            session.add(pinned)
            session.commit()
        return pinned.model


class LegacyCheckpoint(Exception):
    pass


def migrate_profiles() -> None:
    """Preserve legacy checkpoints, but never replay their side effects with another provider."""
    _, _, default = configuration()
    with db() as session:
        for profile in session.exec(select(AgentProfile).where(AgentProfile.model.like("claude-%"))).all():
            if runtime_for(profile.id):
                continue
            for task in session.exec(select(AgentTask).where(AgentTask.agent_id == profile.id,
                                                             AgentTask.attempts > 0)).all():
                if not session.get(TaskModel, task.id):
                    session.add(TaskModel(task_id=task.id, model=profile.model, provider="anthropic"))
                if task.status in {"running", "queued"}:
                    task.status, task.progress = "failed", "Restart this task under OpenAI"
                    task.error = "Saved Claude progress is preserved; an explicit restart is required."
                    session.add(task)
            profile.model = default
            session.add(profile)
        session.commit()
