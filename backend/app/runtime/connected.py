"""Vendor runtimes supply reasoning; the checkpointed app harness owns every tool effect.

Each model step uses a fresh, tool-disabled vendor session and a structured decision.
This deliberately keeps native shell/MCP/plugin execution out of the worker host.
The durable graph retains the conversation, tools, approvals, helpers and memory.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field
from jsonschema import ValidationError, validate

from app.agent_connections import credential
from app.auth import decrypt_secret, encrypt_secret
from app.persistence.models import AgentConnection, db
from app.config import settings
from app.runtime.connected_runner import decision_schema

_codex_account_lock = asyncio.Lock()


def isolated_environment(directory: str, token: str) -> dict[str, str]:
    # No Band/E2B/Composio keys, APP_SECRET, inherited provider keys or user CLI settings.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "SSL_CERT_FILE"}}
    env.update(HOME=directory, USERPROFILE=directory, CODEX_HOME=directory, CLAUDE_CONFIG_DIR=directory,
               TMP=directory, TEMP=directory, TMPDIR=directory, BAND_RUNTIME_TOKEN=token,
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return env


async def run_step(provider: str, model: str, token: str, payload: dict) -> dict:
    runner = str(Path(__file__).with_name("connected_runner.py"))
    with tempfile.TemporaryDirectory(prefix="band-runtime-") as directory:
        env = isolated_environment(directory, token)
        native = provider == "codex" and payload.get("auth_method") == "codex_chatgpt"
        if native:
            (Path(directory) / "auth.json").write_text(token, encoding="utf-8")
            env["BAND_RUNTIME_TOKEN"] = ""
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        process = await asyncio.create_subprocess_exec(sys.executable, runner, provider, model,
            settings.codex_executable, cwd=directory, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=2_000_000, **options)
        try:
            # Input is bounded by the caller. Credentials travel only through the child environment.
            async with asyncio.timeout(180):
                stdout, _ = await process.communicate(json.dumps(payload).encode())
            if process.returncode or len(stdout) > 2_000_000:
                raise ValueError(f"{provider.title()} runtime failed; check the connection, model and runtime installation")
            return json.loads(stdout)
        finally:
            if os.name != "nt":
                # Also reap descendants if the runner exited before its CLI child.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                await killer.wait()
            if process.returncode is None:
                await process.communicate()
            if native and payload.get("connection_id"):
                # Codex manages OAuth refresh. Persist rotation even if a later turn fails/cancels.
                auth_file = Path(directory) / "auth.json"
                if auth_file.is_file() and auth_file.stat().st_size <= 100_000:
                    try:
                        updated = json.loads(auth_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        updated = None
                    if isinstance(updated, dict) and isinstance(updated.get("tokens"), dict):
                        with db() as session:
                            current = session.get(AgentConnection, payload["connection_id"])
                            if current and current.auth_method == "codex_chatgpt":
                                current.secret_enc = encrypt_secret(json.dumps({"auth_json": updated}))
                                session.add(current)
                                session.commit()


class ConnectedRuntimeModel(BaseChatModel):
    provider: str
    model_name: str
    connection_id: str = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return f"band-{self.provider}-runtime"

    @property
    def _identifying_params(self) -> dict:
        return {"provider": self.provider, "model": self.model_name}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t)["function"] for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("Connected runtimes run asynchronously in the task worker")

    async def _agenerate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        with db() as session:
            current = session.get(AgentConnection, self.connection_id)
        if current and current.auth_method == "codex_chatgpt":
            # Serialize credential reads, native refresh, and writes across channels/helpers.
            async with _codex_account_lock:
                return await self._runtime_response(messages, **kwargs)
        return await self._runtime_response(messages, **kwargs)

    async def _runtime_response(self, messages: list[BaseMessage], **kwargs) -> ChatResult:
        row, token = await credential(self.connection_id)
        if row.provider != self.provider:
            raise ValueError("The agent connection does not match its runtime")
        tools = kwargs.get("tools", [])
        names = {t["name"] for t in tools}
        transcript = [m.model_dump(mode="json", exclude={"response_metadata", "usage_metadata", "id"}) for m in messages]
        payload = {"messages": transcript, "tools": tools, "auth_method": row.auth_method}
        if row.auth_method == "codex_chatgpt":
            payload["connection_id"] = row.id
        if len(json.dumps(payload)) > 6_000_000:
            raise ValueError("Runtime context exceeds its transport limit")
        if self.provider == "openclaw":
            from app.openclaw_gateway import run_gateway_step
            try:
                result = await run_gateway_step(json.loads(decrypt_secret(row.secret_enc)), self.model_name, payload)
            except Exception:
                raise ValueError("OpenClaw request failed. Check the connection, Gateway policy, and model access.") from None
        else:
            result = await run_step(self.provider, self.model_name, token, payload)
        decision = result["decision"]
        try:
            validate(decision, decision_schema(sorted(names)))
        except ValidationError:
            raise ValueError("Runtime returned an invalid app decision") from None
        calls = []
        for item in decision.get("tool_calls", []):
            if item["name"] not in names:
                raise ValueError("Runtime requested a tool outside this task's tool set")
            args = json.loads(item["arguments_json"])
            if not isinstance(args, dict):
                raise ValueError("Runtime returned invalid tool arguments")
            calls.append({"name": item["name"], "args": args, "id": f"call_{uuid4().hex}", "type": "tool_call"})
        if not calls and not decision.get("content", "").strip():
            raise ValueError("Runtime returned no answer or tool request")
        raw_usage = result.get("usage", {})
        input_tokens = max(0, int(raw_usage.get("input_tokens", 0)))
        output_tokens = max(0, int(raw_usage.get("output_tokens", 0)))
        # Missing usage must never silently turn a connected runtime into an unmetered agent.
        if not input_tokens and not output_tokens:
            raise ValueError("Runtime did not report token usage; refusing unmetered execution")
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                 "total_tokens": input_tokens + output_tokens,
                 "input_token_details": {"cache_read": int(raw_usage.get("cache_read_tokens", 0)),
                                         "cache_creation": int(raw_usage.get("cache_write_tokens", 0))}}
        message = AIMessage(content=decision.get("content", ""), tool_calls=calls, usage_metadata=usage,
            id=f"runtime_{uuid4().hex}", response_metadata={"model_name": self.model_name, "provider": self.provider})
        return ChatResult(generations=[ChatGeneration(message=message)])
