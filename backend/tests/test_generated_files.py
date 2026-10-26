"""Regression for streamed plot citations, durable downloads, and stateless continuation."""
import asyncio
import io
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from PIL import Image
from sqlmodel import select

from app import documents
from app.persistence.models import ChannelMember, Document, TaskDocument, TaskDocumentSource, db
from app.runtime.generated_files import GeneratedFilesMiddleware
from app.runtime.harness import build_harness
from app.tasks.worker import execute
from helpers import sdk_mock, upload, publish
from helpers import ScriptedModel
ANNOTATION = {"type": "container_file_citation", "container_id": "private-container",
              "file_id": "private-file", "filename": "sales-chart.png", "start_index": 0, "end_index": 0}


def chart_message(block_type="function_call"):
    return AIMessage(id="response-chart", content=[
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "preserve-this"},
        {"type": "code_interpreter_call", "id": "ci_1", "container_id": "private-container",
         "status": "completed", "code": "plt.show()"},
        ({"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "write_todos",
          "arguments": '{"todos": []}', "annotations": [ANNOTATION]} if block_type == "function_call" else
         {"type": "text", "text": "Sales chart", "annotations": [ANNOTATION]})],
        tool_calls=[{"name": "write_todos", "args": {"todos": []}, "id": "call_1"}]
        if block_type == "function_call" else [])


def fake_download(monkeypatch):
    import app.runtime.generated_files as middleware
    import app.runtime.tools.documents as tools
    data = io.BytesIO()
    Image.new("RGB", (100, 60), "white").save(data, format="PNG")
    download = AsyncMock(return_value=("sales-chart.png", "image/png", data.getvalue()))
    monkeypatch.setattr(middleware, "client", sdk_mock().client)
    monkeypatch.setattr(tools, "generated_file", download)
    return download, data.getvalue()


@pytest.mark.parametrize("block_type", ["function_call", "text"])
def test_plot_citations_persist_and_replay_as_valid_responses_input(space, monkeypatch, block_type):
    download, data = fake_download(monkeypatch)
    original = chart_message(block_type)
    middleware = GeneratedFilesMiddleware(space.task)
    normalized = asyncio.run(middleware.aafter_model({"messages": [original]}, None))["messages"][0]
    assert normalized.id == original.id and normalized.tool_calls == original.tool_calls
    assert normalized.content[:2] == original.content[:2]  # Preserve CI and encrypted reasoning.
    assert original.content[2]["annotations"] == [ANNOTATION]  # No in-place checkpoint mutation.
    if block_type == "function_call":
        assert "annotations" not in normalized.content[2]
    link = normalized.content[-1]["text"].split("(")[-1].rstrip(")")
    assert space.api.get(link, headers=space.auth).content == data
    assert space.api.get(link, headers=space.other_auth).status_code == 403
    assert documents.validate_citations(space.task, f"[chart]({link})") == f"[chart]({link})"
    model = ChatOpenAI(model="gpt-6-luna", api_key="offline", use_responses_api=True, store=False)
    messages = [HumanMessage("Plot sales"), normalized]
    if normalized.tool_calls:
        messages.append(ToolMessage(content="Updated", tool_call_id="call_1"))
    payload = model._get_request_payload(messages)
    for item in payload["input"]:
        if item["type"] == "function_call":
            assert set(item) <= {"type", "id", "call_id", "name", "arguments", "status"}
    assert "preserve-this" in str(payload)
    # A replay reuses saved bytes/metadata; deleting the document never resurrects it.
    again = asyncio.run(middleware.aafter_model({"messages": [original]}, None))["messages"][0]
    assert again.content[-1] == normalized.content[-1]
    assert download.await_count == 1
    assert space.api.delete(link.removesuffix("/download"), headers=space.auth).status_code == 200
    update = asyncio.run(middleware.aafter_model({"messages": [original]}, None))
    assert update is None or "Generated files" not in str(update)
    assert download.await_count == 1
    assert space.api.get(link, headers=space.auth).status_code == 404


