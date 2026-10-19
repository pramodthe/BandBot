from __future__ import annotations

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlmodel import select

from app.auth import issue_token
from app.main import app
from app.persistence.models import AgentTask, Message, MessageComponent, db
from app.runtime.tools import tools_for
from test_tasks import RecordingModel, ScriptedModel, job, run_attempt  # noqa: F401  (job is a fixture)

TABLE_ARGS = {
    "caption": "Laptops compared",
    "columns": [
        {"key": "model", "label": "Model"},
        {"key": "price", "label": "Price", "format": {"kind": "currency", "currency": "USD"}},
        {"key": "in_stock", "label": "In stock", "format": {"kind": "boolean", "true_label": "Yes"}},
    ],
    "rows": [{"model": "Air", "price": 999, "in_stock": True}, {"model": "Pro", "price": 1999, "in_stock": False}],
}


def call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def components(task_id: str) -> list[MessageComponent]:
    with db() as session:
        return session.exec(select(MessageComponent).where(MessageComponent.task_id == task_id)
                            .order_by(MessageComponent.id)).all()


def test_components_attach_to_the_reply_and_reach_the_messages_api(tmp_path, job):
    task, _, user = job
    model = ScriptedModel(responses=[
        call("show_table", TABLE_ARGS, "table-1"),
        call("show_chart", {"label": "Price", "value": "$999", "points": [1099, 1049, 999], "delta": "-9%",
                            "up_is_good": False}, "chart-1"),
        call("show_spec", {"title": "Air", "rows": [{"label": "Weight", "value": "1.2 kg", "emphasis": True}]},
             "spec-1"),
        AIMessage(content="The Air is $999, $1,000 less than the Pro."),
    ])
    finished = run_attempt(tmp_path, task, model)
    assert finished.status == "completed"
    attached = components(task.id)
    assert [c.kind for c in attached] == ["table", "chart", "spec"]
    assert {c.message_id for c in attached} == {finished.reply_message_id}
    with TestClient(app) as client:
        messages = client.get(f"/api/channels/{task.channel_id}/messages",
                              headers={"Authorization": f"Bearer {issue_token(user.id)}"}).json()
    reply = next(m for m in messages if m["id"] == finished.reply_message_id)
    table, chart, spec = reply["components"]
    assert table["props"]["columns"][1]["format"] == {"kind": "currency", "currency": "USD"}
    assert table["props"]["columns"][2]["format"] == {"kind": "boolean", "trueLabel": "Yes"}
    assert table["props"]["rows"][0] == {"model": "Air", "price": 999, "in_stock": True}
    assert chart["props"] == {"label": "Price", "value": "$999", "points": [1099, 1049, 999], "delta": "-9%",
                              "upIsGood": False, "variant": "area", "visibleCount": 3}
    assert spec["props"]["rows"] == [{"label": "Weight", "value": "1.2 kg", "emphasis": True}]
    assert spec["props"]["visibleCount"] == 1


def test_replayed_tool_call_attaches_once(job):
    task, _, _ = job
    show_table = {t.name: t for t in tools_for(task)}["show_table"]
    for _ in range(2):
        result = show_table.invoke({"name": "show_table", "args": TABLE_ARGS, "id": "same-call", "type": "tool_call"})
        assert "Shown under your reply" in result.content
    assert len(components(task.id)) == 1


def test_invalid_components_return_errors_the_agent_can_fix(tmp_path, job):
    task, _, _ = job
    too_many = {**TABLE_ARGS, "rows": [{"model": str(i)} for i in range(101)]}
    bad_format = {**TABLE_ARGS, "columns": [{"key": "model", "label": "Model", "format": {"kind": "sparkline"}}]}
    no_currency = {**TABLE_ARGS, "columns": [{"key": "price", "label": "Price", "format": {"kind": "currency"}}]}
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "show_table", "args": args, "id": f"bad-{i}"}
                                          for i, args in enumerate((too_many, bad_format, no_currency))]),
        AIMessage(content="Here is the comparison in words instead."),
    ])
    finished = run_attempt(tmp_path, task, model)
    assert finished.status == "completed"
    errors = [m.content for m in model.seen[1] if getattr(m, "type", "") == "tool"]
    assert len(errors) == 3 and all("error" in e.lower() for e in errors)
    assert "ISO currency code" in errors[2]
    assert components(task.id) == []


def test_failed_task_leaves_its_components_unshown(tmp_path, job):
    task, _, _ = job
    model = ScriptedModel(responses=[call("show_table", TABLE_ARGS, "table-1"), RuntimeError("provider down")])
    finished = run_attempt(tmp_path, task, model)
    assert finished.status == "failed"
    (attached,) = components(task.id)
    assert attached.message_id is None
    with db() as session:
        assert session.exec(select(Message).where(Message.band_message_id == f"task:{task.id}")).first() is None
        assert session.get(AgentTask, task.id).reply_message_id is None
