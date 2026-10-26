"""Token usage per model call and daily token budgets (UTC days) for agents and channels."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import settings
from app.persistence.models import (AgentProfile, AgentTask, Budget, Channel, ChannelMember, DocumentUsage, TaskCacheUsage, TaskUsage,
                                    db, utcnow)

logger = logging.getLogger(__name__)


def _prices() -> dict[str, dict[str, float]]:
    if not settings.model_prices:
        return {}
    try:
        prices = {}
        for model, rate in json.loads(settings.model_prices).items():
            given = float(rate.get("input", 0))
            prices[model] = {"input": given, "output": float(rate.get("output", 0)),
                             "cache_read": float(rate.get("cache_read", given)),
                             "cache_write": float(rate.get("cache_write", given))}
        return prices
    except (ValueError, AttributeError, TypeError):
        logger.warning("MODEL_PRICES is not valid JSON of per-model rates; costs are not shown")
        return {}


PRICES = _prices()


def rate_for(model: str) -> dict[str, float] | None:
    """The configured rate for a model ID; dated IDs can use an explicitly configured alias's rate."""
    if model in PRICES:
        return PRICES[model]
    aliases = [name for name in PRICES if model.startswith(name)]
    return PRICES[max(aliases, key=len)] if aliases else None


def cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float | None:
    """Estimated USD from the configured rates, or None when the model has no rate.

    `input_tokens` includes the cache reads and writes, which are charged at their own rates.
    """
    rate = rate_for(model)
    if rate is None:
        return None
    uncached = max(input_tokens - cache_read - cache_write, 0)
    total = (uncached * rate["input"] + cache_read * rate["cache_read"] + cache_write * rate["cache_write"]
             + output_tokens * rate["output"])
    return round(total / 1_000_000, 4)


def record_usage(task: AgentTask, message, default_model: str, *, fallback_id: str | None = None) -> None:
    """Store one model response's token counts; the message id makes replays count once."""
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return
    details = usage.get("input_token_details") or {}
    cache_read, cache_write = int(details.get("cache_read") or 0), int(details.get("cache_creation") or 0)
    from app.providers.context import usage_selection
    explicit = usage_selection()
    model = (getattr(message, "response_metadata", None) or {}).get("model_name") or (explicit[0].get("model") if explicit else None) or default_model
    with db() as session:
        row = TaskUsage(task_id=task.id, channel_id=task.channel_id, agent_id=task.agent_id,
                        message_id=getattr(message, "id", None) or fallback_id or f"anonymous-{uuid4().hex}",
                        model=model, input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0))
        session.add(row)
        try:
            session.flush()
            from app.providers.bindings import bindings_for
            from app.providers.models import ProviderUsage
            binding = explicit[0] if explicit else bindings_for(task).get("llm", {})
            if binding.get("revision") and binding.get("provider") != "openai":
                row.model = binding["provider"] + ":" + model
                session.add(row)
            session.merge(ProviderUsage(usage_id=row.id, provider=binding.get("provider", "openai"),
                                     connection_id=binding.get("connection_id", ""), revision=binding.get("revision", 0), service=explicit[1] if explicit else "llm"))
            if cache_read or cache_write:
                session.add(TaskCacheUsage(usage_id=row.id, cache_read_tokens=cache_read,
                                           cache_write_tokens=cache_write))
            session.commit()
        except IntegrityError:
            session.rollback()


class BudgetExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class UsageRecorder(AsyncCallbackHandler):
    """Counts every model call in a task, including helpers and summarization, and refuses calls past a budget.

    Callbacks reach every model call made under the task's run config, unlike the stream, which only shows the
    messages that end up in the graph state.
    """
    raise_error = True

    def __init__(self, task: AgentTask, default_model: str):
        self.task = task
        self.default_model = default_model

    async def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs: Any) -> None:
        with db() as session:
            reason = over_budget(session, self.task.agent_id, self.task.channel_id)
        if reason:
            raise BudgetExceeded(reason)

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                if (message := getattr(generation, "message", None)) is not None:
                    record_usage(self.task, message, self.default_model, fallback_id=f"run-{run_id}")


def day_start(now: datetime | None = None) -> datetime:
    return (now or utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)


def tokens_today(session: Session, *, agent_id: int | None = None, channel_id: int | None = None) -> int:
    query = select(func.coalesce(func.sum(TaskUsage.input_tokens + TaskUsage.output_tokens), 0)).where(
        TaskUsage.at >= day_start())
    if agent_id is not None:
        query = query.where(TaskUsage.agent_id == agent_id)
    if channel_id is not None:
        query = query.where(TaskUsage.channel_id == channel_id)
    total = int(session.exec(query).one())
    if channel_id is not None and agent_id is None:
        total += int(session.exec(select(func.coalesce(func.sum(DocumentUsage.input_tokens + DocumentUsage.output_tokens), 0))
                                  .where(DocumentUsage.channel_id == channel_id, DocumentUsage.at >= day_start())).one())
    return total


