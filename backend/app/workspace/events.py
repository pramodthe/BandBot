from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable

from fastapi import WebSocket

from app.persistence.models import db
from app.workspace.sync import advance_cursor

logger = logging.getLogger(__name__)


def _channel_id(event: dict[str, Any]) -> int | None:
    for value in (event.get("channel_id"), (event.get("message") or {}).get("channel_id"),
                  (event.get("channel") or {}).get("id"), (event.get("task") or {}).get("channel_id")):
        if value is not None:
            return value
    return None


class EventBus:
    """Fans app events out to authorized members of each conversation."""

    def __init__(self) -> None:
        self._sockets: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # None preserves the audience of legacy shared-workspace channels.
        self.audience: Callable[[int], int | set[int] | None] = lambda _channel_id: None
        self.agent_visible: Callable[[int, int], bool] = lambda _uid, _aid: True

    @property
    def online_user_ids(self) -> list[int]:
        return [uid for uid, sockets in self._sockets.items() if sockets]

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            first = not self._sockets[user_id]
            self._sockets[user_id].add(ws)
        if first:
            await self.publish({"type": "presence", "user_id": user_id, "online": True})

    async def disconnect(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets[user_id].discard(ws)
            last = not self._sockets[user_id]
        if last:
            await self.publish({"type": "presence", "user_id": user_id, "online": False})

    async def publish(self, event: dict[str, Any], *, users: set[int] | None = None) -> None:
        if "cursor" not in event:
            try:
                with db() as session:
                    event = {**event, "cursor": advance_cursor(session)}
                    session.commit()
            except Exception:
                logger.exception("Could not stamp an event cursor")
        channel_id = _channel_id(event)
        owner = self.audience(channel_id) if channel_id is not None else None
        audience = ({owner} if isinstance(owner, int) else owner) if users is None else users
        agent_id = (event.get("agent") or {}).get("id") or (event.get("agent_id") if channel_id is None else None)
        async with self._lock:
            targets = [ws for user_id, sockets in self._sockets.items()
                       if (audience is None or user_id in audience)
                       and (not agent_id or self.agent_visible(user_id, agent_id)) for ws in sockets]
        results = await asyncio.gather(
            *(ws.send_json(event) for ws in targets), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                logger.debug("Dropped event for a closed socket: %s", result)


bus = EventBus()
