"""Codex-managed browser login in a separate credential home, never the host's login.

The API only runs authentication/catalog RPCs. Model execution stays in the worker.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from sqlmodel import select

from app.agent_connections import MODEL_ID
from app.auth import encrypt_secret
from app.config import settings
from app.persistence.models import AgentConnection, AgentOAuthAttempt, User, db
from app.runtime.connected_runner import codex_command
from app.workspace.audit import record_human

_jobs: dict[str, asyncio.Task] = {}


async def stop_process(process):
    if process.returncode is None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            await killer.wait()
        else:
            process.kill()
    await process.communicate()
    if process.stdin:
        try:
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


def login_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname != "auth.openai.com" or parts.username or parts.password or parts.port not in {None, 443}:
        raise ValueError("Codex returned an invalid sign-in URL")
    return value


async def start_login(user_id: int) -> dict:
    attempt_id = uuid4().hex
    with db() as session:
        active = session.exec(select(AgentOAuthAttempt).where(AgentOAuthAttempt.state_hash.startswith("codex-native:"),
            AgentOAuthAttempt.status == "pending")).all()
        # The native browser callback uses one local port. Never start competing login listeners.
        if any(row.id in _jobs for row in active):
            raise ValueError("Another Codex sign-in is in progress. Finish or cancel it first.")
        for row in active:
            row.status = "expired"
            session.add(row)
        session.add(AgentOAuthAttempt(id=attempt_id, user_id=user_id, state_hash="codex-native:" + attempt_id,
            verifier_enc="", nonce="", redirect_uri=""))
        session.commit()
    ready = asyncio.get_running_loop().create_future()
    job = asyncio.create_task(_login(attempt_id, user_id, ready))
    _jobs[attempt_id] = job
    job.add_done_callback(lambda _: _jobs.pop(attempt_id, None))
    try:
        return await asyncio.wait_for(asyncio.shield(ready), 30)
    except BaseException:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        raise


async def _login(attempt_id: str, user_id: int, ready: asyncio.Future):
    process = None
    succeeded = False
    try:
        from app.runtime.connected import isolated_environment

        with tempfile.TemporaryDirectory(prefix="band-codex-login-") as directory:
            try:
                command = codex_command(settings.codex_executable) + ["-c", 'cli_auth_credentials_store="file"',
                    "app-server", "--listen", "stdio://"]
                process = await asyncio.create_subprocess_exec(*command, cwd=directory,
                    env=isolated_environment(directory, ""), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=2_000_000,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                next_id = 0
                events = []

                async def send(message):
                    process.stdin.write((json.dumps(message) + "\n").encode())
                    await process.stdin.drain()

                async def receive():
                    raw = await process.stdout.readline()
                    if not raw:
                        raise ValueError("Codex sign-in process stopped")
                    return json.loads(raw)

                async def rpc(method, params):
                    nonlocal next_id
                    next_id += 1
                    await send({"id": next_id, "method": method, "params": params})
                    while True:
                        event = await receive()
                        if event.get("id") == next_id:
                            if "error" in event:
                                raise ValueError("Codex sign-in request failed")
                            return event.get("result", {})
                        if "id" in event:
                            raise ValueError("Unexpected Codex request during sign-in")
                        events.append(event)

                async with asyncio.timeout(600):
                    await rpc("initialize", {"clientInfo": {"name": "BAND_BOTS", "title": "BAND_BOTS", "version": "0.1.0"}})
                    await send({"method": "initialized", "params": {}})
                    login = await rpc("account/login/start", {"type": "chatgpt"})
                    url = login_url(login["authUrl"])
                    login_id = login["loginId"]
                    ready.set_result({"attempt_id": attempt_id, "url": url})
                    while True:
                        event = events.pop(0) if events else await receive()
                        if event.get("method") == "account/login/completed" and event.get("params", {}).get("loginId") == login_id:
                            if not event["params"].get("success"):
                                raise ValueError("Codex sign-in was not completed")
                            break
                    account = (await rpc("account/read", {"refreshToken": False})).get("account") or {}
                    if account.get("type") != "chatgpt":
                        raise ValueError("Codex did not authenticate a ChatGPT account")
                    models = []
                    cursor = None
                    for _ in range(10):
                        page = await rpc("model/list", {"limit": 100, **({"cursor": cursor} if cursor else {})})
                        models.extend(item for item in page.get("data", []) if MODEL_ID.fullmatch(item.get("model", "")))
                        cursor = page.get("nextCursor")
                        if not cursor:
                            break
                    models.sort(key=lambda item: not item.get("isDefault", False))
                    catalog = list(dict.fromkeys(item["model"] for item in models))
                    if not catalog:
                        raise ValueError("Codex did not return available models")
                    auth_file = Path(directory) / "auth.json"
                    if not auth_file.is_file() or auth_file.stat().st_size > 100_000:
                        raise ValueError("Codex credentials were not saved")
                    auth_data = json.loads(auth_file.read_text(encoding="utf-8"))
                    if not isinstance(auth_data.get("tokens"), dict):
                        raise ValueError("Codex did not save a token session")
                    with db() as session:
                        row = session.get(AgentOAuthAttempt, attempt_id)
                        owner = session.get(User, user_id)
                        if not row or row.status != "pending" or owner is None:
                            raise ValueError("Sign-in was cancelled")
                        # This block contains no awaits: cancellation cannot interleave with the save.
                        connection = AgentConnection(id=uuid4().hex, provider="codex",
                            label=str(account.get("email") or "Codex account")[:80], created_by=user_id,
                            auth_method="codex_chatgpt", models=catalog,
                            secret_enc=encrypt_secret(json.dumps({"auth_json": auth_data})))
                        session.add(connection)
                        row.status, row.connection_id = "connected", connection.id
                        session.add(row)
                        record_human(owner, "agent_connection_created", session=session, provider="codex",
                                     connection_id=connection.id)
                        session.commit()
                    succeeded = True
            finally:
                if process:
                    # Shutdown can cancel a successful login while its pipes are closing.
                    # Finish cleanup before its event loop or credential home disappears.
                    cleanup = asyncio.create_task(stop_process(process))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        if not ready.done():
            ready.set_exception(ValueError("Codex sign-in could not start. Check its installation and that port 1455 is free."))
    finally:
        with db() as session:
            row = session.get(AgentOAuthAttempt, attempt_id)
            if row and row.status == "pending":
                row.status = "connected" if succeeded else "failed"
                session.add(row)
                session.commit()


async def cancel_login(attempt_id: str):
    job = _jobs.get(attempt_id)
    if job:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


async def shutdown():
    jobs = list(_jobs.values())
    for job in jobs:
        if not job.cancelling():
            job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
