"""Read immutable revisions; revoked credentials cannot be used by saved work."""
import json
from app.auth import decrypt_secret
from app.persistence.models import db
from app.providers.contracts import ProviderError, ResolvedConnection
from app.providers.models import ProviderConnection, ProviderRevision

def resolve(binding: dict, service: str, *, session=None) -> ResolvedConnection:
    if not binding or binding.get("disabled"):
        raise ProviderError(service, "not_configured")
    def read(current):
        row = current.get(ProviderConnection, binding.get("connection_id"))
        revision = current.get(ProviderRevision, (binding.get("connection_id"), binding.get("revision")))
        if not row or not revision or row.status == "revoked":
            raise ProviderError(service, "revoked" if row and row.status == "revoked" else "missing", connection_id=binding.get("connection_id", ""))
        if row.provider != binding.get("provider"):
            raise ProviderError(service, "identity_mismatch", connection_id=row.id)
        try:
            secret = json.loads(decrypt_secret(revision.secret_enc)) if revision.secret_enc else {}
        except Exception:
            raise ProviderError(service, "credentials_unreadable", connection_id=row.id) from None
        return ResolvedConnection(row.id, revision.revision, row.provider, dict(revision.config), secret, dict(revision.verification))
    if session is not None:
        return read(session)
    with db() as current:
        return read(current)

def has_llm(agent_id=None):
    from app.providers.bindings import selected
    with db() as session:
        binding = selected(session, agent_id).get("llm", {})
        if not binding or binding.get("disabled") or not binding.get("connection_id"):
            return False
        row = session.get(ProviderConnection, binding.get("connection_id"))
        return bool(row and row.status == "active")

def public_connection(row, revisions):
    return {"id": row.id, "provider": row.provider, "label": row.label, "status": row.status,
            "current_revision": row.current_revision, "revisions": [
                {"revision": r.revision, "config": r.config, "verification": r.verification,
                 "has_secret": bool(r.secret_enc), "origin": r.origin} for r in revisions]}
