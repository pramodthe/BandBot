"""Private-computer authority is independent of shared channel membership."""

from fastapi import HTTPException

from app.persistence.models import AgentProfile, TaskPrincipal, TaskComputerBinding, UserDesktop, User
from app.workspace.access import agent_owner


def computer_owner_id(session, task):
    binding = session.get(TaskComputerBinding, task.id)
    desktop = session.get(UserDesktop, binding.computer_key) if binding else None
    if desktop:
        return desktop.user_id
    profile = session.get(AgentProfile, task.agent_id)
    return agent_owner(session, profile) if profile else None


def require_computer_owner(session, task, user_id):
    """Private user desktops stay with their owner. Shared workspace and per-channel computers follow channel policy."""
    binding = session.get(TaskComputerBinding, task.id)
    key = binding.computer_key if binding else ""
    from app.computer.identity import is_user_desktop, user_identity
    if not is_user_desktop(key):
        return
    owner = computer_owner_id(session, task)
    if owner is not None and owner != user_id:
        raise HTTPException(403, "Only this agent's owner can access its private computer")
    user = session.get(User, user_id)
    if owner is None or not user or not key.startswith("tenki-desktop-" + user_identity(user) + "-"):
        raise HTTPException(403, "Private computer ownership is unavailable")


def task_may_use_private_computer(session, task, user_id):
    principal = session.get(TaskPrincipal, task.id)
    return bool(principal and principal.user_id == user_id)


