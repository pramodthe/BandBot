"""Distinguish a task using its computer from an agent doing other work."""
from sqlmodel import Session, select

from app.persistence.models import AuditEvent


COMPUTER_TOOLS = (
    "run_python", "sandbox_exec", "computer_screenshot", "computer_open_browser",
    "computer_action", "sandbox_write_file", "sandbox_export_file", "publish_app",
)


def used_computer(session: Session, task_id: str) -> bool:
    # Includes helper calls, but excludes provider code_interpreter and browser-only UI.
    return session.exec(select(AuditEvent.id).where(
        AuditEvent.task_id == task_id, AuditEvent.action == "tool_call",
        AuditEvent.detail["tool"].as_string().in_(COMPUTER_TOOLS),
    ).limit(1)).first() is not None
