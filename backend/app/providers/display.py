from copy import deepcopy
from app.persistence.models import db
from app.providers.bindings import selected

def model_reference(agent_id=None):
    with db() as session:
        reference = deepcopy(selected(session, agent_id).get("llm"))
        if not reference or reference.get("disabled"):
            return None
        if agent_id and reference.get("provider") == "openai":
            from app.persistence.models import AgentProfile
            from app.providers.models import ProviderRevision, ServiceSelection
            revision = session.get(ProviderRevision, (reference.get("connection_id"), reference.get("revision")))
            override = session.get(ServiceSelection, f"agent:{agent_id}")
            profile = session.get(AgentProfile, agent_id)
            if revision and revision.origin == "imported" and not (override and override.bindings.get("llm")) and profile and profile.model in revision.config.get("models", []):
                reference["model"] = profile.model
        return reference

def configured():
    from app.providers.connections import resolve
    try:
        binding = model_reference()
        if not binding:
            return False
        resolve(binding, "llm")
        return True
    except Exception:
        return False

def set_agent_model(session, agent_id, reference, user):
    from fastapi import HTTPException
    from app.workspace.permissions import is_admin
    from app.providers.management import apply_selection
    from app.providers.models import ServiceSelection
    if not is_admin(session, user.id):
        raise HTTPException(403, "Only workspace admins can select provider connections")
    row = session.get(ServiceSelection, f"agent:{agent_id}")
    apply_selection(session, scope=f"agent:{agent_id}", expected_version=row.version if row else 0,
        bindings={"llm": reference} if reference else {})

def model_catalog():
    reference = model_reference()
    if not reference or not reference.get("connection_id"):
        from app.openai_provider import model_catalog as legacy
        return legacy()
    from app.providers.models import ProviderRevision
    with db() as session:
        revision = session.get(ProviderRevision, (reference["connection_id"], reference["revision"]))
    if not revision:
        return []
    models = revision.verification.get("models", {})
    return [{"id": name, "label": name, "default": name == reference.get("model"), "provider": reference["provider"]}
        for name, capabilities in models.items() if capabilities.get("tools") and capabilities.get("vision")]
