"""User-owned provider connections. OAuth callbacks use single-use, owner-bound state."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from typing import Literal
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import update
from sqlmodel import Session, select

from app.agent_connections import (RESOURCE, TOKEN_URL, public_connection, runtime_available, save_connection, unexpired, validate_key)
from app.auth import current_user, decrypt_secret, encrypt_secret
from app.config import settings
from app.openai_provider import model_catalog
from app.openclaw_gateway import gateway_urls, validate_gateway
from app.persistence.models import AgentConnection, AgentHost, AgentOAuthAttempt, AgentRuntime, User, db, get_session, utcnow
from app.workspace.audit import record
from app.workspace.permissions import can_connect_agents

router = APIRouter()


class OAuthAccessFilter(logging.Filter):
    """Authorization codes and state must not end up in Uvicorn's URL access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and path.startswith("/auth/callback?"):
                args = list(record.args)
                args[2] = "/auth/callback"
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(OAuthAccessFilter())


def connection_user(user: User = Depends(current_user), session: Session = Depends(get_session)) -> User:
    if not can_connect_agents(session, user.id):
        raise HTTPException(403, "Sign in before adding agent connections")
    return user


class ConnectBody(BaseModel):
    provider: Literal["codex", "claude", "cursor", "openclaw"]
    label: str = Field(min_length=1, max_length=80)
    api_key: SecretStr = Field(min_length=10, max_length=2000)
    gateway_url: str | None = Field(default=None, max_length=500)
    gateway_agent_id: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,80}$")


@router.get("/api/agent-connections")
def connections(request: Request, _user: User = Depends(connection_user), session: Session = Depends(get_session)):
    return {"connections": [public_connection(row) for row in session.exec(select(AgentConnection).where(
                AgentConnection.created_by == _user.id)).all()],
            "codex_chatgpt_sign_in": settings.codex_chatgpt_sign_in,
            "codex_local_sign_in": request.url.hostname in {"localhost", "127.0.0.1"} and runtime_available("codex"),
            "runtimes": {provider: runtime_available(provider) for provider in ("codex", "claude", "cursor", "openclaw")},
            "openclaw_gateways": gateway_urls()}


@router.post("/api/agent-connections")
async def connect(body: ConnectBody, user: User = Depends(connection_user)):
    key = body.api_key.get_secret_value().strip()
    if not body.label.strip() or not key:
        raise HTTPException(422, "Enter a connection name and API key")
    secret = {"api_key": key}
    method = "api_key"
    try:
        if body.provider == "openclaw":
            if not body.gateway_url or not body.gateway_agent_id:
                raise HTTPException(422, "Choose a Gateway and enter its dedicated agent ID")
            models = await validate_gateway(body.gateway_url, key, body.gateway_agent_id)
            secret.update(gateway_url=body.gateway_url.rstrip("/"), agent_id=body.gateway_agent_id)
            method = "gateway_token"
        else:
            models = await validate_key(body.provider, key)
    except HTTPException:
        raise
    except Exception:
        if body.provider == "openclaw":
            raise HTTPException(422, "Gateway connection failed. Check the token, approved URL, and dedicated agent setup in docs/connected-agents.md.") from None
        raise HTTPException(422, "Authentication failed. Check the API key and provider account access.") from None
    row = save_connection(body.provider, body.label, secret, user.id, method=method, models=models)
    record("agent_connection_created", actor_type="human", actor_id=user.id, actor_name=user.display_name,
           detail={"provider": body.provider, "connection_id": row.id})
    return public_connection(row)


@router.post("/api/agent-connections/codex/login")
async def native_login(request: Request, user: User = Depends(connection_user)):
    if request.url.hostname not in {"localhost", "127.0.0.1"}:
        raise HTTPException(409, "Codex browser sign-in requires the app and browser on the same local computer")
    if not runtime_available("codex"):
        raise HTTPException(409, "Install the Codex CLI on this server before signing in")
    from app.codex_login import start_login
    try:
        return await start_login(user.id)
    except Exception:
        raise HTTPException(409, "Codex sign-in could not start. Finish any other Codex login and check that local port 1455 is free.") from None


@router.get("/api/agent-connections/codex/login/{attempt_id}")
def native_login_status(attempt_id: str, user: User = Depends(connection_user), session: Session = Depends(get_session)):
    return sign_in_status(attempt_id, user, session)


