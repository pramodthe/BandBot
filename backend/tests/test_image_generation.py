"""Real agent-tool/storage contracts, with a fake image provider and no live model calls."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import AIMessage
from openai import APITimeoutError, PermissionDeniedError
from PIL import Image
from sqlmodel import select

from app import documents
from app.persistence.models import (Budget, ChannelMember, Document, ImageGeneration, MessageComponent,
                                    TaskDocument, TaskDocumentSource, TaskUsage, db)
from app.runtime.tools.provider_images import image_tools
from app.providers.models import ProviderOperation, ProviderRevision, TaskServiceBinding
from app.providers.management import create_connection
from app.providers.bindings import bindings_for
from app.providers.contracts import ProviderError
from app.tasks.usage import BudgetExceeded
from test_documents import space  # noqa: F401
from test_tasks import ScriptedModel, run_attempt


@pytest.fixture
def image_provider(space, monkeypatch):
    import app.providers.images as module
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), "#e6b252").save(buffer, format="PNG")
    data = buffer.getvalue()
    sdk = NS(images=NS(generate=AsyncMock(return_value=(data, {"input_tokens": 12, "output_tokens": 45}))))
    original = bindings_for(space.task)
    with db() as session:
        row = create_connection(session, provider="openai", label="Offline image service",
                                key="offline", config={"image_model": "offline-image"}, user_id=space.user.id)
        revision = session.get(ProviderRevision, (row.id, 1))
        revision.verification = {"passed": True, "image_model": "offline-image"}
        session.add(revision)
        binding = {"connection_id": row.id, "revision": 1, "provider": "openai", "model": "offline-image"}
        snapshot = session.get(TaskServiceBinding, space.task.id)
        snapshot.bindings = {**original, "images": binding}
        session.add(snapshot)
        session.commit()
    monkeypatch.setattr(module, "request_image", sdk.images.generate)
    return sdk, data


def invoke(task, call_id="image-call", **args):
    tool = image_tools(task)[0]
    message = asyncio.run(tool.ainvoke({"type": "tool_call", "name": tool.name, "id": call_id,
        "args": {"prompt": "A dog enjoying an apple, warm painterly illustration", "title": "Dog with an apple", **args}}))
    return json.loads(message.content)


def test_image_tool_saves_png_usage_inline_card_and_replays_without_regeneration(space, image_provider):
    sdk, data = image_provider
    first = invoke(space.task)
    again = invoke(space.task)
    assert first == again and first["status"] == "completed"
    sdk.images.generate.assert_awaited_once()
    assert len(sdk.images.generate.call_args.kwargs["safety_identifier"]) == 64
    assert "b64_json" not in str(first) and "offline-key" not in str(first)
    with db() as session:
        doc = session.get(Document, first["document_id"])
        assert session.get(TaskDocument, (space.task.id, doc.id))
        assert session.get(TaskDocumentSource, (space.task.id, doc.id))
        [component] = session.exec(select(MessageComponent).where(MessageComponent.task_id == space.task.id)).all()
        assert component.kind == "image" and component.props["documentId"] == doc.id
        [usage] = session.exec(select(TaskUsage).where(TaskUsage.task_id == space.task.id)).all()
        assert usage.input_tokens == 12 and usage.output_tokens == 45
    assert documents.path_for(doc).read_bytes() == data
    assert space.api.get(first["url"], headers=space.auth).content == data
    assert space.api.get(first["url"]).status_code == 401
    assert space.api.get(first["url"], headers=space.other_auth).status_code == 403
    assert space.api.delete(first["url"].removesuffix("/download"), headers=space.auth).status_code == 200
    with pytest.raises(ProviderError):
        invoke(space.task)
    assert sdk.images.generate.await_count == 1


def test_requested_image_is_linked_to_the_completed_reply_without_a_computer(space, image_provider, tmp_path):
    sdk, _ = image_provider
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "generate_image", "id": "render-picture", "args": {"prompt": "A dog eating an apple", "title": "Dog eating an apple"},
    }]), AIMessage(content="Here is the generated image.")])
    completed = run_attempt(tmp_path, space.task, model)
    assert completed.status == "completed"
    with db() as session:
        [component] = session.exec(select(MessageComponent).where(MessageComponent.task_id == space.task.id)).all()
        assert component.kind == "image" and component.message_id == completed.reply_message_id
    assert "/api/documents/" in completed.result
    sdk.images.generate.assert_awaited_once()


def test_image_helper_and_parent_have_the_tool_without_code_or_computer_fallback(space, monkeypatch):
    from app.runtime.harness import build_harness
    import app.runtime.harness as module
    captured = {}
    def fake_graph(**kwargs):
        captured.update(kwargs)
    monkeypatch.setattr(module, "create_deep_agent", fake_graph)
    build_harness(space.task, space.agent, None, model=ScriptedModel(responses=[]))
    assert "generate_image" in {t.name for t in captured["tools"] if hasattr(t, "name")}
    [helper] = [h for h in captured["subagents"] if h["name"] == "image"]
    names = {t.name for t in helper["tools"]}
    assert "generate_image" in names
    assert not {"code_interpreter", "run_python", "sandbox_exec", "computer_screenshot"} & names
    assert "Do not substitute hand-written SVG" in captured["system_prompt"]


def test_real_graph_delegates_generation_to_image_helper(space, image_provider, tmp_path):
    from app.persistence.models import AuditEvent
    sdk, _ = image_provider
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "id": "delegate-image", "args": {
            "subagent_type": "image", "description": "Generate the requested dog illustration with generate_image."}}]),
        AIMessage(content="", tool_calls=[{"name": "generate_image", "id": "helper-image", "args": {
            "prompt": "A dog with a red apple in a warm garden", "title": "Dog and apple"}}]),
        AIMessage(content="The image was generated and saved."),
        AIMessage(content="Here is your image."),
    ])
    assert run_attempt(tmp_path, space.task, model).status == "completed"
    sdk.images.generate.assert_awaited_once()
    with db() as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.task_id == space.task.id)).all()
        assert any(e.detail.get("tool") == "generate_image" and e.detail.get("via") == "helper" for e in events)
        assert not any(e.detail.get("tool") in {"code_interpreter", "sandbox_exec", "computer_screenshot"} for e in events)


def test_disabled_image_service_and_budget_prevent_paid_calls(space, image_provider):
    sdk, _ = image_provider
    with db() as session:
        snapshot = session.get(TaskServiceBinding, space.task.id)
        original = snapshot.bindings
        snapshot.bindings = {**original, "images": {"disabled": True}}
        session.add(snapshot); session.commit()
    with pytest.raises(ProviderError):
        invoke(space.task)
    with db() as session:
        assert not session.exec(select(ProviderOperation).where(ProviderOperation.task_id == space.task.id)).all()
        snapshot = session.get(TaskServiceBinding, space.task.id)
        snapshot.bindings = original
        session.add(snapshot)
        session.add(Budget(scope="agent", scope_id=space.agent.id, daily_tokens=0))
        session.commit()
    with pytest.raises(BudgetExceeded):
        invoke(space.task)
    sdk.images.generate.assert_not_awaited()


def test_access_revoked_during_generation_prevents_publication_but_counts_usage(space, image_provider):
    sdk, _ = image_provider
    response = sdk.images.generate.return_value
    async def revoke(*_args, **_kwargs):
        with db() as session:
            session.delete(session.get(ChannelMember, (space.channel.id, "agent", space.agent.id)))
            session.commit()
        return response
    sdk.images.generate.side_effect = revoke
    with pytest.raises(ProviderError):
        invoke(space.task)
    with db() as session:
        assert not session.exec(select(Document).where(Document.channel_id == space.channel.id)).all()
        assert not session.exec(select(MessageComponent).where(MessageComponent.task_id == space.task.id)).all()
        assert session.exec(select(TaskUsage).where(TaskUsage.task_id == space.task.id)).first()


def test_uncertain_image_request_blocks_replay_and_new_call_id(space, image_provider):
    sdk, _ = image_provider
    sdk.images.generate.side_effect = APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/images/generations"))
    for call_id in ["image-call", "image-call", "retry-with-new-id"]:
        with pytest.raises(ProviderError):
            invoke(space.task, call_id)
    sdk.images.generate.assert_awaited_once()


def test_image_options_reach_the_provider_and_helper_stays_focused(space, image_provider):
    sdk, _ = image_provider
    assert invoke(space.task, transparent_background=True, size="1536x1024", quality="high")["status"] == "completed"
    args = sdk.images.generate.call_args.kwargs
    assert args["transparent_background"] is True and args["size"] == "1536x1024" and args["quality"] == "high"


def test_image_loading_lifecycle_is_scoped_and_published_reply_removes_activity(space, image_provider):
    from app.persistence.models import AgentTask
    sdk, _ = image_provider
    with db() as session:
        task = session.get(AgentTask, space.task.id)
        task.status = "running"
        session.add(task); session.commit()
    url = f"/api/channels/{space.channel.id}/image-generations"
    response = sdk.images.generate.return_value
    async def pending(*_args, **_kwargs):
        result = space.api.get(url, headers=space.auth)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        [activity] = result.json()
        assert activity["status"] == "generating" and activity["title"] == "Dog with an apple"
        assert activity["size"] == "1536x1024" and activity["document_id"] is None
        assert "prompt" not in activity and "b64_json" not in str(activity)
        return response
    sdk.images.generate.side_effect = pending
    result = invoke(space.task, size="1536x1024")
    assert result["status"] == "completed"
    [activity] = space.api.get(url, headers=space.auth).json()
    assert activity["status"] == "ready" and activity["document_id"] == result["document_id"]
    assert space.api.get(url).status_code == 401
    assert space.api.get(url, headers=space.other_auth).status_code == 403
    with db() as session:
        task = session.get(AgentTask, space.task.id)
        task.reply_message_id = 123456
        session.add(task); session.commit()
    assert space.api.get(url, headers=space.auth).json() == []


def test_loading_stops_on_failure_pause_cancel_and_private_scope(space, image_provider):
    from app.persistence.models import AgentTask, DirectMessage, ImageGenerationDisplay
    with db() as session:
        task = session.get(AgentTask, space.task.id)
        task.status = "running"
        session.add(task)
        session.add(ImageGeneration(id="loading-test", task_id=task.id, model="offline"))
        session.add(ImageGenerationDisplay(id="loading-test", title="Picture", size="1024x1024"))
        session.commit()
    url = f"/api/channels/{space.channel.id}/image-generations"
    for task_status, receipt_status, expected in [("running", "failed", "error"), ("waiting", "pending", "error"),
                                                 ("cancelled", "pending", None), ("failed", "pending", None)]:
        with db() as session:
            task = session.get(AgentTask, space.task.id); task.status = task_status; session.add(task)
            receipt = session.get(ImageGeneration, "loading-test"); receipt.status = receipt_status; session.add(receipt)
            session.commit()
        rows = space.api.get(url, headers=space.auth).json()
        assert (rows[0]["status"] if rows else None) == expected
    with db() as session:
        session.add(DirectMessage(channel_id=space.channel.id, user_id=space.user.id, agent_id=space.agent.id))
        session.add(ChannelMember(channel_id=space.channel.id, member_type="human", member_id=space.other.id))
        session.commit()
    assert space.api.get(url, headers=space.other_auth).status_code == 404


def test_provider_error_is_sanitized_and_does_not_claim_svg_success(space, image_provider):
    sdk, _ = image_provider
    sdk.images.generate.side_effect = PermissionDeniedError("secret provider body", response=httpx.Response(
        403, request=httpx.Request("POST", "https://api.openai.com")), body={"secret": "must-not-leak"})
    with pytest.raises(ProviderError) as exc:
        invoke(space.task)
    assert "secret" not in str(exc.value) and "must-not-leak" not in str(exc.value)


@pytest.mark.parametrize("encoded", [b"not-image", b"<svg>code drawing</svg>", b""])
def test_invalid_provider_images_are_never_published(space, image_provider, encoded):
    sdk, _ = image_provider
    sdk.images.generate.return_value = (encoded, {})
    with pytest.raises(ProviderError):
        invoke(space.task)
    with db() as session:
        assert not session.exec(select(Document).where(Document.channel_id == space.channel.id)).all()


def test_cancelled_task_cannot_publish_or_start_an_image(space, image_provider):
    from app.persistence.models import AgentTask
    sdk, _ = image_provider
    with db() as session:
        task = session.get(AgentTask, space.task.id)
        task.cancel_requested = True
        session.add(task); session.commit()
    with pytest.raises(ProviderError):
        invoke(space.task)
    sdk.images.generate.assert_not_awaited()


def test_openai_image_request_disables_retries_and_requests_png(monkeypatch):
    from app.providers.images import request_image
    from app.providers.contracts import ResolvedConnection
    import openai
    sdk = NS(images=NS(generate=AsyncMock(return_value=NS(
        data=[NS(b64_json=base64.b64encode(b"PNG-fixture").decode())], usage=None))))
    context = AsyncMock()
    context.__aenter__.return_value = sdk
    constructor = Mock(return_value=context)
    monkeypatch.setattr(openai, "AsyncOpenAI", constructor)
    connection = ResolvedConnection("offline", 1, "openai", {}, {"api_key": "offline"})
    data, _ = asyncio.run(request_image(connection, "offline-image", "A picture", size="1536x1024",
                                         quality="high", transparent_background=True, safety_identifier="hashed"))
    assert data == b"PNG-fixture" and constructor.call_args.kwargs["max_retries"] == 0
    args = sdk.images.generate.call_args.kwargs
    assert args["output_format"] == "png" and args["n"] == 1 and args["background"] == "transparent"
    assert args["size"] == "1536x1024" and args["quality"] == "high"

def test_cancellation_during_paid_call_cannot_publish_an_image(space, image_provider):
    from app.persistence.models import AgentTask
    sdk, _ = image_provider
    response = sdk.images.generate.return_value

    async def cancel(*_args, **_kwargs):
        with db() as session:
            task = session.get(AgentTask, space.task.id)
            task.cancel_requested = True
            session.add(task)
            session.commit()
        return response

    sdk.images.generate.side_effect = cancel
    with pytest.raises(ProviderError):
        invoke(space.task)
    sdk.images.generate.assert_awaited_once()
    with db() as session:
        assert not session.exec(select(Document).where(Document.channel_id == space.channel.id)).all()
        assert not session.exec(select(MessageComponent).where(MessageComponent.task_id == space.task.id)).all()
        assert session.exec(select(TaskUsage).where(TaskUsage.task_id == space.task.id)).first()

def test_legacy_completed_image_replays_without_a_new_paid_call(space, image_provider):
    from uuid import NAMESPACE_URL, uuid5
    sdk, _ = image_provider
    first = invoke(space.task)
    legacy_id = uuid5(NAMESPACE_URL, f"band:image:{space.task.id}:legacy-call").hex
    with db() as session:
        session.add(ImageGeneration(id=legacy_id, task_id=space.task.id, status="completed",
                                    document_id=first["document_id"], model="legacy-image"))
        session.commit()
    sdk.images.generate.reset_mock()
    replay = invoke(space.task, "legacy-call")
    assert replay["document_id"] == first["document_id"]
    sdk.images.generate.assert_not_awaited()


def test_uncertain_legacy_image_blocks_upgrade_replay_and_new_call_id(space, image_provider):
    from uuid import NAMESPACE_URL, uuid5
    sdk, _ = image_provider
    legacy_id = uuid5(NAMESPACE_URL, f"band:image:{space.task.id}:legacy-call").hex
    with db() as session:
        session.add(ImageGeneration(id=legacy_id, task_id=space.task.id, status="uncertain", model="legacy-image"))
        session.commit()
    for call_id in ("legacy-call", "different-call"):
        with pytest.raises(ProviderError):
            invoke(space.task, call_id)
    sdk.images.generate.assert_not_awaited()
