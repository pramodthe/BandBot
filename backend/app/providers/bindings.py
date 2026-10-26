"""Snapshot all services within the task/document creation transaction."""
from copy import deepcopy
from app.persistence.models import AgentProfile, AgentRuntime, AgentConnection, TaskModel, db
from app.providers.models import ServiceSelection, TaskServiceBinding, DocumentServiceBinding

def workspace_bindings(session) -> dict:
    row = session.get(ServiceSelection, "workspace")
    return deepcopy(row.bindings) if row else {}

def selected(session, agent_id: int | None = None, channel_id: int | None = None) -> dict:
    bindings = workspace_bindings(session)
    if agent_id is not None:
        override = session.get(ServiceSelection, f"agent:{agent_id}")
        if override:
            bindings.update(deepcopy(override.bindings))
        profile = session.get(AgentProfile, agent_id)
        if profile and channel_id is not None:
            override = session.get(ServiceSelection, f"computer:{profile.band_agent_id}:{channel_id}")
            if override:
                bindings.update(deepcopy(override.bindings))
    return bindings

def task_binding(session, task_id: str):
    return next((r for r in session.new if isinstance(r, TaskServiceBinding) and r.task_id == task_id), None) or session.get(TaskServiceBinding, task_id)

def snapshot_task(session, task, *, legacy=False):
    existing = task_binding(session, task.id)
    if existing:
        return existing
    bindings = selected(session, task.agent_id, task.channel_id)
    profile = session.get(AgentProfile, task.agent_id)
    runtime = session.get(AgentRuntime, task.agent_id)
    bindings["_agent"] = {"band_id": profile.band_agent_id if profile else ""}
    connected_secret = ""
    pinned = session.get(TaskModel, task.id) if legacy else None
    if legacy and task.attempts > 0 and pinned is None:
        bindings["llm"] = {"unresolved": True, "provider": "unknown", "model": profile.model if profile else ""}
    elif runtime and (not pinned or pinned.provider == runtime.provider):
        connection = session.get(AgentConnection, runtime.connection_id)
        bindings["llm"] = {"connected": True, "connection_id": runtime.connection_id,
                           "provider": runtime.provider, "model": pinned.model if pinned else profile.model}
        connected_secret = connection.secret_enc if connection else ""
    elif pinned and pinned.provider != "openai":
        bindings["llm"] = {"unresolved": True, "provider": pinned.provider, "model": pinned.model}
    elif not bindings.get("llm"):
        bindings["llm"] = {"legacy": True, "provider": "openai", "model": pinned.model if pinned else ""}
    else:
        llm = bindings["llm"]
        if pinned:
            llm["model"] = pinned.model
        elif profile and llm.get("connection_id") and not (session.get(ServiceSelection, f"agent:{task.agent_id}") or ServiceSelection(scope="")).bindings.get("llm"):
            from app.providers.models import ProviderRevision
            revision = session.get(ProviderRevision, (llm.get("connection_id"), llm.get("revision")))
            allowed = (revision.config.get("models") or []) if revision else []
            if revision and revision.origin == "imported" and llm.get("provider") == "openai" and profile.model in allowed:
                llm["model"] = profile.model
    row = TaskServiceBinding(task_id=task.id, bindings=bindings, connected_secret_enc=connected_secret)
    session.add(row)
    return row

def bindings_for(task) -> dict:
    with db() as session:
        row = session.get(TaskServiceBinding, task.id)
        if row and row.runtime_version != 1:
            from app.providers.contracts import ProviderError
            raise ProviderError("llm", "runtime_contract", detail="This saved runtime contract requires explicit recovery. Its checkpoint is preserved.")
        return deepcopy(row.bindings) if row else {}

def document_bindings(document_id: str) -> dict:
    with db() as session:
        row = session.get(DocumentServiceBinding, document_id)
        return deepcopy(row.bindings) if row else workspace_bindings(session)
