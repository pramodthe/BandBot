"""Stable ownership for isolated computers and the opt-in workspace computer."""
import hashlib

from app.config import settings


def workspace_id(config=settings) -> str:
    return hashlib.sha256(config.app_secret.encode()).hexdigest()[:16]


def shared_key(config=settings) -> str:
    return f"tenki-workspace-{workspace_id(config)}" + namespace(config)


def namespace(config=settings):
    value = getattr(config, "computer_namespace", "")
    return f"-account-{value}" if value else ""

def isolated_key(owner, channel_id, provider, config=settings):
    return f"{provider}-agent-{owner}-channel-{channel_id}" + namespace(config)

def is_shared(key: str) -> bool:
    return key.startswith("tenki-workspace-")


def is_user_desktop(key: str) -> bool:
    return key.startswith("tenki-desktop-")


def user_identity(user) -> str:
    return hashlib.sha256(f"{user.id}:{user.created_at.isoformat()}".encode()).hexdigest()[:24]


def user_keys(session, agent_identity: str, config=settings) -> tuple[str, str, int] | None:
    from sqlmodel import select
    from app.persistence.models import AgentProfile, BandAgentOwner, User
    profile = session.exec(select(AgentProfile).where(AgentProfile.band_agent_id == agent_identity)).first()
    owner = session.get(BandAgentOwner, profile.id) if profile else None
    user = session.get(User, owner.user_id) if owner else None
    if not user:
        return None
    host = f"tenki-user-{workspace_id(config)}-{user_identity(user)}" + namespace(config)
    desktop = f"tenki-desktop-{user_identity(user)}-{hashlib.sha256(agent_identity.encode()).hexdigest()[:24]}" + namespace(config)
    return host, desktop, user.id


def selected_key(session, owner: str, channel_id: int, provider: str, config=settings) -> str:
    from app.persistence.models import DirectMessage, PrivateConversation
    if provider == "tenki" and config.tenki_computer_sharing == "user":
        keys = user_keys(session, owner, config)
        if keys:
            return keys[1]
        # Unowned legacy agents cannot acquire a user's private host.
        return isolated_key(owner, channel_id, provider, config)
    # before_flush also binds tasks created alongside their private-chat marker.
    # Pending rows are not in Session.get's identity map until they are flushed.
    private = any(isinstance(row, (DirectMessage, PrivateConversation)) and row.channel_id == channel_id
                  for row in session.new)
    if (provider == "tenki" and config.tenki_computer_sharing == "workspace"
            and not private
            and session.get(DirectMessage, channel_id) is None
            and session.get(PrivateConversation, channel_id) is None):
        return shared_key(config)
    return isolated_key(owner, channel_id, provider, config)


def key_from_labels(labels: dict) -> str:
    if labels.get("scope") == "user":
        return labels["host_key"]
    if labels.get("scope") == "workspace":
        return f"tenki-workspace-{labels['workspace']}" + ("-account-" + labels["account_namespace"] if labels.get("account_namespace") else "")
    return f"tenki-agent-{labels.get('agent')}-channel-{labels.get('channel')}" + ("-account-" + labels["account_namespace"] if labels.get("account_namespace") else "")
