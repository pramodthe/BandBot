"""The audit log: an ordered record of what people and agents did, shown in a channel's Activity view."""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.persistence.models import AgentProfile, AgentTask, AuditEvent, db

HIDDEN = "[hidden]"
SECRET_WORDS = ("apikey", "accesskey", "privatekey", "secret", "token", "passw", "pwd", "authorization", "cookie",
                "credential", "signature", "bearer")
# Each pattern keeps its first group and hides the rest; leaning toward hiding too much is fine.
SECRET_PATTERNS = [
    re.compile(r"()(?:\bbearer\s+[\w.~+/=-]+|\b(?:sk-|sk_|band_[a-z]_|ghp_|gho_|ghs_|github_pat_|xox[abprs]-|AKIA)"
               r"[\w-]{8,})", re.I),
    # URL user info: scheme://user:pass@host or scheme://token@host.
    re.compile(r"(\b[a-z][\w+.-]*://)[^/\s@\"']+@", re.I),
    # Header values of any scheme (Basic, Digest, token), up to the closing quote or the line end.
    re.compile(r"(\b(?:proxy-)?(?:authorization|cookie|set-cookie|x-api-key|x-auth-token|api-key)[\"']?\s*[:=]\s*)"
               r"[\"']?[^\"'\r\n]+", re.I),
    # key=value, key: value, and "key": "quoted value with spaces".
    re.compile(r"(\b(?:api[_-]?key|access[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|token|secret"
               r"|client[_-]?secret|passw(?:or)?d|pwd)[\"']?\s*[=:]\s*)(?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s&\"',}]+)",
               re.I),
    # Flags: --password value, --token=value, and an attached -p<password> (mysql, sshpass).
    re.compile(r"((?<!\S)--?(?:password|passwd|pass|token|secret|api-key)[= ]\s*)[^\s\"']+", re.I),
    re.compile(r"((?<![^\s\"'])-p)[^\s\"'-][^\s\"']*"),
]
MAX_VALUE = 200
MAX_ARGS = 800


def _secret_name(name: Any) -> bool:
    squashed = re.sub(r"[^a-z]", "", str(name).lower())
    return any(word in squashed for word in SECRET_WORDS)


def _mask_text(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + HIDDEN, text)
    return text


def _mask(value: Any) -> Any:
    """Hide values under secret-looking keys at any depth, and secrets inside strings."""
    if isinstance(value, dict):
        return {key: HIDDEN if _secret_name(key) else _mask(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(item) for item in value]
    if isinstance(value, str):
        return _mask_text(value)
    return value


def summarize(value: Any, limit: int = MAX_VALUE) -> str:
    if isinstance(value, str):
        text = _mask_text(value)
    else:
        text = json.dumps(_mask(value), default=lambda item: _mask_text(str(item)), ensure_ascii=False)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summarize_args(args: Any) -> dict[str, str]:
    """A short view of tool arguments; values of secret-looking arguments are never stored."""
    if not isinstance(args, dict):
        return {"input": summarize(args)}
    summary: dict[str, str] = {}
    used = 0
    for name, value in args.items():
        if used >= MAX_ARGS:
            summary["…"] = f"{len(args) - len(summary)} more"
            break
        if name in {"css", "html", "jsFunctions", "jsExpressions"}:
            summary[name] = "[interface source omitted]"
        else:
            summary[name] = HIDDEN if _secret_name(name) else summarize(value)
        used += len(summary[name])
    return summary


def record(action: str, /, *, actor_type: str, actor_name: str, actor_id: int | None = None,
           channel_id: int | None = None, task_id: str | None = None, detail: dict[str, Any] | None = None,
           ref: str | None = None, session: Session | None = None) -> None:
    """Add an event. With `session`, it commits with the caller's transaction; `ref` makes it once per task."""
    event = AuditEvent(action=action, actor_type=actor_type, actor_name=actor_name[:100], actor_id=actor_id,
                       channel_id=channel_id, task_id=task_id, ref=ref,
                       detail={k: summarize(v) if isinstance(v, str) else _mask(v) for k, v in (detail or {}).items()})
    if session is not None:
        session.add(event)
        return
    with db() as own:
        own.add(event)
        try:
            own.commit()
        except IntegrityError:
            own.rollback()


def record_agent(task: AgentTask, action: str, /, *, session: Session | None = None, ref: str | None = None,
                 **detail: Any) -> None:
    """Add an event done by the task's agent; see `record` for `session` and `ref`."""
    def add(target: Session) -> None:
        agent = target.get(AgentProfile, task.agent_id)
        record(action, actor_type="agent", actor_id=task.agent_id, actor_name=agent.name if agent else "Agent",
               channel_id=task.channel_id, task_id=task.id, detail=detail, ref=ref, session=target)

    if session is not None:
        add(session)
        return
    with db() as own:
        add(own)
        try:
            own.commit()
        except IntegrityError:
            own.rollback()


def serialize_event(event: AuditEvent) -> dict[str, Any]:
    return {"id": event.id, "at": event.at.isoformat(), "channel_id": event.channel_id, "task_id": event.task_id,
            "actor_type": event.actor_type, "actor_id": event.actor_id, "actor_name": event.actor_name,
            "action": event.action, "detail": event.detail}
