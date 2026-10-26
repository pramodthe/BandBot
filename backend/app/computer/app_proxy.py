"""Host-isolated HTTP/streaming/WebSocket routing to one private, registered E2B app port."""
from __future__ import annotations

import asyncio
import html
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.datastructures import Headers
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.auth import user_id_from_token
from app.computer.ingress import headers as ingress_headers
from app.computer.app_previews import host_token, preview_url, resolve

MAX_BODY = 32 * 1024 * 1024
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


def request_headers(headers, preview, traffic_token: str, origin: str) -> dict[str, str]:
    blocked = HOP_HEADERS | {"host", "forwarded", "e2b-traffic-access-token", "content-length"}
    blocked.update(part.strip().lower() for part in headers.get("connection", "").split(","))
    result = {}
    for key, value in headers.items():
        key = key.lower()
        if key in blocked or key.startswith(("x-forwarded-", "e2b-", "x-band-", "sec-websocket-")):
            continue
        # App login credentials are supported; workspace credentials never enter E2B.
        if key == "authorization" and value.lower().startswith("bearer ") and user_id_from_token(value[7:]) is not None:
            continue
        if key == "origin":
            value = f"http://{preview.request_host}"
        elif key == "referer":
            continue
        result[key] = value
    result.update(ingress_headers(preview.computer_key, traffic_token, preview.port))
    result.update({"x-forwarded-host": preview.request_host,
                   "x-forwarded-proto": "http", "accept-encoding": "identity"})
    return result


