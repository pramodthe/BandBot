"""Worker heartbeat and authenticated service status. Public health stays minimal."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import select

from app.agent_connections import runtime_available
from app.config import settings
from app.persistence.models import (as_utc, AgentConnection, AgentProfile, AgentRuntime, ConnectionHealth, User,
                                    WorkerHeartbeat, db, utcnow)

_api_started_at: datetime | None = None


def mark_api_started() -> None:
    global _api_started_at
    _api_started_at = utcnow()


def beat(status: str) -> None:
    """Record that this worker process is alive. Idle and busy workers both beat."""
    with db() as session:
        row = session.get(WorkerHeartbeat, 1) or WorkerHeartbeat(id=1, started_at=utcnow())
        row.beat_at = utcnow()
        row.status = status if status in {"idle", "busy"} else "idle"
        session.add(row)
        session.commit()


def worker_state(*, now: datetime | None = None, stale_seconds: int | None = None, grace_seconds: int | None = None) -> str:
    moment = now or utcnow()
    stale = settings.worker_heartbeat_stale if stale_seconds is None else stale_seconds
    grace = settings.worker_startup_grace if grace_seconds is None else grace_seconds
    with db() as session:
        row = session.get(WorkerHeartbeat, 1)
    if row is not None and (moment - as_utc(row.beat_at)).total_seconds() <= stale:
        return "busy" if row.status == "busy" else "ready"
    if _api_started_at is not None and (moment - as_utc(_api_started_at)).total_seconds() <= grace:
        return "starting"
    return "unavailable"


def service_status(viewer: User) -> dict:
    """Authenticated diagnostics. Provider secrets and raw errors are never included."""
    providers = []
    with db() as session:
        runtimes = session.exec(select(AgentRuntime)).all()
        for runtime in runtimes:
            profile = session.get(AgentProfile, runtime.agent_id)
            connection = session.get(AgentConnection, runtime.connection_id)
            if profile is None or connection is None:
                continue
            health = session.get(ConnectionHealth, connection.id)
            if health is not None:
                state = health.state if health.state in {"ready", "sign_in_required", "unavailable"} else "unavailable"
            else:
                state = "ready" if runtime_available(runtime.provider) or runtime.provider == "deepagents" else "unavailable"
            item = {"agent_id": profile.id, "name": profile.name, "provider": runtime.provider, "state": state}
            if state == "sign_in_required" and connection.created_by == viewer.id:
                item["action"] = "reconnect"
            providers.append(item)
    return {
        "api": "ok",
        "human_chat": "ok",
        "worker": worker_state(),
        "providers": providers,
    }


def remember_provider_state(connection_id: str, state: str) -> None:
    if state not in {"ready", "sign_in_required", "unavailable"}:
        state = "unavailable"
    with db() as session:
        row = session.get(ConnectionHealth, connection_id) or ConnectionHealth(connection_id=connection_id)
        row.state = state
        row.detail = state
        session.add(row)
        session.commit()
