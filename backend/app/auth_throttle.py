"""Limits repeated failed sign-in and signup attempts for one API process."""
from __future__ import annotations

import time

from app.config import settings


class AttemptLimiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        hits = [item for item in self._hits.get(key, []) if now - item < self.window_seconds]
        self._hits[key] = hits
        return hits

    def too_many(self, key: str, now: float | None = None) -> bool:
        return len(self._prune(key, time.monotonic() if now is None else now)) >= self.limit

    def record_failure(self, key: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        hits = self._prune(key, moment)
        hits.append(moment)
        self._hits[key] = hits


login_limiter = AttemptLimiter(settings.auth_attempt_limit, settings.auth_attempt_window)
signup_limiter = AttemptLimiter(settings.auth_attempt_limit, settings.auth_attempt_window)
GENERIC_LOCKOUT = "Too many attempts. Try again later."
