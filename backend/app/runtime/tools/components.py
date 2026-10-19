"""Attach a data table, chart, or spec sheet to the reply; the app draws it under the text."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.tools import InjectedToolCallId, tool
from pydantic import BaseModel, Field, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy.exc import IntegrityError

from app.persistence.models import AgentTask, MessageComponent, db

SHOWN = ("Shown under your reply in the app. Band and other clients only see your text, so state the "
         "conclusion and its key figures in words, without repeating every row.")

Cell = str | int | float | bool | None
Number = Annotated[float, Field(allow_inf_nan=False)]


class ColumnFormat(BaseModel):
    kind: Literal["text", "number", "currency", "percent", "delta", "date", "boolean", "badge", "link"]
    decimals: int | None = Field(None, ge=0, le=6)
    compact: bool | None = Field(None, description="number/currency: 1.2K style")
    unit: str | None = Field(None, max_length=12, description="number/delta: suffix such as ms or %")
    currency: str | None = Field(None, pattern=r"^[A-Z]{3}$", description="currency: ISO code, required")
    basis: Literal["fraction", "unit"] | None = Field(None, description="percent: 0.25 (fraction) or 25 (unit)")
    up_is_good: bool | None = Field(None, description="delta: whether a rise is good news")
    style: Literal["date", "datetime", "relative"] | None = Field(None, description="date: ISO date strings")
    true_label: str | None = Field(None, max_length=20)
    false_label: str | None = Field(None, max_length=20)
    href_key: str | None = Field(None, description="link: row key holding the URL; default is the cell itself")
    tones: dict[str, Literal["neutral", "success", "warning", "danger"]] | None = Field(
        None, description="badge: cell value -> tone")

    @model_validator(mode="after")
    def _currency_code(self):
        if self.kind == "currency" and not self.currency:
            raise ValueError("currency format needs an ISO currency code such as USD")
        return self


class TableColumn(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=60)
    align: Literal["start", "end"] | None = None
    format: ColumnFormat | None = None


class ShowTable(BaseModel):
    caption: str = Field(max_length=200, description="What the table shows, for screen readers")
    columns: list[TableColumn] = Field(min_length=1, max_length=12)
    rows: list[dict[str, Cell]] = Field(min_length=1, max_length=100,
                                        description="One object per row, keyed by column key")
    tool_call_id: Annotated[str, InjectedToolCallId]

    @model_validator(mode="after")
    def _unique_keys(self):
        if len({c.key for c in self.columns}) != len(self.columns):
            raise ValueError("column keys must be unique")
        return self


class ShowChart(BaseModel):
    label: str = Field(min_length=1, max_length=60, description="Metric name, e.g. Weekly revenue")
    value: str = Field(min_length=1, max_length=40, description="Current value, formatted, e.g. $48.2k")
    points: list[Number] = Field(min_length=2, max_length=200, description="Series in time order, oldest first")
    delta: str | None = Field(None, max_length=20, description="Change, formatted, e.g. +12% or -3")
    trend: Literal["up", "down", "flat"] | None = None
    up_is_good: bool = True
    variant: Literal["area", "line", "bars"] = "area"
    tool_call_id: Annotated[str, InjectedToolCallId]


class SpecRow(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=200)
    emphasis: bool = False


class ShowSpec(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    subtitle: str | None = Field(None, max_length=120)
    rows: list[SpecRow] = Field(min_length=1, max_length=30)
    tool_call_id: Annotated[str, InjectedToolCallId]

    @model_validator(mode="after")
    def _unique_labels(self):
        if len({r.label for r in self.rows}) != len(self.rows):
            raise ValueError("row labels must be unique")
        return self


def _props(model: BaseModel) -> Any:
    """Component props in the frontend's camelCase, without unset fields."""
    data = model.model_dump(exclude_none=True, exclude={"tool_call_id"})

    def camel(value):
        if isinstance(value, dict):
            return {to_camel(k): v if k == "tones" else camel(v) for k, v in value.items()}
        if isinstance(value, list):
            return [camel(v) for v in value]
        return value

    rows = data.pop("rows", None)
    props = camel(data)
    if rows is not None:
        # Table cells are keyed by the agent's own column keys, which stay as written.
        props["rows"] = rows if isinstance(model, ShowTable) else camel(rows)
    return props


def attach(task: AgentTask, call_id: str, kind: str, props: dict) -> dict:
    """Replay-safe: the same tool call attaches its component once."""
    with db() as session:
        session.add(MessageComponent(task_id=task.id, tool_call_id=call_id, kind=kind, props=props))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
    return {"status": "shown", "note": SHOWN}


def component_tools(task: AgentTask):
    @tool(args_schema=ShowTable)
    def show_table(caption: str, columns: list[TableColumn], rows: list[dict[str, Cell]],
                   tool_call_id: str) -> dict:
        """Attach a sortable table to your reply, for comparisons across several items and attributes.

        Give each column a key, a label, and optionally a format (number, currency, percent, delta, date,
        boolean, badge, link) so values stay raw numbers and sort correctly. Not for two or three facts.
        """
        args = ShowTable(caption=caption, columns=columns, rows=rows, tool_call_id=tool_call_id)
        return attach(task, tool_call_id, "table", _props(args))

    @tool(args_schema=ShowChart)
    def show_chart(label: str, value: str, points: list[float], tool_call_id: str, delta: str | None = None,
                   trend: str | None = None, up_is_good: bool = True, variant: str = "area") -> dict:
        """Attach a compact metric sparkline: its current value, change, and series.

        For a requested data graph with labelled axes, categories, or multiple series, use ui_catalog
        and present_ui with the native Chart component instead.
        """
        args = ShowChart(label=label, value=value, points=points, delta=delta, trend=trend,
                         up_is_good=up_is_good, variant=variant, tool_call_id=tool_call_id)
        props = _props(args)
        props["visibleCount"] = len(args.points)
        return attach(task, tool_call_id, "chart", props)

    @tool(args_schema=ShowSpec)
    def show_spec(title: str, rows: list[SpecRow], tool_call_id: str, subtitle: str | None = None) -> dict:
        """Attach a spec sheet to your reply: one item's attributes as labeled rows (emphasis marks key ones)."""
        args = ShowSpec(title=title, subtitle=subtitle, rows=rows, tool_call_id=tool_call_id)
        props = _props(args)
        props["visibleCount"] = len(args.rows)
        return attach(task, tool_call_id, "spec", props)

    return [show_table, show_chart, show_spec]
