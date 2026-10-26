"""Offline adapter and lifecycle contracts, including operation without OpenAI."""
import asyncio
from copy import deepcopy
import io
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from PIL import Image
from sqlmodel import select
from app.persistence.models import (db, ChannelMember, Document,
    AgentTask, TaskModel, TaskUsage)
from app.providers.models import (ProviderConnection, ProviderRevision, ServiceSelection,
    TaskServiceBinding, DocumentServiceBinding, ProviderOperation, ProviderUsage)
from app.providers.management import create_connection, apply_selection, rotate
from app.providers.connections import resolve
from app.providers.contracts import ResolvedConnection, ProviderError
from app.providers.language_models import build_model, verify_models
from helpers import connection, make_task

@pytest.mark.parametrize("provider,model", [("openai", "gpt-offline"), ("anthropic", "claude-offline"),
    ("gemini", "gemini-offline"), ("compatible", "local-model")])
def test_native_adapter_builds_without_network_and_keeps_provider_identity(provider, model):
    config = {"base_url": "https://models.example/v1", "context_window": 32000}
    result = build_model(ResolvedConnection("test", 1, provider, config, {"api_key": "offline-key"}), model)
    if provider == "compatible":
        assert result.use_responses_api is False
        asyncio.run(result.http_async_client.aclose())
    elif provider == "openai":
        assert result.use_responses_api is True
    elif provider == "gemini":
        assert result.vertexai is False
    assert "offline-key" not in repr(ResolvedConnection("test", 1, provider, {}, {"api_key": "offline-key"}))

@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "compatible"])
def test_verification_requires_image_tools_continuation_and_usage(provider, monkeypatch):
    import app.providers.language_models as models
    seen = []
    class Fake:
        def bind_tools(self, tools): return NS(ainvoke=self.complete)
        async def ainvoke(self, messages): raise AssertionError("Tool declarations must also accompany continuation")
        async def complete(self, messages):
            seen.append(messages)
            if len(messages) == 1:
                assert "data:image/png;base64," in str(messages[0].content)
                return AIMessage(content="", tool_calls=[{"name": "check_color", "args": {"color": "red"}, "id": "check"}],
                    usage_metadata={"input_tokens": 20, "output_tokens": 4, "total_tokens": 24})
            assert isinstance(messages[-1], ToolMessage)
            return AIMessage(content="VERIFIED", usage_metadata={"input_tokens": 30, "output_tokens": 2, "total_tokens": 32})
    monkeypatch.setattr(models, "build_model", lambda *a: Fake())
    verified = asyncio.run(verify_models(ResolvedConnection("test", 1, provider, {}, {}), ["model"]))
    assert verified["model"] == {"tools": True, "vision": True, "usage": True}
    assert len(seen) == 2
    class TextOnly(Fake):
        async def complete(self, messages): return AIMessage(content="I can chat")
    monkeypatch.setattr(models, "build_model", lambda *a: TextOnly())
    with pytest.raises(ValueError):
        asyncio.run(verify_models(ResolvedConnection("test", 1, provider, {}, {}), ["model"]))

def test_admin_api_secret_masking_activation_revision_conflicts_and_linked_restart(admin_space):
    s = admin_space
    client = s.client
    assert client.get("/api/settings/providers", headers=s.member).status_code == 403
    result = client.post("/api/settings/connections", headers=s.auth, json={"provider": "anthropic", "label": "Test reasoning", "api_key": "private-test-key", "config": {"models": ["claude-offline"]}})
    assert result.status_code == 201 and "private-test-key" not in result.text and "secret_enc" not in result.text
    row = result.json()
    binding = {"connection_id": row["id"], "revision": 1, "provider": "anthropic", "model": "claude-offline"}
    settings = client.get("/api/settings/providers", headers=s.auth).json()
    body = {"expected_version": settings["version"], "bindings": {"llm": binding}}
    assert client.put("/api/settings/services", headers=s.auth, json=body).status_code == 422
    with db() as session:
        revision = session.get(ProviderRevision, (row["id"], 1))
        revision.verification = {"passed": True, "models": {"claude-offline": {"tools": True, "vision": True}}}
        session.add(revision)
        session.commit()
    assert client.post("/api/settings/services/preview", headers=s.auth, json=body).status_code == 200
    assert client.put("/api/settings/services", headers=s.auth, json=body).status_code == 200
    assert client.put("/api/settings/services", headers=s.auth, json=body).status_code == 409
    with db() as session:
        task = make_task(session, (s.agent, s.channel))
        task.status = "failed"
        session.add(task)
        session.commit()
        original = deepcopy(session.get(TaskServiceBinding, task.id).bindings)
    response = client.post(f"/api/tasks/{task.id}/restart-with-providers", headers=s.auth)
    assert response.status_code == 200, response.text
    assert response.json()["id"] != task.id
    assert response.json()["restarted_from"] == task.id
    assert client.post(f"/api/tasks/{task.id}/restart-with-providers", headers=s.auth).json()["id"] == response.json()["id"]
    with db() as session:
        assert session.get(TaskServiceBinding, task.id).bindings == original
        assert session.get(AgentTask, task.id).status == "failed"
        assert session.get(ProviderOperation, "restart:" + response.json()["id"]).result["original_task_id"] == task.id

