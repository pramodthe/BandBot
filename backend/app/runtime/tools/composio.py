"""One Composio Tool Router session for the workspace, shared by every agent."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sqlmodel import select

from app.config import settings
from app.persistence.models import AgentPluginAccess, ComposioSession, db

logger = logging.getLogger(__name__)

API = "https://backend.composio.dev/api/v3.1"
TOOLKITS = "https://backend.composio.dev/api/v3"
SLUG = re.compile(r"^[a-z0-9_]{1,40}$")
READ_TOOLS = {"COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS"}
# Sign-in stays in Plugins. These would hand the model an auth URL.
HIDDEN_TOOLS = {"COMPOSIO_MANAGE_CONNECTIONS", "COMPOSIO_WAIT_FOR_CONNECTIONS"}
EXECUTE_TOOL = "COMPOSIO_MULTI_EXECUTE_TOOL"

# Upstream discovery guidance says to run at the start of every task. Scope it to external apps here.
SEARCH_DESCRIPTION = (
    "Discover actions for a task in an external connected app, such as Gmail, Google Drive, Slack or GitHub. "
    "Use only when the request needs data or actions in that external service. "
    "Files uploaded or saved in this conversation's Documents library use list_documents, search_documents, "
    "read_document and analyze_documents directly; do not discover app tools for those files, "
    "including when no documents are found. This is not a prerequisite for built-in tools or every new task. "
    "Describe each external-app action in queries.use_case, name the app, and put known identifiers in "
    "queries.known_fields. Reuse the returned session for related app calls. Inspect the returned schemas "
    "before executing actions. Discovery does not grant app access or bypass human approval."
)

CURATED: list[dict[str, Any]] = [
    {"slug": "gmail", "name": "Gmail", "description": "Read and send email"},
    {"slug": "slack", "name": "Slack", "description": "Post updates and read channels"},
    {"slug": "github", "name": "GitHub", "description": "Issues, pull requests, and code"},
    {"slug": "notion", "name": "Notion", "description": "Pages and databases"},
    {"slug": "linear", "name": "Linear", "description": "Issues and project tracking"},
    {"slug": "googlecalendar", "name": "Google Calendar", "description": "Read and create events"},
    {"slug": "googledrive", "name": "Google Drive", "description": "Browse and manage files"},
    {"slug": "jira", "name": "Jira", "description": "Issues and sprints"},
]


class ComposioError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def configured() -> bool:
    return bool(settings.composio_api_key)


def trusted_mcp_url(value: str) -> bool:
    return _trusted(value)


def normalize_slugs(slugs: list[str]) -> list[str]:
    """Lower-case toolkit slugs, in order, without duplicates."""
    if len(slugs) > 64:
        raise ValueError("An agent can list at most 64 plugins")
    chosen: list[str] = []
    for slug in slugs:
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            raise ValueError("Plugin names are lowercase letters, digits, and underscores")
        if slug not in chosen:
            chosen.append(slug)
    return chosen


def plugin_slugs(agent_id: int) -> list[str] | None:
    """None means every connected plugin. A list, even empty, is the exact allow-list."""
    with db() as session:
        row = session.get(AgentPluginAccess, agent_id)
        if row is None:
            return None
        return [slug for slug in row.slugs if isinstance(slug, str)]


def set_plugin_access(agent_id: int, slugs: list[str] | None) -> None:
    """None clears the row so the agent follows whatever Plugins has connected."""
    with db() as session:
        row = session.get(AgentPluginAccess, agent_id)
        if slugs is None:
            if row is not None:
                session.delete(row)
                session.commit()
            return
        clean = normalize_slugs(slugs)
        if row is None:
            session.add(AgentPluginAccess(agent_id=agent_id, slugs=clean))
        else:
            row.slugs = clean
            session.add(row)
        session.commit()


def visible_composio_tool(name: str) -> bool:
    return name.startswith("COMPOSIO_") and name not in HIDDEN_TOOLS


def composio_needs_approval(name: str) -> bool:
    return name not in READ_TOOLS


def service_slug(tool: str, candidates: list[str]) -> str | None:
    """Longest toolkit slug that prefixes the tool, so bland_ai wins over bland."""
    match: str | None = None
    upper = tool.upper()
    for candidate in candidates:
        if upper.startswith(candidate.upper() + "_") and (match is None or len(candidate) > len(match)):
            match = candidate
    if match:
        return match.lower()
    underscore = tool.find("_")
    if underscore <= 0:
        return None
    return tool[:underscore].lower()


def execute_names(arguments: Any) -> list[str] | None:
    """Tool slugs inside a MULTI_EXECUTE call, or None when the shape cannot be read."""
    if not isinstance(arguments, dict):
        return None
    tools = arguments.get("tools")
    if isinstance(tools, list):
        if not tools:
            return None
        names: list[str] = []
        for item in tools:
            slug = item.get("tool_slug") if isinstance(item, dict) else None
            if not isinstance(slug, str) or not slug:
                return None
            names.append(slug)
        return names
    single = arguments.get("tool_slug")
    if isinstance(single, str) and single:
        return [single]
    return None


def call_refusal(agent_id: int, tool_name: str, arguments: Any) -> str | None:
    """Why this Composio call must not run, or None when the agent's plugins allow it."""
    allowed = plugin_slugs(agent_id)
    if allowed is None or tool_name in READ_TOOLS:
        return None
    if tool_name != EXECUTE_TOOL:
        return ("This agent can only run its granted app tools through COMPOSIO_MULTI_EXECUTE_TOOL. "
                "This call was not performed.")
    names = execute_names(arguments)
    if names is None:
        return ("A COMPOSIO_MULTI_EXECUTE_TOOL call arrived in a shape that could not be read, "
                "so it was not performed. Send tools as a list of {tool_slug, arguments}.")
    allowed_set = {slug.lower() for slug in allowed}
    denied = [name for name in names if service_slug(name, allowed) not in allowed_set]
    if not denied:
        return None
    listed = ", ".join(f'"{name}"' for name in denied)
    verb = "is" if len(denied) == 1 else "are"
    return (f"{listed} {verb} not granted to this agent. This call was not performed. "
            "Ask the humans to allow the app for this agent.")


