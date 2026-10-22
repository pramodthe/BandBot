"""Exercise the bundled SDK/CLI against a fake local Anthropic endpoint."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.runtime.connected_runner import claude


def test_real_claude_runtime_keeps_native_tools_disabled(tmp_path, monkeypatch):
    requests = []
    decision = {"content": "Offline Claude verified", "tool_calls": []}

    class FakeAnthropic(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(body)
            if self.path.endswith("/count_tokens"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens":30}')
                return
            tool = next((t for t in body.get("tools", []) if t.get("name") == "StructuredOutput"), None)
            block = ({"type": "tool_use", "id": "toolu_offline", "name": "StructuredOutput", "input": {}} if tool else
                     {"type": "text", "text": ""})
            delta = ({"type": "input_json_delta", "partial_json": json.dumps(decision)} if tool else
                     {"type": "text_delta", "text": json.dumps(decision)})
            events = [
                {"type": "message_start", "message": {"id": "msg_offline", "type": "message", "role": "assistant",
                    "content": [], "model": "claude-sonnet-4-5", "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 30, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": block},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
                    "usage": {"input_tokens": 30, "output_tokens": 10}},
                {"type": "message_stop"},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.chdir(tmp_path)
    for name in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("BAND_RUNTIME_TOKEN", "offline-test-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    try:
        result = asyncio.run(asyncio.wait_for(claude({"messages": [{"type": "human", "content": "Hello"}],
            "tools": []}, "claude-sonnet-4-5"), timeout=30))
        assert result["decision"] == decision
        assert result["usage"]["input_tokens"] > 0
        assert requests
        for body in requests:
            assert all(tool.get("name") == "StructuredOutput" for tool in body.get("tools", []))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
