"""Shared service contracts and safe errors independent of any SDK."""
from dataclasses import dataclass, field
from typing import Any, Protocol

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

class ModelAdapter(Protocol):
    def build(self, connection: ResolvedConnection, model: str): ...
    async def verify(self, connection: ResolvedConnection, models: list[str]) -> dict: ...

class ResearchAdapter(Protocol):
    async def search(self, connection: ResolvedConnection, query: str) -> dict: ...
    async def fetch(self, connection: ResolvedConnection, url: str) -> dict: ...

class ComputerAdapter(Protocol):
    async def get(self, reference: str, *, request_timeout: int = 30): ...
    async def create(self, options, *, timeout: int = 60): ...
    async def close(self): ...

class RetrievalAdapter(Protocol):
    async def search(self, task, query: str) -> dict: ...
    async def index(self, document, binding: dict) -> None: ...

class ImageAdapter(Protocol):
    async def generate(self, connection: ResolvedConnection, model: str, prompt: str, **options) -> tuple[bytes, dict]: ...