def over_budget(session: Session, agent_id: int, channel_id: int) -> str | None:
    """Why this agent can't work in this channel today, or None while both budgets have room."""
    for scope, scope_id in (("channel", channel_id), ("agent", agent_id)):
        budget = session.get(Budget, (scope, scope_id))
        if budget is None or tokens_today(session, **{f"{scope}_id": scope_id}) < budget.daily_tokens:
            continue
        if scope == "channel":
            channel = session.get(Channel, channel_id)
            owner = f"#{channel.name}" if channel else "This channel"
        else:
            agent = session.get(AgentProfile, agent_id)
            owner = agent.name if agent else "This agent"
        return f"{owner} used its daily budget of {budget.daily_tokens:,} tokens"
    return None


def _sums(key, *conditions):
    """Token sums per (key, model), with the cache share of the input tokens."""
    return (select(key, TaskUsage.model, func.sum(TaskUsage.input_tokens), func.sum(TaskUsage.output_tokens),
                   func.coalesce(func.sum(TaskCacheUsage.cache_read_tokens), 0),
                   func.coalesce(func.sum(TaskCacheUsage.cache_write_tokens), 0))
            .outerjoin(TaskCacheUsage, TaskCacheUsage.usage_id == TaskUsage.id)
            .where(*conditions).group_by(key, TaskUsage.model))


def task_usage(session: Session, task_ids: list[str]) -> dict[str, dict]:
    """Tokens (and estimated cost, when rates are configured) per task."""
    totals: dict[str, dict] = {}
    for task_id, model, *tokens in session.exec(_sums(TaskUsage.task_id, TaskUsage.task_id.in_(task_ids))).all():
        input_tokens, output_tokens, cache_read, cache_write = map(int, tokens)
        total = totals.setdefault(task_id, {"input_tokens": 0, "output_tokens": 0, "cost": None})
        total["input_tokens"] += input_tokens
        total["output_tokens"] += output_tokens
        if (price := cost(model, input_tokens, output_tokens, cache_read, cache_write)) is not None:
            total["cost"] = round((total["cost"] or 0) + price, 4)
    return totals


def channel_usage(session: Session, channel_id: int) -> dict:
    """Today's usage in a channel and per agent, with the budgets that apply."""
    since = day_start()
    rows = session.exec(_sums(TaskUsage.agent_id, TaskUsage.channel_id == channel_id, TaskUsage.at >= since)).all()
    members = session.exec(select(ChannelMember.member_id).where(ChannelMember.channel_id == channel_id,
                                                                 ChannelMember.member_type == "agent")).all()
    agents = {agent_id: {"agent_id": agent_id, "tokens": 0, "cost": None} for agent_id in members}
    for agent_id, model, *tokens in rows:
        input_tokens, output_tokens, cache_read, cache_write = map(int, tokens)
        entry = agents.setdefault(agent_id, {"agent_id": agent_id, "tokens": 0, "cost": None})
        entry["tokens"] += input_tokens + output_tokens
        if (price := cost(model, input_tokens, output_tokens, cache_read, cache_write)) is not None:
            entry["cost"] = round((entry["cost"] or 0) + price, 4)
    for entry in agents.values():
        budget = session.get(Budget, ("agent", entry["agent_id"]))
        entry["agent_tokens_today"] = tokens_today(session, agent_id=entry["agent_id"])
        entry["agent_budget"] = budget.daily_tokens if budget else None
    channel_budget = session.get(Budget, ("channel", channel_id))
    costs = [a["cost"] for a in agents.values() if a["cost"] is not None]
    document_rows = session.exec(select(DocumentUsage).where(DocumentUsage.channel_id == channel_id,
                                                             DocumentUsage.at >= since)).all()
    document_costs = [cost(row.model, row.input_tokens, row.output_tokens) for row in document_rows]
    known_document_costs = [value for value in document_costs if value is not None]
    costs.extend(known_document_costs)
    return {"since": since.isoformat(), "tokens": tokens_today(session, channel_id=channel_id),
            "cost": round(sum(costs), 4) if costs and len(known_document_costs) == len(document_costs) else None,
            "budget": channel_budget.daily_tokens if channel_budget else None,
            "agents": sorted(agents.values(), key=lambda a: a["tokens"], reverse=True),
            "prices_configured": bool(PRICES)}
