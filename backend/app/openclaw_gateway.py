"""A bounded, operator-approved OpenClaw bridge into the app's checkpointed tool loop.

Never start an unrestricted remote agent: verify its deny-all native tool policy
before each inference call. Structured text returns a decision to our worker.
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.config import settings
from app.runtime.connected_runner import decision_schema, request_content

AGENT_ID = re.compile(r"[a-z0-9_-]{1,80}")


def gateway_urls() -> list[str]:
    urls = []
    for item in settings.openclaw_gateway_urls.split(","):
        url = item.strip().rstrip("/")
        parts = urlsplit(url)
        try:
            loopback = ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        if (parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password
                and not parts.query and not parts.fragment and parts.path == ""
                and (parts.scheme == "https" or loopback)):
            urls.append(url)
    return sorted(set(urls))


def approved_url(url: str) -> str:
    url = url.rstrip("/")
    if url not in gateway_urls():
        raise ValueError("Gateway is not approved by the server operator")
    return url


async def request_json(client: httpx.AsyncClient, url: str, token: str, body: dict, *, headers=None) -> dict:
    # Do not follow redirects, use ambient proxies, retry uncertain calls, or buffer unbounded responses.
    async with client.stream("POST", url, headers={"Authorization": f"Bearer {token}", **(headers or {})}, json=body) as response:
        response.raise_for_status()
        data = bytearray()
        async for part in response.aiter_bytes():
            data.extend(part)
            if len(data) > 2_000_000:
                raise ValueError("Gateway response exceeds limit")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Invalid Gateway response")
        return result


async def verify_policy(client: httpx.AsyncClient, url: str, token: str, agent_id: str) -> str:
    if not AGENT_ID.fullmatch(agent_id):
        raise ValueError("Invalid Gateway agent ID")
    # Read only the agent subtree, never the gateway's provider keys or full config.
    body = await request_json(client, url + "/tools/invoke", token,
        {"tool": "gateway", "args": {"action": "config.get", "path": "agents.list"}})
    result = body.get("result", {})
    details = result.get("details", {})
    if not body.get("ok") or result.get("isError") or not details.get("ok"):
        raise ValueError("Gateway policy cannot be verified")
    agents = details.get("result", {}).get("config")
    if not isinstance(agents, list):
        raise ValueError("Gateway did not return its agent policy")
    matches = [entry for entry in agents if isinstance(entry, dict) and entry.get("id") == agent_id]
    if len(matches) != 1 or "*" not in matches[0].get("tools", {}).get("deny", []):
        raise ValueError("Dedicated Gateway agent must deny all native tools")
    model = matches[0].get("model")
    if isinstance(model, dict):
        model = model.get("primary")
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,120}", model):
        raise ValueError("Dedicated Gateway agent must specify a model")
    return model


async def validate_gateway(url: str, token: str, agent_id: str) -> list[str]:
    url = approved_url(url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        return [await verify_policy(client, url, token, agent_id)]


async def run_gateway_step(secret: dict, model: str, payload: dict) -> dict:
    url = approved_url(secret["gateway_url"])
    token, agent_id = secret["api_key"], secret["agent_id"]
    prompt, images = request_content(payload)
    content = [{"type": "text", "text": prompt}]
    for image in images:
        image_url = image.get("image_url")
        if not image_url and image.get("base64"):
            image_url = {"url": f"data:{image.get('mime_type', 'image/png')};base64,{image['base64']}"}
        if image_url:
            content.append({"type": "image_url", "image_url": {"url": image_url} if isinstance(image_url, str) else image_url})
    schema = decision_schema([tool["name"] for tool in payload["tools"]])
    content[0]["text"] += "\nReturn only a JSON object matching this schema, without markdown:\n" + json.dumps(schema)
    body = {"model": f"openclaw/{agent_id}", "stream": False,
        "messages": [{"role": "user", "content": content}], "tools": [], "tool_choice": "none"}
    async with httpx.AsyncClient(timeout=180, follow_redirects=False, trust_env=False) as client:
        await verify_policy(client, url, token, agent_id)
        result = await request_json(client, url + "/v1/chat/completions", token, body, headers={
            "x-openclaw-model": model,
            # Each model step is isolated; the app checkpoint supplies all history and scoped memory.
            "x-openclaw-session-key": f"agent:{agent_id}:band:{uuid4().hex}"})
    message = result["choices"][0]["message"]
    if message.get("tool_calls"):
        raise ValueError("Gateway returned an unexpected native tool call")
    raw = result.get("usage") or {}
    cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return {"decision": json.loads(message["content"]), "usage": {
        "input_tokens": raw.get("prompt_tokens", 0), "output_tokens": raw.get("completion_tokens", 0),
        "cache_read_tokens": cached}}
