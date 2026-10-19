"""Workspace roles (owner, admin, member) and per-channel policies for who may act for agents."""
from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.persistence.models import ChannelPolicy, User, WorkspaceRole, utcnow

ADMIN_ROLES = ("owner", "admin")
POLICY_VALUES = ("members", "admins")


def ensure_owner(session: Session) -> None:
    """Make the oldest account the owner when no current account is (a fresh or pre-roles workspace)."""
    owner = session.exec(select(WorkspaceRole).join(User, User.id == WorkspaceRole.user_id).where(
        WorkspaceRole.role == "owner")).first()
    if owner:
        return
    oldest = session.exec(select(User).order_by(User.created_at, User.id)).first()
    if oldest:
        session.merge(WorkspaceRole(user_id=oldest.id, role="owner", updated_at=utcnow()))
        session.commit()


def role_of(session: Session, user_id: int) -> str:
    row = session.get(WorkspaceRole, user_id)
    return row.role if row else "member"


def is_admin(session: Session, user_id: int) -> bool:
    return role_of(session, user_id) in ADMIN_ROLES


def workspace_roles(session: Session) -> dict[int, str]:
    """Roles of people above member; everyone else is a member."""
    return {row.user_id: row.role for row in session.exec(select(WorkspaceRole)).all()}


def policy_of(session: Session, channel_id: int) -> ChannelPolicy:
    return session.get(ChannelPolicy, channel_id) or ChannelPolicy(channel_id=channel_id)


def serialize_policy(policy: ChannelPolicy) -> dict[str, Any]:
    return {"approvers": policy.approvers, "computer_control": policy.computer_control}


def allowed(session: Session, user_id: int, channel_id: int, setting: str) -> bool:
    """Whether the person passes a channel policy setting ("approvers" or "computer_control")."""
    return getattr(policy_of(session, channel_id), setting) == "members" or is_admin(session, user_id)
