"""Only the latest custom answer for a task is visible; older saved IDs remain readable."""
from sqlalchemy import or_
from sqlalchemy.orm import aliased
from sqlmodel import select

from app.persistence.models import MessageComponent


def current_components():
    newer = aliased(MessageComponent)
    superseded = select(newer.id).where(
        newer.task_id == MessageComponent.task_id, newer.kind == "sandbox",
        newer.id > MessageComponent.id,
    ).exists()
    return or_(MessageComponent.kind != "sandbox", ~superseded)
