"""Regression tests for safe activation, cleanup, credential errors and recovery."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from sqlalchemy import text
from langchain_core.messages import HumanMessage
from app.persistence.models import (db, Document, AgentTask, TaskModel, AgentProfile)
from app.providers.models import (ProviderRevision, ProviderConnection, TaskServiceBinding, ProviderBlock,
    DocumentProviderIndex, ProviderDocumentCollection, ServiceChange, ServiceSelection)
from app.providers.management import apply_selection, binding_hash
from app.providers.contracts import ProviderError
from helpers import connection, make_task


def test_configuration_is_encrypted_at_rest_and_invalid_key_input_is_masked(admin_space):
    s = admin_space
    with db() as session:
        binding = connection(session, "anthropic", "claude-private-config")
        session.commit()
        raw = session.execute(text("SELECT config FROM providerrevision WHERE connection_id=:id"), {"id": binding["connection_id"]}).scalar_one()
        assert "claude-private-config" not in raw
        assert session.get(ProviderRevision, (binding["connection_id"], 1)).config["models"] == ["claude-private-config"]
    secret = "key-must-not-return-" * 300
    response = s.client.post("/api/settings/connections", headers=s.auth,
        json={"provider": "anthropic", "label": "Bad key", "api_key": secret, "config": {"models": ["test"]}})
    assert response.status_code == 422
    assert "key-must-not-return" not in response.text

def test_ambiguous_old_checkpoint_is_blocked_and_effective_claude_model_is_preserved(provider_scope):
    from app.providers.bootstrap import bootstrap
    from app.workspace.service import service
    agent, channel = provider_scope
    with db() as session:
        reference = connection(session, "anthropic", "claude-new-default")
        selection = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=selection.version, bindings={"llm": reference})
        task = make_task(session, provider_scope)
        session.commit()
        session.delete(session.get(TaskServiceBinding, task.id))
        session.delete(session.get(TaskModel, task.id))
        task.attempts = 2
        session.add(task)
        session.commit()
    bootstrap()
    with db() as session:
        saved = session.get(TaskServiceBinding, task.id)
        assert saved.bindings["llm"]["unresolved"]
        assert saved.bindings["llm"]["provider"] == "unknown"
    assert service.serialize_agent(agent)["model"] == "claude-new-default"

@pytest.mark.parametrize("metadata", [{"finish_reason": "MAX_TOKENS"}, {"stop_reason": "max_tokens"},
    {"finish_reason": "length"}, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}])
def test_native_truncation_is_not_reported_as_complete_scan(metadata):
    from app.providers.document_analysis import incomplete
    assert incomplete(NS(response_metadata=metadata))
    assert not incomplete(NS(response_metadata={"finish_reason": "STOP"}))

@pytest.mark.parametrize("status,code", [(401, "authentication"), (403, "authentication"),
    (429, "quota_or_rate_limit"), (503, "unavailable"), (None, "unavailable")])
def test_provider_failure_keeps_binding_and_exposes_safe_needs_connection(provider_scope, status, code):
    from app.tasks.worker import process
    with db() as session:
        reference = connection(session)
        selection = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=selection.version, bindings={"llm": reference})
        task = make_task(session, provider_scope)
        session.commit()
        before = deepcopy(session.get(TaskServiceBinding, task.id).bindings)
    error_type = type("SDKError", (Exception,), {"__module__": "anthropic", "status_code": status})
    async def fail(*args): raise error_type("Authorization: Bearer private-upstream-secret")
    asyncio.run(process(task, None, executor=fail))
    with db() as session:
        saved = session.get(AgentTask, task.id)
        assert saved.status == "failed" and saved.progress == "Needs connection"
        assert "private-upstream-secret" not in saved.error
        assert session.get(ProviderBlock, task.id).code == code
        assert session.get(TaskServiceBinding, task.id).bindings == before

def test_cancelled_model_dispatch_is_uncertain_and_cannot_be_repeated(provider_scope):
    from app.providers.model_dispatch import DurableModel
    with db() as session:
        reference = connection(session)
        task = make_task(session, provider_scope)
        session.commit()
    async def scenario():
        entered = asyncio.Event()
        async def wait(*a, **kw):
            entered.set()
            await asyncio.Event().wait()
        inner = NS(ainvoke=AsyncMock(side_effect=wait))
        model = DurableModel(inner=inner, binding=reference, task_id=task.id)
        call = asyncio.create_task(model.ainvoke([HumanMessage(content="Request")]))
        await entered.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        with pytest.raises(ProviderError, match="interrupted"):
            await model.ainvoke([HumanMessage(content="Request")])
        assert inner.ainvoke.await_count == 1
    asyncio.run(scenario())

def test_reindex_does_not_overwrite_an_administrator_change(provider_scope, monkeypatch):
    from app.providers import retrieval
    with db() as session:
        reference = connection(session)
        retrieval_binding = {k:v for k,v in reference.items() if k != "model"}
        selection = session.get(ServiceSelection, "workspace")
        before = deepcopy(selection.bindings)
        document = Document(id=uuid4().hex, channel_id=provider_scope[1].id, uploaded_by=1, name="index.txt", media_type="text/plain", size=4,
            status="ready", units=[{"location": "Line 1", "text": "Test"}])
        change = ServiceChange(id=uuid4().hex, expected_version=selection.version, bindings={"retrieval": retrieval_binding})
        session.add_all([document, change])
        session.commit()
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def build(*a):
            entered.set()
            await release.wait()
        monkeypatch.setattr(retrieval, "index_document", build)
        operation = asyncio.create_task(retrieval.process_change())
        await entered.wait()
        with db() as session:
            selection = session.get(ServiceSelection, "workspace")
            assert selection.bindings == before
            apply_selection(session, scope="workspace", expected_version=selection.version, bindings={"llm": reference})
            session.commit()
        release.set()
        await operation
    asyncio.run(scenario())
    with db() as session:
        assert session.get(ServiceChange, change.id).status == "failed"
        assert session.get(ServiceSelection, "workspace").bindings == {"llm": reference}

def test_document_cleanup_uses_each_original_account_after_switch(provider_scope, monkeypatch):
    from app import documents
    from app.providers import retrieval, openai_resources
    from app.providers.connections import resolve
    with db() as session:
        first = connection(session)
        second = connection(session)
        owners = [{k:v for k,v in b.items() if k != "model"} for b in (first, second)]
        document = Document(id=uuid4().hex, channel_id=provider_scope[1].id, uploaded_by=1, name="delete.txt", media_type="text/plain", size=4, status="deleting")
        session.add(document)
        for number, owner in enumerate(owners):
            digest = binding_hash(owner)
            session.add(DocumentProviderIndex(document_id=document.id, binding_hash=digest, binding=owner, status="ready", file_id=f"file-{number}", index_file_id=f"index-{number}", store_id=f"store-{number}"))
            session.add(ProviderDocumentCollection(channel_id=document.channel_id, binding_hash=digest, binding=owner, store_id=f"store-{number}", state="ready"))
        session.commit()
    actions = []
    @asynccontextmanager
    async def client():
        owner = resolve(openai_resources._binding.get(), "retrieval")
        async def delete_file(identity): actions.append((owner.id, "file", identity))
        async def delete_store(identity): actions.append((owner.id, "store", identity))
        yield NS(files=NS(delete=delete_file), vector_stores=NS(delete=delete_store))
    monkeypatch.setattr(documents, "client", client)
    path = documents.path_for(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Test")
    asyncio.run(retrieval.delete_resources(document))
    assert len(actions) == 6
    for number, owner in enumerate(owners):
        assert (owner["connection_id"], "file", f"file-{number}") in actions
        assert (owner["connection_id"], "store", f"store-{number}") in actions
    with db() as session:
        assert session.get(Document, document.id).status == "deleted"
    assert not path.exists()

def test_portable_document_calculation_starts_computer_and_saves_durable_export(provider_scope, desktop_job, monkeypatch):
    from app import documents
    from app.providers import document_analysis as analysis
    from app.computer import TaskComputer
    from app.persistence.models import ChannelMember
    from app.providers.management import create_connection
    from helpers import ScriptedModel
    from langchain_core.messages import AIMessage
    original, agent, user, _, client, sandbox = desktop_job
    with db() as session:
        reference = connection(session, "gemini", "gemini-offline")
        computer_connection = create_connection(session, provider="e2b", label="Calculation computer", key="offline", config={}, user_id=1)
        computer_binding = {"connection_id": computer_connection.id, "revision": 1, "provider": "e2b"}
        revision = session.get(ProviderRevision, (computer_connection.id, 1))
        revision.verification = {"passed": True, "pause_resume": True}
        session.add(revision)
        selection = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=selection.version, bindings={"llm": reference, "computer": computer_binding, "document_analysis": reference})
        session.add(ChannelMember(channel_id=original.channel_id, member_type="agent", member_id=agent.id))
        task = make_task(session, (agent, NS(id=original.channel_id)))
        document = Document(id=uuid4().hex, channel_id=original.channel_id, uploaded_by=user.id, name="source.csv", media_type="text/csv", size=12, status="ready",
            units=[{"location": "Rows 1–3", "text": "amount,2,2"}])
        session.add(document)
        session.commit()
    source = documents.path_for(document)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"amount\n2\n2\n")
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{"name": "run_python", "args": {"code": "print(2 + 2)"}, "id": "calculate"}]),
        AIMessage(content="", tool_calls=[{"name": "save_analysis_file", "args": {"path": "/home/user/result.csv"}, "id": "export"}]), AIMessage(content="The total is 4.")])
    monkeypatch.setattr(analysis, "for_binding", lambda *a: model)
    async def scenario():
        from app.providers.context import task_services
        computer = TaskComputer(task, client_factory=lambda: client)
        async with task_services(task, computer), computer:
            return await analysis.analyze(task, [document], reference, "Sum every row and export a CSV", True)
    # Pass the same task computer through the context used by the worker.
    answer = asyncio.run(scenario())
    assert answer["complete"] and len(answer["downloads"]) == 1
    assert client.create.await_count == 1
    assert sandbox.fs.upload_file.await_count == 1
    with db() as session:
        exported = session.get(Document, answer["downloads"][0]["url"].split("/")[-2])
        assert exported.channel_id == original.channel_id and exported.media_type == "text/csv"
        assert documents.path_for(exported).read_bytes() == b"hello notes"
        documents.path_for(exported).unlink(missing_ok=True)
    source.unlink(missing_ok=True)

def test_billable_research_replays_completed_requests_and_blocks_uncertain_retry(provider_scope, monkeypatch):
    from app.providers import research
    from app.providers.context import task_services
    from app.providers.contracts import ResolvedConnection
    with db() as session:
        task = make_task(session, provider_scope)
        session.commit()
    upstream = AsyncMock(side_effect=[{"results": []}, ProviderError("research")])
    monkeypatch.setattr(research, "_request", upstream)
    connection = ResolvedConnection("offline-research", 1, "tavily", {}, {"api_key": "offline"})
    async def scenario():
        async with task_services(task):
            assert await research.search(connection, "first") == {"results": []}
            assert await research.search(connection, "first") == {"results": []}
            assert upstream.await_count == 1
            with pytest.raises(ProviderError):
                await research.search(connection, "second")
            with pytest.raises(ProviderError, match="previous service request"):
                await research.search(connection, "second")
            assert upstream.await_count == 2
    asyncio.run(scenario())

@pytest.mark.parametrize("original_provider,destination_provider", [("e2b", "tenki"), ("tenki", "e2b"), ("tenki", "tenki")])
def test_switching_computer_connections_preserves_original_vm_and_disk(provider_scope, original_provider, destination_provider):
    from app.providers.management import create_connection
    from app.persistence.models import TaskComputerBinding, TaskSandbox, ComputerDisk
    agent, channel = provider_scope
    with db() as session:
        bindings = []
        for provider in (original_provider, destination_provider):
            row = create_connection(session, provider=provider, label="Separate computer account", key="offline", config={"image": "offline", "sharing": "workspace"} if provider == "tenki" else {}, user_id=1)
            revision = session.get(ProviderRevision, (row.id, 1))
            revision.verification = {"passed": True, "pause_resume": True}
            session.add(revision)
            bindings.append({"connection_id": row.id, "revision": 1, "provider": provider})
        selection = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=selection.version, bindings={"computer": bindings[0]})
        original = make_task(session, provider_scope)
        session.commit()
        binding = session.get(TaskComputerBinding, original.id)
        original_key = binding.computer_key
        session.add(TaskSandbox(task_id=original_key, name="Original computer-" + original.id, sandbox_id="vm-original", status="stopped"))
        session.add(ComputerDisk(key="test-disk-" + original.id, name="original-owned-disk-" + original.id, volume_id="vol-original", state="ready"))
        session.refresh(selection)
        apply_selection(session, scope="workspace", expected_version=selection.version, bindings={"computer": bindings[1]})
        destination = make_task(session, provider_scope)
        session.commit()
        assert session.get(TaskComputerBinding, destination.id).computer_key != original_key
        assert session.get(TaskComputerBinding, original.id).computer_key == original_key
        assert session.get(TaskServiceBinding, original.id).bindings["computer"] == bindings[0]
        assert session.get(TaskSandbox, original_key).sandbox_id == "vm-original"
        assert session.get(ComputerDisk, "test-disk-" + original.id).volume_id == "vol-original"

def test_reused_agent_row_cannot_resume_another_teammates_saved_task(provider_scope):
    from app.tasks.worker import execute
    agent, _ = provider_scope
    with db() as session:
        task = make_task(session, provider_scope)
        session.commit()
        current = session.get(AgentProfile, agent.id)
        current.band_agent_id = "replacement-" + uuid4().hex
        session.add(current)
        session.commit()
    def forbidden(*a, **kw): pytest.fail("A replaced identity reached the agent harness")
    with pytest.raises(ProviderError, match="removed teammate"):
        asyncio.run(execute(task, None, factory=forbidden))

def test_removed_agents_overrides_clear_without_resetting_revisions_or_workspace(provider_scope):
    from app.providers.management import clear_agent_overrides
    agent, _ = provider_scope
    with db() as session:
        reference = connection(session)
        apply_selection(session, scope=f"agent:{agent.id}", expected_version=0, bindings={"llm": reference})
        original = deepcopy(session.get(ServiceSelection, "workspace").bindings)
        clear_agent_overrides(session, agent)
        session.commit()
        cleared = session.get(ServiceSelection, f"agent:{agent.id}")
        assert cleared.bindings == {} and cleared.version == 2
        assert session.get(ServiceSelection, "workspace").bindings == original


def test_queued_upload_prepares_active_index_without_changing_saved_binding(provider_scope, monkeypatch):
    from app import documents
    from app.providers import retrieval
    from app.providers.context import document_services
    from app.providers.management import create_connection
    from app.providers.models import DocumentServiceBinding
    with db() as session:
        bindings = []
        for label in ("Original retrieval", "Replacement retrieval"):
            row = create_connection(session, provider="local", label=label, key="", config={}, user_id=1)
            revision = session.get(ProviderRevision, (row.id, 1))
            revision.verification = {"passed": True}
            session.add(revision)
            bindings.append({"connection_id": row.id, "revision": 1, "provider": "local"})
        workspace = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=workspace.version, bindings={"retrieval": bindings[0]})
        document = Document(id=uuid4().hex, channel_id=provider_scope[1].id, uploaded_by=1, name="queued.txt",
            media_type="text/plain", size=5, status="queued", units=[{"location": "Line 1", "text": "hello"}])
        session.add(document)
        session.commit()
        session.refresh(workspace)
        apply_selection(session, scope="workspace", expected_version=workspace.version, bindings={"retrieval": bindings[1]})
        session.commit()
    prepared = []
    async def index(doc, binding): prepared.append((doc.id, binding))
    monkeypatch.setattr(retrieval, "index_document", index)
    async def scenario():
        async with document_services(document.id):
            await documents.ingest(document.id)
    asyncio.run(scenario())
    assert prepared == [(document.id, binding) for binding in bindings]
    with db() as session:
        assert session.get(DocumentServiceBinding, document.id).bindings["retrieval"] == bindings[0]
        assert session.get(Document, document.id).status == "ready"


@pytest.mark.parametrize("action", ["retire", "restore"])
def test_emergency_revocation_cannot_be_undone_via_retirement(admin_space, action):
    space = admin_space
    with db() as session:
        reference = connection(session)
        session.commit()
    url = f"/api/settings/connections/{reference['connection_id']}/state"
    revoked = space.client.post(url, headers=space.auth,
        json={"action": "revoke", "expected_revision": 1, "expected_status": "active"})
    assert revoked.status_code == 200
    response = space.client.post(url, headers=space.auth,
        json={"action": action, "expected_revision": 1, "expected_status": "revoked"})
    assert response.status_code == 409
    with db() as session:
        assert session.get(ProviderConnection, reference["connection_id"]).status == "revoked"
