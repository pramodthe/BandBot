"""Offline provider configuration, immutable snapshots, and endpoint isolation."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import asyncio
import pytest
import httpx
from sqlmodel import select

from app.auth import decrypt_secret
from app.persistence.models import AgentProfile, AgentTask, Channel, TaskModel, db, init_db
from app.providers.models import ProviderConnection, ProviderRevision, ServiceSelection, TaskServiceBinding
from app.providers.management import (create_connection, rotate, apply_selection, ConfigurationConflict)
from app.providers.connections import resolve, public_connection
from app.providers.bootstrap import bootstrap
from app.providers.endpoint import GuardedTransport, validate_base

def connection(session, provider="openai", model="test-model"):
    row = create_connection(session, provider=provider, label="Offline " + uuid4().hex,
        key="offline-secret", config={"models": [model]}, user_id=1)
    revision = session.get(ProviderRevision, (row.id, 1))
    revision.verification = {"passed": True, "models": {model: {"tools": True, "vision": True, "usage": True}}}
    session.add(revision)
    session.flush()
    return {"connection_id": row.id, "revision": 1, "provider": provider, "model": model}

@pytest.fixture
def provider_scope():
    init_db()
    with db() as session:
        settings = session.get(ServiceSelection, "workspace")
        original = deepcopy(settings.bindings)
        agent = AgentProfile(name="Provider test", band_agent_id=uuid4().hex, band_api_key_enc="offline", model="test-model")
        channel = Channel(name=uuid4().hex)
        session.add_all([agent, channel]); session.commit()
    yield agent, channel
    with db() as session:
        settings = session.get(ServiceSelection, "workspace")
        settings.bindings = original; session.add(settings)
        from app.persistence.models import Document
        from app import documents
        for document in session.exec(select(Document).where(Document.channel_id == channel.id)).all():
            document.status = "deleted"
            document.units = []
            documents.path_for(document).unlink(missing_ok=True)
            session.add(document)
        session.commit()

def job(session, scope):
    agent, channel = scope
    task = AgentTask(id=uuid4().hex, source_key=uuid4().hex, agent_id=agent.id,
                     channel_id=channel.id, room_id="offline", prompt="Test immutable configuration")
    session.add(task); session.flush()
    return task

def test_tasks_snapshot_connections_models_and_auxiliary_services_atomically(provider_scope):
    with db() as session:
        first = connection(session)
        selection = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=selection.version,
                        bindings={"llm": first, "document_analysis": first})
        task = job(session, provider_scope); session.commit()
        binding = deepcopy(session.get(TaskServiceBinding, task.id).bindings)
        assert binding["llm"] == first
        second = connection(session, "anthropic", "claude-offline")
        session.refresh(selection)
        apply_selection(session, scope="workspace", expected_version=selection.version, bindings={"llm": second})
        later = job(session, provider_scope); session.commit()
        assert session.get(TaskServiceBinding, task.id).bindings == binding
        assert session.get(TaskServiceBinding, later.id).bindings["llm"] == second
        assert session.get(TaskModel, later.id).provider == "anthropic"

def test_rollback_does_not_leave_snapshots_or_scope_selections(provider_scope):
    with db() as session:
        task = job(session, provider_scope)
        assert session.get(TaskServiceBinding, task.id)
        session.rollback()
    with db() as session:
        assert session.get(TaskServiceBinding, task.id) is None

def test_rotation_retains_original_encrypted_revision_and_has_no_public_secret(provider_scope):
    with db() as session:
        binding = connection(session)
        row = session.get(ProviderConnection, binding["connection_id"])
        revision = rotate(session, row, expected_revision=1, key="replacement-secret", config=None)
        session.commit()
        assert resolve(binding, "llm", session=session).secret["api_key"] == "offline-secret"
        assert resolve({**binding, "revision": revision}, "llm", session=session).secret["api_key"] == "replacement-secret"
        revisions = session.exec(select(ProviderRevision).where(ProviderRevision.connection_id == row.id)).all()
        public = str(public_connection(row, revisions))
        assert "offline-secret" not in public and "replacement-secret" not in public and "secret_enc" not in public
        with pytest.raises(ConfigurationConflict):
            rotate(session, row, expected_revision=1, key="stale-secret", config=None)

def test_selection_requires_verified_models_and_rejects_stale_revision(provider_scope):
    with db() as session:
        binding = connection(session)
        selection = session.get(ServiceSelection, "workspace")
        old = selection.version
        apply_selection(session, scope="workspace", expected_version=old, bindings={"llm": binding})
        session.commit()
        with pytest.raises(ConfigurationConflict):
            apply_selection(session, scope="workspace", expected_version=old, bindings={"llm": binding})
        session.rollback()
        selection = session.get(ServiceSelection, "workspace")
        with pytest.raises(ValueError, match="verified"):
            apply_selection(session, scope="workspace", expected_version=selection.version,
                            bindings={"llm": {**binding, "model": "unverified-model"}})

def test_computer_scopes_without_a_channel_are_skipped(provider_scope):
    from app.persistence.models import ComputerScope
    from app.providers.bootstrap import remember_computer_scopes
    binding = {"connection_id": "imported-local", "revision": 1, "provider": "local"}
    channel = uuid4().hex
    with db() as session:
        session.add(ComputerScope(key=f"agent-orphan-{channel}", provider="local"))
        session.add(ComputerScope(key=f"agent-band-{channel}-channel-{channel}", provider="local"))
        remember_computer_scopes(session, {"local": binding})
        saved = session.get(ServiceSelection, f"computer:band-{channel}:{channel}")
        assert saved is not None and saved.bindings["computer"] == binding
        assert session.get(ServiceSelection, f"computer:orphan-{channel}") is None
        session.rollback()


def test_bootstrap_is_idempotent_and_does_not_reapply_environment(provider_scope):
    with db() as session:
        row = session.get(ServiceSelection, "workspace")
        before = (row.version, deepcopy(row.bindings))
    bootstrap(); bootstrap()
    with db() as session:
        row = session.get(ServiceSelection, "workspace")
        assert (row.version, row.bindings) == before

@pytest.mark.parametrize("url", ["http://127.0.0.1:11434/v1", "https://169.254.169.254/", "https://user:password@example.com/v1", "https://example.com/v1?key=secret"])
def test_unapproved_endpoint_is_rejected(url, monkeypatch):
    import app.providers.endpoint as endpoint
    monkeypatch.setattr(endpoint, "settings", SimpleNamespace(provider_endpoint_allowlist=""))
    with pytest.raises(ValueError): validate_base(url)

def test_endpoint_dns_is_checked_and_pinned_before_secrets_are_sent(monkeypatch):
    import app.providers.endpoint as endpoint
    monkeypatch.setattr(endpoint, "settings", SimpleNamespace(provider_endpoint_allowlist=""))
    sent = []
    async def scenario():
        loop = asyncio.get_running_loop()
        async def records(*a, **kw): return [(2, 1, 6, "", ("93.184.215.14", 443))]
        monkeypatch.setattr(loop, "getaddrinfo", records)
        transport = GuardedTransport("https://models.example/v1", httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post("https://models.example/v1/chat/completions", headers={"Authorization": "Bearer offline"})
        assert sent[0].url.host == "93.184.215.14"
        assert sent[0].headers["Host"] == "models.example"
        assert sent[0].extensions["sni_hostname"] == "models.example"
        async def rebound(*a, **kw): return [(2, 1, 6, "", ("127.0.0.1", 443))]
        monkeypatch.setattr(loop, "getaddrinfo", rebound)
        async with httpx.AsyncClient(transport=GuardedTransport("https://models.example/v1", httpx.MockTransport(lambda r: pytest.fail("Secret reached private address")))) as client:
            with pytest.raises(ValueError, match="disallowed"):
                await client.post("https://models.example/v1/chat/completions")
    asyncio.run(scenario())

def test_unset_services_cannot_fall_back_to_new_environment_credentials(provider_scope, monkeypatch):
    from dataclasses import replace
    from app.providers import bootstrap as imports
    with db() as session:
        current = session.get(ServiceSelection, "workspace")
        current.bindings = {**current.bindings, "llm": {"disabled": True}, "computer": {"disabled": True}, "research": {"disabled": True}}
        session.add(current); session.commit()
    monkeypatch.setattr(imports, "settings", replace(imports.settings, openai_api_key="later-unverified-key", e2b_api_key="later-computer-key"))
    imports.bootstrap()
    with db() as session:
        task = job(session, provider_scope); session.commit()
        assert session.get(TaskServiceBinding, task.id).bindings["llm"] == {"disabled": True}
        assert session.get(TaskServiceBinding, task.id).bindings["computer"] == {"disabled": True}
        assert not session.get(ProviderConnection, "imported-openai")

def test_cleared_computer_override_keeps_monotonic_revision_and_rejects_stale_admin(provider_scope):
    with db() as session:
        row = create_connection(session, provider="e2b", label="Override computer", key="offline", config={}, user_id=1)
        revision = session.get(ProviderRevision, (row.id, 1))
        revision.verification = {"passed": True, "pause_resume": True}; session.add(revision)
        binding = {"connection_id": row.id, "revision": 1, "provider": "e2b"}
        scope = f"computer:{provider_scope[0].band_agent_id}:{provider_scope[1].id}"
        apply_selection(session, scope=scope, expected_version=0, bindings={"computer": binding})
        workspace = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=workspace.version,
            bindings={"computer": binding}, clear_computer_overrides=True)
        session.commit()
        cleared = session.get(ServiceSelection, scope)
        assert cleared.version == 2 and cleared.bindings == {}
        apply_selection(session, scope=scope, expected_version=2, bindings={"computer": binding})
        session.commit()
        with pytest.raises(ConfigurationConflict):
            apply_selection(session, scope=scope, expected_version=1, bindings={"computer": {"disabled": True}})


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
def test_recoverable_checkpoints_retain_connections_after_defaults_change(provider_scope, status):
    from app.providers.management import dependencies
    with db() as session:
        binding = connection(session)
        workspace = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=workspace.version, bindings={"llm": binding})
        task = job(session, provider_scope)
        task.status = status
        session.add(task)
        session.flush()
        session.refresh(workspace)
        apply_selection(session, scope="workspace", expected_version=workspace.version, bindings={"llm": {"disabled": True}})
        session.commit()
        usage = dependencies(session, binding["connection_id"])
        assert usage["selections"] == []
        assert usage["unfinished_tasks"] == int(status != "completed")


def test_allowlist_cannot_enable_mapped_metadata_addresses(monkeypatch):
    import app.providers.endpoint as endpoint
    url = "https://[::ffff:169.254.169.254]/v1"
    monkeypatch.setattr(endpoint, "settings", SimpleNamespace(provider_endpoint_allowlist=url))
    with pytest.raises(ValueError, match="Infrastructure"):
        validate_base(url)

def test_allowlisted_dns_cannot_resolve_to_mapped_metadata(monkeypatch):
    import app.providers.endpoint as endpoint
    monkeypatch.setattr(endpoint, "settings", SimpleNamespace(provider_endpoint_allowlist="https://models.example"))
    async def scenario():
        async def records(*args, **kwargs): return [(10, 1, 6, "", ("::ffff:169.254.169.254", 443, 0, 0))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", records)
        async with httpx.AsyncClient(transport=GuardedTransport("https://models.example/v1", httpx.MockTransport(lambda request: pytest.fail("Secret reached metadata")))) as client:
            with pytest.raises(ValueError, match="disallowed"):
                await client.post("https://models.example/v1/chat/completions")
    asyncio.run(scenario())
