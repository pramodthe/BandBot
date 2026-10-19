"""Reply drafts: the parent agent's answer as it is written, shown in the channel until the final message lands."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

from langchain.agents.middleware.internal_call_transformer import INTERNAL_CALL_METADATA_KEY
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError

from app.persistence.models import AgentTask, MessageDraft, db, utcnow

logger = logging.getLogger(__name__)

DRAFT_INTERVAL = 0.3
LOCKED_PAUSE = 2.0
MAX_DRAFT = 60000


NOT_TOOLS = (None, "text", "reasoning")

# Models sometimes emit hidden citation markup (U+E200 kind U+E202 ref ... U+E201) that browsers render as junk.
MARKER = re.compile("(\\s?)\ue200([^\ue200\ue201]*)\ue201")
UNFINISHED_MARKER = re.compile("\ue200[^\ue201]*$")
PRIVATE_USE = re.compile("[\ue200-\ue2ff]")


def _marker_links(match: re.Match) -> str:
    refs = [ref.strip() for ref in match.group(2).split("\ue202")[1:]]
    links = [f"[{urlparse(ref).hostname}]({ref})" for ref in refs
             if ref.startswith(("https://", "http://")) and urlparse(ref).hostname]
    return f" ({', '.join(links)})" if links else ""


def clean_markers(text: str) -> str:
    return PRIVATE_USE.sub("", UNFINISHED_MARKER.sub("", MARKER.sub(_marker_links, text)))


def _after_tools(content) -> str | None:
    """Text after the last server-side tool block (e.g. code execution), or None when there is none."""
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    tools = [i for i, b in enumerate(blocks) if b.get("type") not in NOT_TOOLS]
    if not tools:
        return None
    return "".join(b.get("text", "") for b in blocks[tools[-1] + 1:] if b.get("type") == "text")


def answer_text(message) -> str:
    """The reply without text the model wrote before running server-side tools."""
    after = _after_tools(getattr(message, "content", ""))
    return clean_markers(after if after and after.strip() else message.text)


def save_draft(task: AgentTask, content: str) -> bool:
    """Best effort: drafts are cosmetic, so a busy database must not fail the task."""
    try:
        with db() as session:
            if content.strip():
                session.merge(MessageDraft(task_id=task.id, channel_id=task.channel_id, agent_id=task.agent_id,
                                           content=content[:MAX_DRAFT], updated_at=utcnow()))
            else:
                session.exec(delete(MessageDraft).where(MessageDraft.task_id == task.id))
            session.commit()
    except OperationalError as exc:
        logger.warning("Skipped a draft update for task %s (%s)", task.id, type(exc.orig).__name__)
        return False
    return True


def clear_draft(task_id: str | None = None) -> None:
    """Remove one task's draft, or every draft when a worker starts (none can still be in progress)."""
    with db() as session:
        query = delete(MessageDraft)
        session.exec(query if task_id is None else query.where(MessageDraft.task_id == task_id))
        session.commit()


class Draft:
    """Accumulates the root agent's model tokens; tool activity resets it so only the final answer remains."""

    def __init__(self, task: AgentTask, enabled: bool):
        self.task = task
        self.enabled = enabled
        self.text = ""
        self.saved = ""
        self.saved_at = 0.0
        self.paused_until = 0.0

    def feed(self, namespace: tuple, chunk, metadata: dict) -> None:
        if not self.enabled or namespace:
            return
        # Middleware (e.g. context summarization) calls the model inside the root model node; never show that.
        if metadata.get("lc_source") == "summarization" or INTERNAL_CALL_METADATA_KEY in metadata:
            return
        kind = getattr(chunk, "type", "")
        if kind == "tool":
            self.reset()
        elif kind in ("ai", "AIMessageChunk") and metadata.get("langgraph_node") == "model":
            if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
                self.reset()
            elif (after := _after_tools(chunk.content)) is not None:
                self.text = after
                self.flush(force=True)
            else:
                self.text += chunk.text
                self.flush()

    def reset(self) -> None:
        self.text = ""
        self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not self.enabled or self.text == self.saved or now < self.paused_until:
            return
        if not force and now - self.saved_at < DRAFT_INTERVAL:
            return
        if not save_draft(self.task, clean_markers(self.text)):
            # Each locked write can block the event loop for the busy timeout, so back off before retrying.
            self.paused_until = now + LOCKED_PAUSE
            return
        self.saved = self.text
        self.saved_at = time.monotonic()