async def mcp_connection() -> dict[str, Any] | None:
    """Streamable-HTTP connection for the session, or None when Composio is not configured."""
    if not configured():
        return None
    session = await ensure_session()
    return {
        "transport": "streamable_http",
        "url": session["mcp_url"],
        "headers": {"x-api-key": settings.composio_api_key},
    }


async def list_plugins(query: str) -> dict[str, Any]:
    needle = query.strip().lower()
    if not configured():
        cards = [dict(card, logo=None, connected=False) for card in CURATED
                 if not needle or needle in card["slug"] or needle in card["name"].lower()]
        return {"configured": False, "plugins": cards}
    connected = {item["slug"]: item for item in await _connected()}
    cards = await _catalog(needle)
    seen = {card["slug"] for card in cards}
    for slug, item in connected.items():
        if slug not in seen and (not needle or needle in slug or needle in item["name"].lower()):
            cards.append(item)
    for card in cards:
        match = connected.get(card["slug"])
        card["connected"] = match is not None
        if match and not card.get("logo"):
            card["logo"] = match.get("logo")
    cards.sort(key=lambda card: (not card["connected"], card["name"].lower()))
    return {"configured": True, "plugins": cards}


async def authorization_link(slug: str) -> str:
    _check_slug(slug)
    session = await ensure_session()
    response = await _send("POST", f"{API}/tool_router/session/{session['session_id']}/link",
                           json_body={"toolkit": slug})
    body = _json(response, "Composio authorization failed")
    url = body.get("redirect_url") if isinstance(body, dict) else None
    if not isinstance(url, str) or not _trusted(url):
        raise ComposioError("Composio returned an untrusted authorization link")
    return url


async def disconnect_plugin(slug: str) -> None:
    _check_slug(slug)
    session = await ensure_session()
    removed = await _delete_accounts(session["user_id"], slug)
    if removed:
        return
    listed = await _send("GET", f"{API}/tool_router/session/{session['session_id']}/toolkits",
                         params={"limit": "50", "toolkits": slug})
    body = _json(listed, "Composio could not list this plugin")
    items = body.get("items") if isinstance(body, dict) else None
    for item in items or []:
        account = item.get("connected_account") if isinstance(item, dict) else None
        account_id = account.get("id") if isinstance(account, dict) else None
        if isinstance(account_id, str) and account_id:
            await _delete_account(account_id)


async def ensure_session() -> dict[str, str]:
    if not configured():
        raise ComposioError("Add COMPOSIO_API_KEY to the server environment", 409)
    row = _stored_session()
    if row is not None:
        existing = await _read_session(row.session_id)
        if existing is not None:
            return existing
        return await _create_session(row.user_id)
    return await _create_session(f"bandslack_{uuid4().hex}")


def _stored_session() -> ComposioSession | None:
    with db() as session:
        return session.exec(select(ComposioSession).order_by(ComposioSession.id)).first()


def _save_session(user_id: str, session_id: str) -> None:
    with db() as session:
        row = session.exec(select(ComposioSession).order_by(ComposioSession.id)).first()
        if row is None:
            session.add(ComposioSession(user_id=user_id, session_id=session_id))
        else:
            row.user_id = user_id
            row.session_id = session_id
            session.add(row)
        session.commit()


async def _read_session(session_id: str) -> dict[str, str] | None:
    response = await _send("GET", f"{API}/tool_router/session/{session_id}")
    if response.status_code == 404:
        return None
    body = _json(response, "Composio session could not be read")
    parsed = _session_body(body)
    if parsed is None:
        return None
    return parsed