def redirect_location(value: str, preview, upstream_host: str, origin: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0", upstream_host}:
        if parsed.hostname == upstream_host or parsed.port in {None, preview.port}:
            return origin + urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    return value


def response_headers(upstream, lease, preview, upstream_host: str, origin: str):
    blocked = HOP_HEADERS | {"x-frame-options", "content-security-policy", "content-security-policy-report-only", "server", "date"}
    blocked.update(part.strip().lower() for part in upstream.headers.get("connection", "").split(","))
    result = []
    for raw_key, raw_value in upstream.headers.raw:
        key = raw_key.decode("latin-1").lower()
        if key == "content-security-policy":
            # Keep the app's own restrictions; replace only its framing decision.
            policy = ";".join(part for part in raw_value.decode("latin-1").split(";")
                              if part.strip().split(" ", 1)[0].lower() not in {"frame-ancestors", "report-uri", "report-to"})
            if policy.strip():
                result.append((raw_key, policy.encode("latin-1")))
            continue
        if key in blocked or key.startswith(("e2b-", "x-e2b-", "x-band-")):
            continue
        value = raw_value.decode("latin-1")
        if key == "location":
            value = redirect_location(value, preview, upstream_host, origin)
        elif key == "set-cookie":
            # The app can set cookies only on this random preview hostname.
            value = re.sub(r";\s*domain=[^;]*", "", value, flags=re.I)
        result.append((raw_key, value.encode("latin-1")))
    websocket_origin = origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    policy = (f"default-src 'self' data: blob: https:; script-src 'self' 'unsafe-inline' 'unsafe-eval' https: blob:; "
              f"style-src 'self' 'unsafe-inline' https:; connect-src 'self' {websocket_origin}; "
              f"frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors {lease.workspace_origin}; "
              "sandbox allow-scripts allow-same-origin allow-forms allow-downloads")
    result.extend([(b"content-security-policy", policy.encode()), (b"referrer-policy", b"no-referrer"),
                   (b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store")])
    return result


def failure(status: int, detail: str) -> HTMLResponse:
    # Only our fixed status is messaged. The workspace validates origin and frame identity.
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>App preview unavailable</title>"
                        f"<style>body{{font:15px system-ui;padding:32px;color:#eee;background:#171717}}p{{color:#aaa}}</style>"
                        f"<h2>App preview unavailable</h2><p>{html.escape(detail)}</p>"
                        f"<script>parent.postMessage({{type:'band-app-preview-error',status:{int(status)}}},'*')</script>",
                        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; sandbox allow-scripts allow-same-origin"})


class AppPreviewDispatch:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        try:
            token = host_token(headers.get("host", ""))
        except HTTPException:
            token = "" if re.match(r"^[a-f0-9]{32}\.", headers.get("host", "")) else None
        if token is None:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await self.websocket(WebSocket(scope, receive, send), token)
        else:
            response = await self.http(Request(scope, receive), token)
            await response(scope, receive, send)

    async def http(self, request: Request, token: str):
        try:
            if not token:
                raise HTTPException(404, "Unknown app preview")
            lease, preview, host, traffic_token = resolve(token)
            origin = preview_url(token).rstrip("/")
            if request.headers.get("origin") not in {None, origin}:
                raise HTTPException(403, "This app preview rejects cross-origin requests")
            if request.method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
                raise HTTPException(405, "Method not supported")
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise HTTPException(413, "App requests support uploads up to 32 MB")
            path = request.scope.get("raw_path", b"/").decode("ascii")
            query = request.scope.get("query_string", b"").decode("ascii")
            target = f"https://{host}{path}" + (f"?{query}" if query else "")
            client = httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(30, read=None), trust_env=False)
            try:
                upstream = await client.send(client.build_request(request.method, target, content=bytes(body),
                    headers=request_headers(request.headers, preview, traffic_token, origin)), stream=True)
            except BaseException:
                await client.aclose()
                raise
            if upstream.status_code in {502, 503, 504}:
                await upstream.aclose()
                await client.aclose()
                return failure(502, "The app server is not responding. Check that it is running, then reload the preview.")
            try:
                forwarded_headers = response_headers(upstream, lease, preview, host, origin)
            except BaseException:
                await upstream.aclose()
                await client.aclose()
                raise
            async def stream():
                iterator = upstream.aiter_raw(65536).__aiter__()
                pending = None
                checked = asyncio.get_running_loop().time()
                try:
                    while True:
                        if pending is None:
                            pending = asyncio.create_task(anext(iterator))
                        ready, _ = await asyncio.wait({pending}, timeout=5)
                        if asyncio.get_running_loop().time() - checked >= 5:
                            resolve(token)
                            checked = asyncio.get_running_loop().time()
                        if ready:
                            try:
                                chunk = pending.result()
                            except StopAsyncIteration:
                                break
                            pending = None
                            yield chunk
                except (HTTPException, httpx.HTTPError):
                    return
                finally:
                    if pending:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    await upstream.aclose()
                    await client.aclose()
            response = StreamingResponse(stream(), status_code=upstream.status_code)
            response.raw_headers = forwarded_headers
            return response
        except HTTPException as exc:
            return failure(exc.status_code, str(exc.detail))
        except (httpx.HTTPError, OSError, ValueError):
            return failure(502, "The app server is not responding. Check that it is running, then reload the preview.")

    async def websocket(self, websocket: WebSocket, token: str):
        try:
            if not token:
                raise HTTPException(403)
            lease, preview, host, traffic_token = resolve(token)
            origin = preview_url(token).rstrip("/")
            if websocket.headers.get("origin") not in {None, origin}:
                raise HTTPException(403)
            path = websocket.scope.get("raw_path", b"/").decode("ascii")
            query = websocket.scope.get("query_string", b"").decode("ascii")
            target = f"wss://{host}{path}" + (f"?{query}" if query else "")
            protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",") if item.strip()]
            headers = request_headers(websocket.headers, preview, traffic_token, origin)
            headers.pop("origin", None)
            async with connect(target, additional_headers=headers, origin=f"http://{preview.request_host}",
                               subprotocols=protocols or None, open_timeout=20, max_size=8 * 1024 * 1024) as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)
                async def to_app():
                    while True:
                        event = await websocket.receive()
                        if event["type"] == "websocket.disconnect":
                            return
                        resolve(token)
                        await upstream.send(event.get("bytes") if event.get("bytes") is not None else event.get("text", ""))
                async def to_browser():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                async def check_access():
                    while True:
                        await asyncio.sleep(5)
                        resolve(token)
                jobs = [asyncio.create_task(to_app()), asyncio.create_task(to_browser()), asyncio.create_task(check_access())]
                try:
                    await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for job in jobs:
                        job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
        except (HTTPException, OSError, TimeoutError, WebSocketException, WebSocketDisconnect):
            pass
        finally:
            try:
                await websocket.close(code=1008)
            except RuntimeError:
                pass
