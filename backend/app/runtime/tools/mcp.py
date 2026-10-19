"""MCP integrations from a JSON config. Tools not marked read-only pause for human approval."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.types import interrupt

from app.config import settings
from app.runtime.tools.composio import (SEARCH_DESCRIPTION, call_refusal, composio_needs_approval, mcp_connection,
                                        visible_composio_tool)

logger = logging.getLogger(__name__)

APPROVAL_MODES = ("writes", "always", "never")
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any, missing: set[str]) -> Any:
    """Fill ${VAR} references from the environment, so secrets never live in the config file."""
    if isinstance(value, str):
        def substitute(match: re.Match) -> str:
            if match.group(1) not in os.environ:
                missing.add(match.group(1))
                return ""
            return os.environ[match.group(1)]
        return _VARIABLE.sub(substitute, value)
    if isinstance(value, list):
        return [_expand(item, missing) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, missing) for key, item in value.items()}
    return value


def load_servers(path: str | Path | None = None) -> dict[str, dict]:
    """Configured servers keyed by name. Servers needing unset variables are skipped with a warning."""
    path = Path(path or settings.mcp_config_path)
    if not path.is_file():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("servers", {})
        assert isinstance(entries, dict)
    except (OSError, ValueError, AttributeError, AssertionError):
        logger.warning("Ignoring unreadable MCP config %s", path)
        return {}
    servers = {}
    for name, entry in entries.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) or not isinstance(entry, dict):
            logger.warning("Skipping MCP server %r: use a short name of letters, digits, _ or -", name)
            continue
        missing: set[str] = set()
        entry = _expand(entry, missing)
        if missing:
            logger.warning("Skipping MCP server %s: set %s", name, ", ".join(sorted(missing)))
            continue
        approval = entry.pop("approval", "writes")
        agents = entry.pop("agents", None)
        if approval not in APPROVAL_MODES:
            logger.warning("MCP server %s has unknown approval %r; requiring approval for every tool", name, approval)
            approval = "always"
        servers[name] = {"connection": entry, "approval": approval, "agents": agents}
    return servers


def needs_approval(tool: BaseTool, mode: str) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    # MCP annotations are hints from the server; anything not declared read-only is treated as a write.
    return not (tool.metadata or {}).get("readOnlyHint", False)


def _wrap(server: str, tool: BaseTool, approval: bool, agent_id: int | None = None) -> BaseTool:
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}__{tool.name}")[:64]
    upstream = tool.name

    async def call(**arguments):
        if server == "composio" and agent_id is not None:
            refusal = call_refusal(agent_id, upstream, arguments)
            if refusal:
                return (refusal, None)
        if approval:
            decision = interrupt({"action": f"Use the {server} integration: {tool.name}",
                                  "details": json.dumps(arguments, default=str)[:4000]})
            if not decision.get("approved"):
                who = decision.get("decided_by") or "The humans"
                return (f"{who} denied this {server} action. Do not retry it; tell the humans what you did instead.",
                        None)
        return await tool.coroutine(**arguments)

    label = f"[{server} integration{'; pauses for human approval' if approval else ''}] "
    description = (SEARCH_DESCRIPTION if server == "composio" and upstream == "COMPOSIO_SEARCH_TOOLS"
                   else tool.description or "")
    return StructuredTool(name=name, description=label + description, args_schema=tool.args_schema,
                          coroutine=call, response_format="content_and_artifact", metadata=tool.metadata,
                          handle_tool_error=tool.handle_tool_error)


async def integration_tools(agent_name: str, servers: dict[str, dict] | None = None,
                            agent_id: int | None = None) -> list[BaseTool]:
    """Tools for this agent from every reachable server. Unreachable servers are skipped and logged."""
    own = servers is None
    servers = load_servers() if own else dict(servers)
    if own and settings.composio_api_key:
        # The workspace session replaces a hand-written composio server, so the key is not mounted twice.
        servers = {name: server for name, server in servers.items() if name != "composio"}
        try:
            connection = await mcp_connection()
        except Exception as exc:
            logger.warning("Composio is unavailable (%s)", type(exc).__name__)
            connection = None
        if connection:
            servers["composio"] = {"connection": connection, "approval": "writes", "agents": None}
    allowed = {name: s for name, s in servers.items() if s["agents"] is None or agent_name in s["agents"]}
    if not allowed:
        return []
    client = MultiServerMCPClient({name: s["connection"] for name, s in allowed.items()})
    tools: list[BaseTool] = []
    for name, server in allowed.items():
        try:
            loaded = await asyncio.wait_for(client.get_tools(server_name=name), timeout=30)
        except Exception as exc:
            # Connection errors can echo headers; log only the type.
            logger.warning("MCP server %s is unavailable (%s)", name, type(exc).__name__)
            continue
        if name == "composio":
            loaded = [tool for tool in loaded if visible_composio_tool(tool.name)]
        for tool in loaded:
            approval = composio_needs_approval(tool.name) if name == "composio" else needs_approval(tool, server["approval"])
            tools.append(_wrap(name, tool, approval, agent_id if name == "composio" else None))
    return tools
