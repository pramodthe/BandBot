"""Optional real-CLI contract check against a loopback-only fake inference server."""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.runtime import connected_runner as runner


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI is not installed")
def test_real_codex_runtime_uses_no_native_execution_tools(tmp_path, monkeypatch):
    requests = []
    decision = json.dumps({"content": "Offline runtime verified", "tool_calls": []})
    item = {"type": "message", "id": "msg_offline", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": decision, "annotations": []}]}

    class FakeInference(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(body)
            events = [
                {"type": "response.created", "response": {"id": "resp_offline", "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "resp_offline", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40,
                              "input_tokens_details": {"cached_tokens": 0}}}},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeInference)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.chdir(tmp_path)
    for name in ("HOME", "USERPROFILE", "CODEX_HOME"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("BAND_RUNTIME_TOKEN", "offline-test-token")
    monkeypatch.setitem(runner.CODEX_SETTINGS, "model_providers.band_runtime.base_url", f"http://127.0.0.1:{server.server_port}/v1")
    try:
        result = asyncio.run(asyncio.wait_for(runner.codex({"messages": [{"type": "human", "content": "Hello"}],
            "tools": []}, "gpt-6.1-sol", "codex"), timeout=30))
        assert result["decision"]["content"] == "Offline runtime verified"
        assert result["usage"]["input_tokens"] == 30
        assert requests
        # The only allowed native tool is a state-only planning tool, if supplied by this CLI version.
        for body in requests:
            assert all(tool.get("name") in {"update_plan"} for tool in body.get("tools", [])), body.get("tools")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
