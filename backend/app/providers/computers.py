"""Computer adapters resolve the original account and configuration revision."""
from dataclasses import asdict
from types import SimpleNamespace
from app.config import settings
from app.providers.connections import resolve
from app.providers.contracts import ProviderError
from app.providers.bindings import bindings_for, selected, snapshot_task

def config_from(binding, defaults=settings, *, safe=False):
    if not binding or binding.get("legacy"):
        return defaults
    try:
        connection = resolve(binding, "computer")
    except ProviderError:
        if not safe:
            raise
        from app.providers.models import ProviderRevision
        from app.persistence.models import db
        with db() as session:
            revision = session.get(ProviderRevision, (binding["connection_id"], binding["revision"])) if binding.get("connection_id") and binding.get("revision") else None
        connection = SimpleNamespace(provider=binding.get("provider"), secret={}, config=revision.config if revision else {})
    values = asdict(defaults)
    values["computer_namespace"] = connection.config.get("computer_namespace", "")
    values["computer_provider"] = connection.provider
    key = connection.secret.get("api_key", "")
    config = connection.config
    if connection.provider == "tenki":
        values.update(tenki_api_key=key, tenki_desktop_image=config.get("image", ""),
            tenki_computer_sharing=config.get("sharing", "workspace"), tenki_disk_gib=config.get("disk_gib", 20),
            tenki_timeout=config.get("timeout", 1200), tenki_pause_retention=config.get("pause_retention", 604800))
    elif connection.provider == "e2b":
        values.update(e2b_api_key=key, e2b_template=config.get("template", "desktop"), e2b_timeout=config.get("timeout", 1200))
    return SimpleNamespace(**values)

def config_for_task(task, defaults=settings, *, safe=False):
    return config_from(bindings_for(task).get("computer", {}), defaults, safe=safe)

def scope_config(session, profile, channel_id, defaults=settings, *, safe=False):
    return config_from(selected(session, profile.id, channel_id).get("computer", {}), defaults, safe=safe)

def identity_config(session, task, defaults):
    # Called before flush: read only non-secret revision fields. Revoked credentials
    # block execution, never acceptance of the human's message.
    from app.providers.models import ProviderRevision
    binding = snapshot_task(session, task).bindings.get("computer", {})
    if not binding or binding.get("legacy") or binding.get("disabled"):
        return None
    revision = session.get(ProviderRevision, (binding.get("connection_id"), binding.get("revision")))
    if not revision:
        return None
    values = asdict(defaults)
    values.update(computer_namespace=revision.config.get("computer_namespace", ""),
                  tenki_computer_sharing=revision.config.get("sharing", "workspace"))
    return binding["provider"], SimpleNamespace(**values)

def client(provider, config):
    if provider == "tenki":
        from app.computer.tenki import TenkiDesktopClient
        return TenkiDesktopClient(config=config)
    if provider == "e2b":
        from app.computer.e2b import E2BDesktopClient
        return E2BDesktopClient(config=config)
    from app.providers.contracts import ProviderError
    raise ProviderError("computer", "unsupported_provider")
