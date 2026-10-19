"""Keyed memory scoped to one agent and one channel."""
from __future__ import annotations

import re

from langchain_core.tools import tool
from sqlmodel import select

from app.persistence.models import AgentMemory, AgentTask, db, utcnow


def memory_tools(task: AgentTask):
    @tool
    def recall_memory(query: str = "") -> list[dict]:
        """Find this agent's saved knowledge in this channel. Empty query lists memories."""
        with db() as session:
            rows = session.exec(select(AgentMemory).where(
                AgentMemory.agent_id == task.agent_id, AgentMemory.channel_id == task.channel_id)).all()
        return [{"key": m.key, "content": m.content, "source_task_id": m.source_task_id,
                 "updated_at": m.updated_at.isoformat()} for m in rows
                if query.lower() in (m.key + " " + m.content).lower()][:30]

    @tool
    def remember(key: str, content: str) -> str:
        """Save a reusable fact in this agent/channel. Include source and uncertainty; never save secrets."""
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", key) or not content.strip() or len(content) > 8000:
            return "Use a key of 1–100 letters, digits, dots, underscores or hyphens and nonempty content up to 8000 characters."
        with db() as session:
            row = session.get(AgentMemory, (task.agent_id, task.channel_id, key))
            if row is None:
                row = AgentMemory(agent_id=task.agent_id, channel_id=task.channel_id,
                                  key=key, content=content, source_task_id=task.id)
            row.content, row.source_task_id, row.updated_at = content, task.id, utcnow()
            session.add(row)
            session.commit()
        return "Saved"

    return [recall_memory, remember]
