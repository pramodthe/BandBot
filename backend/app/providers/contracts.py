"""Shared service contracts and safe errors independent of any SDK."""
from dataclasses import dataclass, field
from typing import Any

# Provider HTTP statuses that prove a request was refused, so it is safe to try again.
CONFIRMED_REJECTIONS = frozenset({400, 401, 403, 404, 422, 429})

def failure_code(status, *, authentication_error: bool = False) -> str:
    """Map a provider HTTP status, or an SDK authentication error, to a public recovery code."""
    if status in (401, 403) or authentication_error:
        return "authentication"
    return "quota_or_rate_limit" if status == 429 else "unavailable"

class ProviderError(RuntimeError):
    def __init__(self, service: str, code: str = "unavailable", *, connection_id: str = "", detail: str = ""):
        self.service, self.code, self.connection_id = service, code, connection_id
        self.detail = detail or "This service needs a working connection. Ask an administrator to reconnect it, then retry."
        super().__init__(self.detail)

@dataclass(frozen=True)
class ResolvedConnection:
    id: str
    revision: int
    provider: str
    config: dict[str, Any]
    secret: dict[str, Any] = field(repr=False)
    verification: dict[str, Any] = field(default_factory=dict)
