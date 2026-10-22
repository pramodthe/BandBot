"""Stable ownership for isolated computers and the opt-in workspace computer."""
import hashlib

from app.config import settings


def workspace_id(config=settings) -> str:
    return hashlib.sha256(config.app_secret.encode()).hexdigest()[:16]


def shared_key(config=settings) -> str:
    return f"tenki-workspace-{workspace_id(config)}"


def is_shared(key: str) -> bool:
    return key.startswith("tenki-workspace-")


def selected_key(session, owner: str, channel_id: int, provider: str, config=settings) -> str:
    from app.persistence.models import DirectMessage, PrivateConversation
    # before_flush also binds tasks created alongside their private-chat marker.
    # Pending rows are not in Session.get's identity map until they are flushed.
    private = any(isinstance(row, (DirectMessage, PrivateConversation)) and row.channel_id == channel_id
                  for row in session.new)
    if (provider == "tenki" and config.tenki_computer_sharing == "workspace"
            and not private
            and session.get(DirectMessage, channel_id) is None
            and session.get(PrivateConversation, channel_id) is None):
        return shared_key(config)
    return f"{provider}-agent-{owner}-channel-{channel_id}"


def key_from_labels(labels: dict) -> str:
    if labels.get("scope") == "workspace":
        return f"tenki-workspace-{labels['workspace']}"
    return f"tenki-agent-{labels.get('agent')}-channel-{labels.get('channel')}"