async def _create_session(user_id: str) -> dict[str, str]:
    response = await _send("POST", f"{API}/tool_router/session", json_body={
        "user_id": user_id,
        "manage_connections": {
            "enable": True,
            "enable_wait_for_connections": True,
            "enable_connection_removal": True,
        },
    })
    body = _json(response, "Composio rejected this project key")
    parsed = _session_body(body)
    if parsed is None:
        raise ComposioError("Composio returned an untrusted Session MCP URL")
    saved_user = parsed["user_id"] or user_id
    _save_session(saved_user, parsed["session_id"])
    return {**parsed, "user_id": saved_user}


def _session_body(body: Any) -> dict[str, str] | None:
    if not isinstance(body, dict):
        return None
    session_id = body.get("session_id")
    mcp = body.get("mcp") if isinstance(body.get("mcp"), dict) else {}
    url = mcp.get("url")
    if not isinstance(session_id, str) or not session_id or not isinstance(url, str) or not trusted_mcp_url(url):
        return None
    config = body.get("config") if isinstance(body.get("config"), dict) else {}
    user_id = config.get("user_id")
    return {
        "session_id": session_id,
        "mcp_url": url,
        "user_id": user_id if isinstance(user_id, str) else "",
    }


async def _catalog(query: str) -> list[dict[str, Any]]:
    params: dict[str, str] = {"limit": "24", "sort_by": "usage"}
    if query:
        params["search"] = query
    response = await _send("GET", f"{TOOLKITS}/toolkits", params=params)
    body = _json(response, "Composio catalog is unavailable")
    items = body.get("items") if isinstance(body, dict) else None
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        card = _card(item)
        if card and card["slug"] not in seen:
            seen.add(card["slug"])
            cards.append(card)
    return cards


async def _connected() -> list[dict[str, Any]]:
    session = await ensure_session()
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(5):
        params = {"limit": "50", "is_connected": "true"}
        if cursor:
            params["cursor"] = cursor
        response = await _send("GET", f"{API}/tool_router/session/{session['session_id']}/toolkits", params=params)
        body = _json(response, "Composio could not list connected plugins")
        items = body.get("items") if isinstance(body, dict) else None
        for item in items or []:
            card = _card(item)
            if card and card["slug"] not in seen:
                seen.add(card["slug"])
                card["connected"] = True
                cards.append(card)
        next_cursor = body.get("next_cursor") if isinstance(body, dict) else None
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return cards


async def _delete_accounts(user_id: str, slug: str) -> bool:
    response = await _send("GET", f"{API}/connected_accounts", params={
        "limit": "50", "user_ids": user_id, "toolkit_slugs": slug,
    })
    if response.status_code == 403:
        return False
    body = _json(response, "Composio could not list accounts for this plugin")
    items = body.get("items") if isinstance(body, dict) else None
    found = False
    for item in items or []:
        account_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(account_id, str) and account_id:
            found = True
            await _delete_account(account_id)
    return found


async def _delete_account(account_id: str) -> None:
    response = await _send("DELETE", f"{API}/connected_accounts/{account_id}", params={"revoke_on_delete": "true"})
    if response.status_code not in (200, 204, 404):
        _json(response, "Composio could not disconnect this plugin")


def _card(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    raw = item.get("slug") or item.get("key") or ""
    slug = str(raw).strip().lower()
    if not SLUG.fullmatch(slug):
        return None
    name = item.get("name") if isinstance(item.get("name"), str) and item.get("name") else slug
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    description = meta.get("description") if isinstance(meta.get("description"), str) else item.get("description")
    logo = meta.get("logo") if isinstance(meta.get("logo"), str) else item.get("logo")
    return {
        "slug": slug,
        "name": name[:80],
        "description": description.strip()[:160] if isinstance(description, str) else "",
        "logo": logo if isinstance(logo, str) and logo.startswith("https://") else None,
        "connected": False,
    }


def _check_slug(slug: str) -> None:
    if not SLUG.fullmatch(slug):
        raise ComposioError("Unknown plugin", 422)


def _trusted(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "composio.dev" or host.endswith(".composio.dev"))


def _public(text: str) -> str:
    key = settings.composio_api_key
    if key:
        text = text.replace(key, "[key]")
    return " ".join(text.split())[:300] or "Composio request failed"


def _json(response: httpx.Response, fallback: str) -> Any:
    if response.status_code < 400:
        try:
            return response.json()
        except ValueError as exc:
            raise ComposioError(fallback) from exc
    detail = fallback
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        if isinstance(message, dict):
            message = message.get("message")
        if isinstance(message, str) and message.strip():
            detail = message.strip()
    raise ComposioError(_public(detail), 502)


async def _send(method: str, url: str, *, json_body: Any = None, params: dict[str, str] | None = None) -> httpx.Response:
    if not configured():
        raise ComposioError("Add COMPOSIO_API_KEY to the server environment", 409)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            return await client.request(method, url, headers={"x-api-key": settings.composio_api_key},
                                        json=json_body, params=params)
    except httpx.HTTPError as exc:
        logger.warning("Composio request failed (%s)", type(exc).__name__)
        raise ComposioError("Composio is unavailable") from exc
