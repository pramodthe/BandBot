"""Full-file calculations feed native assistant-ui charts; image exports remain optional. Offline."""
import json

from langchain_core.messages import AIMessage
from sqlmodel import select

from app import documents
from app.persistence.models import Document, MessageComponent, db
from app.runtime.tools.documents import document_tools
from helpers import upload, publish, sdk_mock
from helpers import ScriptedModel, run_attempt
def test_full_spreadsheet_calculation_reaches_native_chart_and_survives_recovery(space, monkeypatch, tmp_path):
    import app.runtime.tools.documents as tools
    values = [(row * 7) % 101 - 35 for row in range(1, 1501)]
    doc = upload(space, "sales.csv", ("Row,Amount\n" + "\n".join(
        f"{row},{value}" for row, value in enumerate(values, 1))).encode())
    publish(space, doc)
    documents.patch(doc["id"], status="ready", file_id="complete-original",
                    units=[{"location": "Preview", "text": "Only a preview; never calculate from this."}])
    data = [{"label": f"{start + 1}-{start + 100}", "value": sum(values[start:start + 100])}
            for start in range(0, len(values), 100)]
    fake = sdk_mock()
    fake.sdk.responses.create.return_value.output_text = json.dumps({
        "row_count": len(values), "total": sum(values), "data": data,
        "coverage": "All 1500 data rows, grouped in consecutive blocks of 100; Amount units unspecified"})
    monkeypatch.setattr(tools, "client", fake.client)
    ui = {"_type": "Col", "gap": 4, "children": [
        {"_type": "Fact", "label": "Data rows", "value": str(len(values))},
        {"_type": "Chart", "variant": "bar", "data": data, "showAxis": True},
        {"_type": "Text", "value": "X: record ranges; Y: total Amount (units unspecified). All rows included."}]}
    chart_call = AIMessage(content="", tool_calls=[{"name": "present_ui", "id": "native-chart",
        "args": {"title": "Sales by 100-record block", "ui": ui}}])
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "analyze_documents", "id": "calculate",
            "args": {"document_ids": [doc["id"]], "calculate": True,
                     "question": "Return chart-ready labels and full-file totals for every consecutive 100-record block."}}]),
        AIMessage(content="", tool_calls=[{"name": "ui_catalog", "id": "schema",
            "args": {"components": ["Col", "Chart", "Text", "Fact"]}}]),
        chart_call, AIMessage(content=f"All 1,500 rows are in the chart. [Sales source](/api/documents/{doc['id']}/download)")])
    assert run_attempt(tmp_path, space.task, model).status == "completed"
    request = fake.sdk.responses.create.call_args.kwargs
    assert request["tools"][0]["container"]["file_ids"] == ["complete-original"]
    instruction = request["input"][0]["content"][0]["text"]
    assert "entire original files" in instruction and "chart-ready JSON" in instruction
    assert "Do not plot, display, or save a chart image unless" in instruction
    assert "static/downloadable export" in instruction
    # The ordinary HTTP reply carries the native component and verified data, with no image artifact.
    messages = space.api.get(f"/api/channels/{space.channel.id}/messages", headers=space.auth).json()
    attached = next(c for message in messages for c in message["components"] if c["kind"] == "ui")
    assert attached["props"]["ui"] == ui
    assert sum(point["value"] for point in attached["props"]["ui"]["children"][1]["data"]) == sum(values)
    with db() as session:
        assert [d.id for d in session.exec(select(Document).where(Document.channel_id == space.channel.id)).all()] == [doc["id"]]
    assert run_attempt(tmp_path, space.task, ScriptedModel(responses=[])).status == "completed"
    with db() as session:
        assert len(session.exec(select(MessageComponent).where(MessageComponent.task_id == space.task.id)).all()) == 1


def test_native_chart_tool_remains_available_alongside_requested_export(space, monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    import io
    from PIL import Image
    import app.runtime.tools.documents as tools
    from app.runtime.tools.generative import generative_tools
    doc = upload(space, "sales.csv", b"Amount\n5\n8\n")
    publish(space, doc)
    documents.patch(doc["id"], status="ready", file_id="complete-original")
    fake = sdk_mock()
    fake.sdk.responses.create.return_value.output = [NS(content=[NS(annotations=[NS(
        type="container_file_citation", container_id="container", file_id="export", filename="chart.png")])])]
    monkeypatch.setattr(tools, "client", fake.client)
    image = io.BytesIO()
    Image.new("RGB", (100, 60), "white").save(image, format="PNG")
    monkeypatch.setattr(tools, "generated_file", AsyncMock(return_value=("chart.png", "image/png", image.getvalue())))
    analyze = {t.name: t for t in document_tools(space.task)}["analyze_documents"]
    result = asyncio.run(analyze.ainvoke({"document_ids": [doc["id"]], "calculate": True,
        "question": "The human requests a downloadable static PNG export in addition to the native chart."}))
    assert result["downloads"] and space.api.get(result["downloads"][0]["url"], headers=space.auth).content == image.getvalue()
    assert "present_ui" in {t.name for t in generative_tools(space.task)}