@pytest.mark.parametrize("state", ["queued", "running", "waiting", "failed", "cancelled"])
def test_key_rotation_and_default_changes_do_not_rebind_existing_work(provider_scope, state):
    with db() as session:
        first = connection(session, "anthropic", "claude-offline")
        setting = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=setting.version, bindings={"llm": first, "document_analysis": first})
        task = make_task(session, provider_scope)
        task.status = state
        session.add(task)
        document = Document(id=uuid4().hex, channel_id=task.channel_id, uploaded_by=1, name="scan.png", media_type="image/png", size=1, status="queued")
        session.add(document)
        session.commit()
        old = deepcopy(session.get(TaskServiceBinding, task.id).bindings)
        row = session.get(ProviderConnection, first["connection_id"])
        rotate(session, row, expected_revision=1, key="new-key", config=None)
        second = connection(session, "gemini", "gemini-offline")
        session.refresh(setting)
        apply_selection(session, scope="workspace", expected_version=setting.version, bindings={"llm": second, "document_analysis": second})
        session.commit()
    with db() as session:
        assert session.get(TaskServiceBinding, task.id).bindings == old
        assert session.get(DocumentServiceBinding, document.id).bindings["document_analysis"] == first
        assert resolve(old["llm"], "llm", session=session).secret["api_key"] == "offline-secret"
        assert session.get(TaskModel, task.id).provider == "anthropic"