def test_plot_checkpoint_survives_new_worker_before_final_answer(space, monkeypatch, tmp_path):
    download, data = fake_download(monkeypatch)
    first = ScriptedModel(responses=[chart_message(), RuntimeError("worker stopped")])
    second = ScriptedModel(responses=[AIMessage(content="[Download chart](sandbox:/mnt/data/temporary-plot.png)")])

    async def scenario():
        checkpoint = str(tmp_path / "chart-checkpoint.db")
        async with AsyncSqliteSaver.from_conn_string(checkpoint) as saver:
            with pytest.raises(RuntimeError, match="worker stopped"):
                await execute(space.task, saver, factory=lambda t, p, c, **kw:
                              build_harness(t, p, c, model=first, **kw))
        with db() as session:
            saved = session.exec(select(Document).where(Document.channel_id == space.channel.id)).one()
            assert session.get(TaskDocument, (space.task.id, saved.id))
            assert session.get(TaskDocumentSource, (space.task.id, saved.id))
        async with AsyncSqliteSaver.from_conn_string(checkpoint) as saver:
            graph = build_harness(space.task, space.agent, saver, model=second)
            state = await graph.aget_state({"configurable": {"thread_id": space.task.id}})
            chart = next(m for m in state.values["messages"] if m.id == "response-chart")
            assert "annotations" not in chart.content[2]
            assert f"/api/documents/{saved.id}/download" in str(chart.content)
            result = await execute(space.task, saver, factory=lambda t, p, c, **kw:
                                   build_harness(t, p, c, model=second, **kw))
        assert "sandbox:" not in result
        assert f"[sales-chart.png](/api/documents/{saved.id}/download)" in result
        return saved
    saved = asyncio.run(scenario())
    assert download.await_count == 1
    assert documents.path_for(saved).read_bytes() == data


def test_membership_revoked_during_artifact_download_prevents_publication(space, monkeypatch):
    download, _ = fake_download(monkeypatch)
    result = download.return_value

    async def revoked(*args):
        with db() as session:
            session.delete(session.get(ChannelMember, (space.channel.id, "agent", space.agent.id)))
            session.commit()
        return result
    download.side_effect = revoked
    with pytest.raises(ValueError, match="no longer has access"):
        asyncio.run(GeneratedFilesMiddleware(space.task).aafter_model({"messages": [chart_message()]}, None))
    with db() as session:
        assert not session.exec(select(Document).where(Document.channel_id == space.channel.id)).all()


def test_source_deleted_during_download_prevents_generated_artifact_publication(space, monkeypatch):
    from app.runtime.tools.documents import save_generated_file
    download, _ = fake_download(monkeypatch)
    original = upload(space)
    publish(space, original)
    result = download.return_value

    async def deleted(*args):
        assert space.api.delete(f"/api/documents/{original['id']}", headers=space.auth).status_code == 200
        return result
    download.side_effect = deleted
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(save_generated_file(space.task, None, NS(**ANNOTATION), source_ids=(original["id"],)))
    with db() as session:
        assert len(session.exec(select(Document).where(Document.channel_id == space.channel.id)).all()) == 1


def test_recovery_repairs_uncommitted_artifact_bytes(space, monkeypatch):
    from app.runtime.tools.documents import save_generated_file
    _, data = fake_download(monkeypatch)
    doc_id = uuid5(NAMESPACE_URL, f"band:{space.task.id}:private-container:private-file").hex
    orphan = Document(id=doc_id, channel_id=space.channel.id, uploaded_by=0, name="sales-chart.png",
                      media_type="image/png", size=3)
    path = documents.path_for(orphan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"interrupted write")
    result = asyncio.run(save_generated_file(space.task, None, NS(**ANNOTATION)))
    assert result["url"] == f"/api/documents/{doc_id}/download"
    assert path.read_bytes() == data
    assert not list(path.parent.glob(f"{path.name}.*.tmp"))


def test_provider_generated_filename_is_not_exposed_in_document_metadata(space, monkeypatch):
    from app.runtime.tools.documents import save_generated_file
    download, data = fake_download(monkeypatch)
    download.return_value = ("cfile_privatefile.png", "image/png", data)
    saved = asyncio.run(save_generated_file(space.task, None, NS(**ANNOTATION)))
    assert saved["name"].startswith("generated-image-")
    response = space.api.get(saved["url"].removesuffix('/download'), headers=space.auth)
    assert response.status_code == 200
    assert "privatefile" not in response.text and "private-container" not in response.text
    assert space.api.get(saved["url"], headers=space.auth).content == data


@pytest.mark.parametrize("target", ["sandbox:/mnt/data/sales-chart.png", "{url}", "https://chatgpt.com{url}"])
def test_final_reply_uses_verified_download_and_does_not_duplicate_or_revive_it(space, monkeypatch, target):
    from app.runtime.tools.documents import save_generated_file
    fake_download(monkeypatch)
    saved = asyncio.run(save_generated_file(space.task, None, NS(**ANNOTATION)))
    text = f"[Download chart]({target.format(url=saved['url'])})"
    expected = f"[Download chart]({saved['url']})"
    assert documents.finalize_document_links(space.task, text) == expected
    assert documents.finalize_document_links(space.task, expected) == expected
    assert space.api.delete(saved['url'].removesuffix('/download'), headers=space.auth).status_code == 200
    result = documents.finalize_document_links(space.task, text)
    assert "sandbox:" not in result and saved['url'] not in result
