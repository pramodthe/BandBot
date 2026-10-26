from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app import openclaw_gateway as gateway

URL = "https://gateway.example.test"
TOKEN = "offline-gateway-token"
PAYLOAD = {"messages": [{"type": "human", "content": "Hello"}], "tools": []}


@pytest.fixture
def approved(monkeypatch):
    monkeypatch.setattr(gateway, "settings", replace(gateway.settings, openclaw_gateway_urls=URL))


def policy(deny=None):
    return {"ok": True, "result": {"details": {"ok": True, "result": {"config": [
        {"id": "band", "model": "openai/gpt-test", "tools": {"deny": ["*"] if deny is None else deny}}
    ]}}}}


def mock_gateway(monkeypatch, handle):
    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(gateway.httpx, "AsyncClient", client)


def test_gateway_validation_is_read_only_and_requires_deny_all(approved, monkeypatch):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        assert request.url.path == "/tools/invoke"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json=policy())

    mock_gateway(monkeypatch, handle)
    assert asyncio.run(gateway.validate_gateway(URL, TOKEN, "band")) == ["openai/gpt-test"]
    assert seen == [{"tool": "gateway", "args": {"action": "config.get", "path": "agents.list"}}]


@pytest.mark.parametrize("denied", [[], ["exec"], ["gateway"]])
def test_unrestricted_agent_is_refused_before_inference(approved, monkeypatch, denied):
    def handle(request):
        assert request.url.path == "/tools/invoke"
        return httpx.Response(200, json=policy(denied))

    mock_gateway(monkeypatch, handle)
    with pytest.raises(ValueError, match="deny all"):
        asyncio.run(gateway.run_gateway_step({"gateway_url": URL, "api_key": TOKEN, "agent_id": "band"},
                                            "openai/gpt-test", PAYLOAD))


def test_gateway_steps_recheck_policy_pin_model_and_isolate_sessions(approved, monkeypatch):
    seen = []
    sessions = []

    def handle(request):
        seen.append(request.url.path)
        if request.url.path == "/tools/invoke":
            return httpx.Response(200, json=policy())
        body = json.loads(request.content)
        assert body["model"] == "openclaw/band"
        assert body["tools"] == [] and body["tool_choice"] == "none"
        assert request.headers["x-openclaw-model"] == "openai/pinned-model"
        sessions.append(request.headers["x-openclaw-session-key"])
        assert TOKEN not in json.dumps(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "content": "Hello", "tool_calls": []})}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    mock_gateway(monkeypatch, handle)
    secret = {"gateway_url": URL, "api_key": TOKEN, "agent_id": "band"}
    for _ in range(2):
        result = asyncio.run(gateway.run_gateway_step(secret, "openai/pinned-model", PAYLOAD))
        assert result["usage"]["input_tokens"] == 12 and result["decision"]["content"] == "Hello"
    assert seen == ["/tools/invoke", "/v1/chat/completions"] * 2
    assert len(set(sessions)) == 2 and all(key.startswith("agent:band:band:") for key in sessions)


@pytest.mark.parametrize("url", ["https://evil.test", "https://gateway.example.test.evil.test", "https://user:pass@gateway.example.test",
    "https://gateway.example.test/path", "https://gateway.example.test?token=secret", "http://169.254.169.254"])
def test_gateway_url_must_exactly_match_operator_configuration(approved, url):
    with pytest.raises(ValueError, match="approved"):
        gateway.approved_url(url)


def test_plain_http_is_only_accepted_for_explicit_loopback_configuration(monkeypatch):
    monkeypatch.setattr(gateway, "settings", replace(gateway.settings,
        openclaw_gateway_urls="http://127.0.0.1:18789, http://public.test, http://169.254.169.254, https://gateway.example.test/"))
    assert gateway.gateway_urls() == ["http://127.0.0.1:18789", "https://gateway.example.test"]


def test_gateway_redirect_is_never_followed(approved, monkeypatch):
    def handle(request):
        assert request.url.host == "gateway.example.test"
        return httpx.Response(302, headers={"location": "https://evil.test"})

    mock_gateway(monkeypatch, handle)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(gateway.validate_gateway(URL, TOKEN, "band"))
