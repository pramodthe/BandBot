"""OpenAI hosted tools. Automatic Python containers are scoped to each task's model history."""
from __future__ import annotations

from app.config import settings

CODE_EXECUTION = {"type": "code_interpreter", "container": {"type": "auto"}}


def hosted_tools() -> list[dict]:
    return [CODE_EXECUTION] if settings.fast_code_execution else []
