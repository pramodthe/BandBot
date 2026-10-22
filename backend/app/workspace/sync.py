"""Orders bootstrap snapshots against live WebSocket events.

The counter lives in its own table so existing installations pick it up with create_all.
It does not make the in-memory event bus safe for multiple API processes.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session

from app.persistence.models import SyncCursor

_ready = False


def ensure_sync_cursor(session: Session) -> None:
    global _ready
    if _ready:
        return
    SyncCursor.__table__.create(session.get_bind(), checkfirst=True)
    _ready = True


def current_cursor(session: Session) -> int:
    ensure_sync_cursor(session)
    row = session.get(SyncCursor, 1)
    return int(row.value) if row else 0


def advance_cursor(session: Session) -> int:
    """Atomically allocate the next cursor. The caller commits."""
    ensure_sync_cursor(session)
    value = session.execute(text(
        "INSERT INTO sync_cursor (id, value) VALUES (1, 1) "
        "ON CONFLICT(id) DO UPDATE SET value = sync_cursor.value + 1 "
        "RETURNING value"
    )).scalar_one()
    return int(value)