@router.delete("/api/agent-connections/codex/login/{attempt_id}")
async def cancel_native_login(attempt_id: str, user: User = Depends(connection_user), session: Session = Depends(get_session)):
    row = session.get(AgentOAuthAttempt, attempt_id)
    if row is None or row.user_id != user.id or not row.state_hash.startswith("codex-native:"):
        raise HTTPException(404, "Sign-in attempt not found")
    if row.status == "connected":
        return {"cancelled": False}
    if row.status == "pending":
        row.status = "cancelled"
        session.add(row)
        session.commit()
    from app.codex_login import cancel_login
    await cancel_login(attempt_id)
    return {"cancelled": True}


@router.delete("/api/agent-connections/{connection_id}")
def disconnect(connection_id: str, user: User = Depends(connection_user), session: Session = Depends(get_session)):
    row = session.get(AgentConnection, connection_id)
    if row is None or row.created_by != user.id:
        raise HTTPException(404, "Connection not found")
    if session.exec(select(AgentRuntime.agent_id).where(AgentRuntime.connection_id == connection_id)).first():
        raise HTTPException(409, "Remove agents using this connection before disconnecting it")
    session.delete(row)
    record("agent_connection_deleted", actor_type="human", actor_id=user.id, actor_name=user.display_name,
           detail={"provider": row.provider, "connection_id": row.id}, session=session)
    session.commit()
    return {"disconnected": True}


@router.post("/api/agent-connections/codex/sign-in")
def sign_in(request: Request, user: User = Depends(connection_user), session: Session = Depends(get_session)):
    redirect = urlsplit(settings.agent_oauth_redirect_uri)
    if not settings.codex_chatgpt_sign_in:
        raise HTTPException(409, "ChatGPT sign-in is not enabled for this deployment. Connect an API key instead.")
    if (redirect.scheme != "http" or redirect.hostname != "127.0.0.1" or redirect.path != "/auth/callback"
            or redirect.username or redirect.query or redirect.fragment
            or request.url.hostname not in {"localhost", "127.0.0.1"}):
        raise HTTPException(409, "This sign-in flow is available only on an eligible local installation")
    host = session.get(AgentHost, 1)
    if host is None:
        host = AgentHost(host_id=f"urn:uuid:{uuid4()}")
        session.add(host)
    state, verifier, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    # Starting a new attempt invalidates this admin's abandoned attempts.
    session.exec(update(AgentOAuthAttempt).where(AgentOAuthAttempt.user_id == user.id,
        AgentOAuthAttempt.status == "pending").values(status="cancelled"))
    attempt = AgentOAuthAttempt(id=uuid4().hex, user_id=user.id, state_hash=hashlib.sha256(state.encode()).hexdigest(),
        verifier_enc=encrypt_secret(verifier), nonce=nonce, redirect_uri=settings.agent_oauth_redirect_uri)
    session.add(attempt)
    session.commit()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = {"client_id": "dynamic_agent_client", "agent_name_hint": "BAND_BOTS", "ext_agent_host_id": host.host_id,
        "response_type": "code", "redirect_uri": attempt.redirect_uri,
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct", "resource": RESOURCE,
        "state": state, "nonce": nonce, "code_challenge_method": "S256", "code_challenge": challenge}
    return {"attempt_id": attempt.id, "url": "https://auth.openai.com/api/accounts/authorize?" + urlencode(query)}


@router.get("/api/agent-connections/codex/sign-in/{attempt_id}")
def sign_in_status(attempt_id: str, user: User = Depends(connection_user), session: Session = Depends(get_session)):
    attempt = session.get(AgentOAuthAttempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(404, "Sign-in attempt not found")
    status = attempt.status if unexpired(attempt.created_at) else "expired"
    return {"status": status, "connection_id": attempt.connection_id}


@router.delete("/api/agent-connections/codex/sign-in/{attempt_id}")
def cancel_sign_in(attempt_id: str, user: User = Depends(connection_user), session: Session = Depends(get_session)):
    session.exec(update(AgentOAuthAttempt).where(AgentOAuthAttempt.id == attempt_id,
        AgentOAuthAttempt.user_id == user.id, AgentOAuthAttempt.status.in_(["pending", "exchanging"]))
        .values(status="cancelled", verifier_enc=""))
    session.commit()
    return {"cancelled": True}


async def verify_identity(client: httpx.AsyncClient, token: str, client_id: str, nonce: str) -> dict:
    # Fixed trusted issuer/JWKS location; never follow an untrusted JWT's jku/x5u URL.
    response = await client.get("https://auth.openai.com/.well-known/jwks.json")
    response.raise_for_status()
    header = jwt.get_unverified_header(token)
    keys = response.json()["keys"]
    key = next(k for k in keys if k.get("kid") == header.get("kid"))
    claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=["RS256"],
                        audience=client_id, issuer="https://auth.openai.com", options={"require": ["exp", "sub", "iat"]})
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("Invalid identity nonce")
    return claims


