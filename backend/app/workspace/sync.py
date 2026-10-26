"""Orders bootstrap snapshots against live WebSocket events.

The counter lives in its own table, created by the Alembic baseline and reconcile revisions.
It does not make the in-memory event bus safe for multiple API processes.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session

from app.persistence.models import SyncCursor


def current_cursor(session: Session) -> int:
    row = session.get(SyncCursor, 1)
    return int(row.value) if row else 0


def advance_cursor(session: Session) -> int:
    """Atomically allocate the next cursor. The caller commits."""
    value = session.execute(text(
        "INSERT INTO sync_cursor (id, value) VALUES (1, 1) "
        "ON CONFLICT(id) DO UPDATE SET value = sync_cursor.value + 1 "
        "RETURNING value"
    )).scalar_one()
    return int(value)
