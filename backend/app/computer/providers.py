"""Task bindings do not change when a scope selects another provider."""
from app.config import settings
from app.persistence.models import AgentProfile, ComputerScope, TaskComputerBinding, db


TRANSFER_LIMIT = 25 * 1024 * 1024  # largest file copied to or from a computer


class ComputerProviderError(RuntimeError):
    """Provider details must never reach user-visible errors."""


class ComputerMissing(ComputerProviderError):
    pass


def provider_for(task) -> str:
    with db() as session:
        binding = session.get(TaskComputerBinding, task.id)
    return binding.provider if binding else "e2b"


def key_for(task) -> str:
    with db() as session:
        binding = session.get(TaskComputerBinding, task.id)
        if binding:
            return binding.computer_key
        profile = session.get(AgentProfile, task.agent_id)
    owner = profile.band_agent_id if profile else f"deleted-agent-{task.agent_id}"
    return f"e2b-agent-{owner}-channel-{task.channel_id}"


def scope_provider(session, profile, channel_id: int) -> str:
    from app.providers.bindings import selected
    binding = selected(session, profile.id, channel_id).get("computer", {})
    if binding and not binding.get("legacy"):
        return binding.get("provider", "disabled")
    scope = session.get(ComputerScope, f"agent-{profile.band_agent_id}-channel-{channel_id}")
    return scope.provider if scope else settings.computer_provider


def scope_computer_key(session, profile, channel_id: int, config=None) -> str:
    from app.computer.identity import selected_key
    from app.providers.computers import scope_config
    config = scope_config(session, profile, channel_id, config or settings, safe=True)
    return selected_key(session, profile.band_agent_id, channel_id, scope_provider(session, profile, channel_id), config)


def running_on(key: str, *, excluding: str | None = None) -> bool:
    from sqlmodel import select
    from app.persistence.models import AgentTask
    with db() as session:
        query = select(AgentTask.id).join(TaskComputerBinding, TaskComputerBinding.task_id == AgentTask.id).where(
            TaskComputerBinding.computer_key == key, AgentTask.status == "running")
        if excluding is not None:
            query = query.where(AgentTask.id != excluding)
        return session.exec(query).first() is not None


def configured(provider: str, config=settings) -> bool:
    return bool(config.e2b_api_key) if provider == "e2b" else bool(
        provider == "tenki" and config.tenki_api_key and config.tenki_desktop_image)


def dashboard(provider: str) -> str:
    return "https://app.tenki.cloud" if provider == "tenki" else "https://e2b.dev/dashboard"
