"""Normalized research results independent of provider response formats."""
from urllib.parse import urlsplit
import httpx
from app.providers.contracts import ProviderError

async def request(connection, path, body):
    from app.providers.context import current_task_id
    task_id = current_task_id()
    if task_id:
        from app.providers.operations import dispatch
        return await dispatch(task_id, "research", {"connection_id": connection.id, "revision": connection.revision, "provider": connection.provider},
            {"path": path, "body": body}, lambda: _request(connection, path, body))
    return await _request(connection, path, body)

async def _request(connection, path, body):
    base = "https://api.exa.ai" if connection.provider == "exa" else "https://api.tavily.com"
    headers = {"x-api-key": connection.secret["api_key"]} if connection.provider == "exa" else {"Authorization": "Bearer " + connection.secret["api_key"]}
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
            result = await client.post(base + path, headers=headers, json=body)
            result.raise_for_status()
            return result.json()
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        failure = ProviderError("research", "authentication" if status in {401, 403} else "quota_or_rate_limit" if status == 429 else "unavailable", connection_id=connection.id)
        failure.confirmed_rejection = status in {400, 401, 403, 404, 422, 429}
        raise failure from None
    except Exception:
        raise ProviderError("research", "unavailable", connection_id=connection.id) from None

async def search(connection, query, *, limit=5):
    if connection.provider == "exa":
        data = await request(connection, "/search", {"query": query[:1000], "type": "auto", "numResults": limit,
            "contents": {"text": {"maxCharacters": 6000}}})
    elif connection.provider == "tavily":
        data = await request(connection, "/search", {"query": query[:1000], "max_results": limit,
            "include_answer": False, "include_raw_content": False})
    else:
        raise ProviderError("research", "unsupported_provider", connection_id=connection.id)
    return {"results": [{"title": item.get("title"), "url": item.get("url"),
        "content": (item.get("text") or item.get("content") or "\n".join(item.get("highlights") or []))[:6000],
        "published_date": item.get("publishedDate") or item.get("published_date")} for item in data.get("results", [])[:limit]]}

async def fetch(connection, url):
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or len(url) > 2048:
        raise ValueError("Provide a public http(s) URL without credentials")
    if connection.provider == "exa":
        data = await request(connection, "/contents", {"urls": [url], "text": {"maxCharacters": 20000}, "livecrawlTimeout": 15000})
    elif connection.provider == "tavily":
        data = await request(connection, "/extract", {"urls": [url], "format": "text"})
    else:
        raise ProviderError("research", "unsupported_provider", connection_id=connection.id)
    items = data.get("results") or []
    if not items:
        return {"error": "This page could not be read. It may require a browser or sign-in.", "retryable": False}
    item = items[0]
    return {"title": item.get("title"), "url": item.get("url") or url,
            "content": (item.get("text") or item.get("raw_content") or "")[:20000],
            "published_date": item.get("publishedDate") or item.get("published_date")}