@router.get("/auth/callback", response_class=HTMLResponse)
async def callback(state: str = "", code: str = "", client_id: str = "", error: str = ""):
    with db() as session:
        attempt = session.exec(select(AgentOAuthAttempt).where(
            AgentOAuthAttempt.state_hash == hashlib.sha256(state.encode()).hexdigest())).first()
        if attempt is None or attempt.status != "pending" or not unexpired(attempt.created_at):
            raise HTTPException(400, "This sign-in attempt is invalid or expired")
        if not can_connect_agents(session, attempt.user_id):
            raise HTTPException(403, "This account can no longer connect agents")
        claimed = session.exec(update(AgentOAuthAttempt).where(AgentOAuthAttempt.id == attempt.id,
            AgentOAuthAttempt.status == "pending").values(status="exchanging"))
        session.commit()
        if not claimed.rowcount:
            raise HTTPException(400, "This sign-in attempt has already been used")
    succeeded = False
    try:
        if error or not code or not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", client_id) or client_id == "dynamic_agent_client":
            raise ValueError("Sign-in was not completed")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(TOKEN_URL, data={"grant_type": "authorization_code", "client_id": client_id,
                "code": code, "code_verifier": decrypt_secret(attempt.verifier_enc),
                "redirect_uri": attempt.redirect_uri, "resource": RESOURCE})
            response.raise_for_status()
            tokens = response.json()
            claims = await verify_identity(client, tokens["id_token"], client_id, attempt.nonce)
        if "chatgpt.tokens.use.direct" not in tokens.get("scope", "").split() or not tokens.get("refresh_token"):
            raise ValueError("ChatGPT plan usage was not authorized")
        secret = {k: tokens[k] for k in ("access_token", "refresh_token", "scope")}
        secret.update(client_id=client_id, expires_at=utcnow().timestamp() + int(tokens.get("expires_in", 3600)))
        connection = AgentConnection(id=uuid4().hex, provider="codex", auth_method="chatgpt",
            label=str(claims.get("email") or "ChatGPT account")[:80], created_by=attempt.user_id,
            secret_enc=encrypt_secret(json.dumps(secret)), models=[m["id"] for m in model_catalog()])
        # Check cancellation/permissions and save the connection and success receipt atomically.
        with db() as session:
            if not can_connect_agents(session, attempt.user_id):
                raise ValueError("Account is no longer available")
            claimed = session.exec(update(AgentOAuthAttempt).where(AgentOAuthAttempt.id == attempt.id,
                AgentOAuthAttempt.status == "exchanging").values(status="connected", connection_id=connection.id, verifier_enc=""))
            if not claimed.rowcount:
                raise ValueError("Sign-in was cancelled")
            session.add(connection)
            record("agent_connection_created", actor_type="human", actor_id=attempt.user_id, actor_name="Workspace admin",
                   detail={"provider": "codex", "connection_id": connection.id}, session=session)
            session.commit()
        succeeded = True
    except Exception:
        # Callback URLs, tokens and provider errors must never appear in app logs or HTML.
        pass
    with db() as session:
        row = session.get(AgentOAuthAttempt, attempt.id)
        if row.status == "exchanging":
            row.status, row.verifier_enc = "failed", ""
            session.add(row)
            session.commit()
    message = "Codex is connected. Return to BAND_BOTS to add your agent." if succeeded else (
        "Sign-in could not be completed. Return to BAND_BOTS and try again, or connect an API key.")
    return HTMLResponse(f"<!doctype html><html><head><title>BAND_BOTS connection</title></head><body><p>{message}</p></body></html>",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
