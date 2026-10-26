from __future__ import annotations

import asyncio
from types import SimpleNamespace

import cursor_sdk
import pytest

from app.runtime import connected_runner as runner


@pytest.mark.parametrize("operation", ["catalog", "inference"])
def test_cursor_uses_isolated_sdk_with_no_tools_or_settings(monkeypatch, operation):
    seen = []
    key = "offline-cursor-key"
    monkeypatch.setenv("BAND_RUNTIME_TOKEN", key)
    monkeypatch.setattr(runner, "cursor_command", lambda: ["offline-bridge"])

    class Agent:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            seen.append("agent_closed")

        async def send(self, message):
            assert "offline-pixels" not in message.text
            assert message.images[0].data == "offline-pixels"
            assert "arguments_json" in message.text
            seen.append("send")
            return self

        async def wait(self):
            return SimpleNamespace(status="finished", result='{"content":"Ready","tool_calls":[]}',
                usage=SimpleNamespace(input_tokens=10, output_tokens=2, cache_read_tokens=3, cache_write_tokens=1))

    class Client:
        agents = None

        def __init__(self):
            self.agents = self

        @classmethod
        async def launch_bridge(cls, **kwargs):
            assert kwargs["max_retries"] == 0 and kwargs["allow_api_key_env_fallback"] is False
            assert kwargs["local"].setting_sources == []
            seen.append("launch")
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            seen.append("client_closed")

        async def me(self, *, api_key):
            assert api_key == key
            seen.append("me")

        async def list_models(self, *, api_key):
            assert api_key == key
            return [SimpleNamespace(id="composer-test")]

        async def create(self, options):
            assert isinstance(options, cursor_sdk.AgentOptions)
            assert options.tools == [] and options.mcp_servers == {} and options.agents == {}
            assert options.api_key == key and options.local.setting_sources == []
            assert options.to_json()["tools"] == {"names": []}
            return Agent()

    monkeypatch.setattr(cursor_sdk, "AsyncClient", Client)
    payload = {"operation": operation, "messages": [{"type": "human", "content": [{"type": "image_url",
        "image_url": {"url": "data:image/png;base64,offline-pixels"}}]}], "tools": []}
    result = asyncio.run(runner.cursor(payload, "composer-test"))
    if operation == "catalog":
        assert result == {"models": ["composer-test"]} and "send" not in seen
    else:
        assert result["decision"]["content"] == "Ready" and result["usage"]["input_tokens"] == 14
        assert "agent_closed" in seen
    assert seen[-1] == "client_closed"