def test_no_openai_document_search_analysis_and_gemini_image_are_independent(admin_space, monkeypatch):
    from app import documents
    import app.providers.document_analysis as analysis
    import app.providers.images as images
    from app.providers.retrieval import search
    s = admin_space
    with db() as session:
        llm = connection(session, "gemini", "gemini-offline")
        local = session.get(ProviderConnection, "imported-local")
        retrieval = {"connection_id": local.id, "revision": 1, "provider": "local"}
        setting = session.get(ServiceSelection, "workspace")
        apply_selection(session, scope="workspace", expected_version=setting.version,
            bindings={"llm": llm, "document_analysis": llm, "retrieval": retrieval})
        task = make_task(session, (s.agent, s.channel))
        document = Document(id=uuid4().hex, channel_id=s.channel.id, uploaded_by=1, name="budget.txt", media_type="text/plain", size=23, status="queued")
        session.add(document)
        session.commit()
    path = documents.path_for(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Budget for launch: 1234", encoding="utf-8")
    asyncio.run(documents.ingest(document.id))
    result = asyncio.run(search(task, retrieval, "launch budget"))
    assert result["results"][0]["document_id"] == document.id
    with db() as session:
        doc = session.get(Document, document.id)
    model = NS(ainvoke=AsyncMock(return_value=AIMessage(content="Budget 1234 (line 1)", usage_metadata={"input_tokens": 15, "output_tokens": 5, "total_tokens": 20})))
    monkeypatch.setattr(analysis, "for_binding", lambda *a: model)
    answer = asyncio.run(analysis.analyze(task, [doc], llm, "What budget?", False))
    assert "1234" in answer["answer"]
    png = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(png, "PNG")
    request = AsyncMock(return_value=(png.getvalue(), {"prompt_token_count": 10, "candidates_token_count": 20, "thoughts_token_count": 5}))
    monkeypatch.setattr(images, "request_image", request)
    image_binding = {**llm, "model": "gemini-image-offline"}
    first = asyncio.run(images.generate(task, image_binding, "A green square", "image-call"))
    again = asyncio.run(images.generate(task, image_binding, "A green square", "image-call"))
    assert first == again and request.await_count == 1
    with db() as session:
        usage, attribution = session.exec(select(TaskUsage, ProviderUsage).join(ProviderUsage, ProviderUsage.usage_id == TaskUsage.id)
            .where(TaskUsage.task_id == task.id, ProviderUsage.service == "images")).one()
        assert (usage.input_tokens, usage.output_tokens, usage.model) == (10, 25, "gemini:gemini-image-offline")
        assert attribution.connection_id == llm["connection_id"]
    assert s.client.get(first["url"], headers=s.auth).status_code == 200
    with db() as session:
        session.delete(session.get(ChannelMember, (s.channel.id, "agent", s.agent.id)))
        session.commit()
    with pytest.raises(ValueError, match="access"):
        asyncio.run(search(task, retrieval, "budget"))
    path.unlink(missing_ok=True)

def test_uncertain_image_dispatch_is_not_repeated_after_worker_restart(admin_space, monkeypatch):
    import app.providers.images as images
    from app.providers.verification import recover
    s = admin_space
    with db() as session:
        binding = connection(session, "gemini", "gemini-offline")
        task = make_task(session, (s.agent, s.channel))
        session.commit()
    request = AsyncMock(side_effect=TimeoutError("secret-bearing upstream error"))
    monkeypatch.setattr(images, "request_image", request)
    for _ in range(2):
        with pytest.raises(ProviderError, match="confirmed|already dispatched") as exc:
            asyncio.run(images.generate(task, binding, "A square", "uncertain-image"))
        assert "secret-bearing" not in str(exc.value)
        recover()
    assert request.await_count == 1

def test_internal_flags_cannot_bypass_provider_activation(provider_scope):
    from app.providers.management import validate_bindings
    with db() as session:
        for binding in ({"legacy": True}, {"connected": True}, {"disabled": True, "connection_id": "hidden"}):
            with pytest.raises(ValueError):
                validate_bindings(session, {"llm": binding})

def test_revocation_blocks_original_credentials_without_erasing_bindings(provider_scope):
    with db() as session:
        binding = connection(session)
        row = session.get(ProviderConnection, binding["connection_id"])
        row.status = "retired"
        session.add(row)
        session.commit()
        assert resolve(binding, "llm").secret["api_key"] == "offline-secret"
        row.status = "revoked"
        session.add(row)
        session.commit()
        with pytest.raises(ProviderError) as error:
            resolve(binding, "llm")
        assert error.value.code == "revoked"

@pytest.mark.parametrize("llm_provider", ["openai", "anthropic", "gemini", "compatible"])
@pytest.mark.parametrize("computer_provider", ["e2b", "tenki"])
def test_model_computer_contract_approval_and_restart_matrix(desktop_job, tmp_path, llm_provider, computer_provider):
    from app.computer import TaskComputer, sandbox_record
    from app.providers.model_dispatch import DurableModel
    from app.runtime.harness import build_harness
    from app.tasks.worker import execute
    from app.tasks.worker import ApprovalRequired
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from helpers import ScriptedModel
    from app.persistence.models import TaskApproval
    original_task, agent, user, _, client, sandbox = desktop_job
    with db() as session:
        model_config = {"models": ["test-model"]}
        if llm_provider == "compatible":
            model_config.update(base_url="https://models.example/v1", context_window=32000)
        row = create_connection(session, provider=llm_provider, label="Matrix model", key="offline", config=model_config, user_id=1)
        llm = {"connection_id": row.id, "revision": 1, "provider": llm_provider, "model": "test-model"}
        computer_row = create_connection(session, provider=computer_provider, label="Matrix computer", key="offline",
            config={"image": "offline-desktop", "sharing": "workspace"} if computer_provider == "tenki" else {}, user_id=1)
        computer_binding = {"connection_id": computer_row.id, "revision": 1, "provider": computer_provider}
        for b in (llm, computer_binding):
            revision = session.get(ProviderRevision, (b["connection_id"], 1))
            revision.verification = {"passed": True, "pause_resume": True, "models": {"test-model": {"tools": True, "vision": True}}}
            session.add(revision)
        session.add(ChannelMember(channel_id=original_task.channel_id, member_type="agent", member_id=agent.id))
        setting = session.get(ServiceSelection, "workspace")
        previous = deepcopy(setting.bindings)
        apply_selection(session, scope="workspace", expected_version=setting.version,
            bindings={"llm": llm, "computer": computer_binding, "integrations": {"disabled": True}})
        task = make_task(session, (agent, NS(id=original_task.channel_id)))
        session.commit()
    controller = TaskComputer(task, client_factory=lambda: client)
    scripted = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "computer_screenshot", "args": {}, "id": "see"}]),
        AIMessage(content="", tool_calls=[{"name": "run_python", "args": {"code": "print(2 + 2)"}, "id": "calculate"}]),
        AIMessage(content="", tool_calls=[{"name": "request_approval", "args": {"action": "Draft approval test", "details": "offline"}, "id": "ask"}]),
        AIMessage(content="Verified the desktop.")])
    durable = DurableModel(inner=scripted, binding=llm, task_id=task.id)
    factory = lambda t,p,c,**kw: build_harness(t,p,c,model=durable,**kw)
    path = str(tmp_path / "provider-matrix.db")
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            await saver.setup()
            with pytest.raises(ApprovalRequired) as paused:
                await execute(task, saver, factory=factory, computer=controller)
            from app.tasks.queue import await_approval
            await_approval(task, paused.value.requests)
        assert client.create.await_count == 1
        assert sandbox.process.exec.await_count >= 1
        with db() as session:
            approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
            approval.status = "approved"
            approval.decided_by = "Offline tester"
            session.add(approval)
            row = session.get(ProviderConnection, llm["connection_id"])
            rotate(session, row, expected_revision=1, key="rotated-offline", config=None)
            setting = session.get(ServiceSelection, "workspace")
            setting.bindings = previous
            session.add(setting)
            session.commit()
        from app.computer import take_control, release_control, ComputerUnavailable
        take_control(controller.key, user_id=user.id, user_name="Human tester")
        with pytest.raises(ComputerUnavailable):
            await controller.command("echo must-not-run")
        release_control(controller.key, user_name="Human tester")
        sandbox.state = "stopped"
        # A new SDK controller and checkpointer instance represent worker restart.
        replacement_controller = TaskComputer(task, client_factory=lambda: client)
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            answer = await execute(task, saver, factory=factory, computer=replacement_controller)
        assert answer == "Verified the desktop."
        assert client.create.await_count == 1
        assert sandbox_record(controller.key).sandbox_id == sandbox.id
        with db() as session:
            assert session.get(TaskServiceBinding, task.id).bindings["llm"] == llm
    try:
        asyncio.run(scenario())
    finally:
        with db() as session:
            setting = session.get(ServiceSelection, "workspace")
            setting.bindings = previous
            session.add(setting)
            session.commit()

def test_model_dispatch_replays_completed_results_but_blocks_uncertain_calls(provider_scope):
    from app.providers.model_dispatch import DurableModel
    from helpers import ScriptedModel
    from langchain_core.messages import HumanMessage
    with db() as session:
        binding = connection(session, "anthropic", "claude-offline")
        task = make_task(session, provider_scope)
        session.commit()
    native = ScriptedModel(responses=[AIMessage(content="Saved result"), TimeoutError("uncertain")])
    model = DurableModel(inner=native, binding=binding, task_id=task.id)
    async def scenario():
        messages = [HumanMessage(content="First request")]
        assert (await model.ainvoke(messages)).content == "Saved result"
        assert (await model.ainvoke(messages)).content == "Saved result"
        assert native.index == 1
        with pytest.raises(TimeoutError):
            await model.ainvoke([HumanMessage(content="Second request")])
        with pytest.raises(ProviderError, match="interrupted"):
            await model.ainvoke([HumanMessage(content="Second request")])
        assert native.index == 2
    asyncio.run(scenario())
