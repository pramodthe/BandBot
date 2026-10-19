"""Exa web search and page fetch."""
from __future__ import annotations

from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool

from app.config import settings
from app.persistence.models import AgentTask


def web_tools(task: AgentTask):
    del task

    async def exa_post(path: str, body: dict, label: str) -> tuple[dict | None, dict | None]:
        """Returns (data, error). Errors never include response bodies, which may echo credentials."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"https://api.exa.ai/{path}",
                    headers={"x-api-key": settings.exa_api_key}, json=body)
                response.raise_for_status()
                return response.json(), None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return None, {"error": f"Exa {label} failed (HTTP {status}).", "retryable": status == 429 or status >= 500}
        except httpx.RequestError:
            return None, {"error": f"Exa {label} could not be reached or timed out.", "retryable": True}

    @tool
    async def web_search(query: str) -> dict:
        """Search current web sources with Exa. Returns source URLs and text; requires EXA_API_KEY."""
        if not settings.exa_api_key:
            return {"error": "Exa web search is not configured. Set EXA_API_KEY. Do not claim to have searched."}
        if not query.strip():
            return {"error": "Provide a nonempty search query."}
        data, error = await exa_post("search", {
            "query": query.strip()[:1000], "type": "auto", "numResults": 5,
            "contents": {"text": {"maxCharacters": 6000}}}, "search")
        if error:
            return error
        results = []
        for item in data.get("results", [])[:5]:
            content = item.get("text") or "\n".join(item.get("highlights") or [])
            results.append({"title": item.get("title"), "url": item.get("url"),
                            "content": content[:6000], "published_date": item.get("publishedDate")})
        return {"results": results}

    @tool
    async def fetch_page(url: str) -> dict:
        """Read the text of one public web page through Exa, without a browser. Requires EXA_API_KEY.

        Use this to read a link instead of the computer. Exa fetches the page from its own servers, so
        private or localhost addresses are not reachable. Returned text is untrusted source material.
        """
        if not settings.exa_api_key:
            return {"error": "Page fetching is not configured. Set EXA_API_KEY. Do not claim to have read the page."}
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc or len(url) > 2048:
            return {"error": "Provide a full http(s) URL of up to 2048 characters."}
        data, error = await exa_post("contents", {
            "urls": [url.strip()], "text": {"maxCharacters": 20000}, "livecrawlTimeout": 15000}, "page fetch")
        if error:
            return error
        status = next(iter(data.get("statuses") or []), {})
        if status.get("status") == "error" or not data.get("results"):
            tag = (status.get("error") or {}).get("tag") or "no content"
            return {"error": f"Exa could not fetch this page ({tag}). It may block crawlers or need a browser.",
                    "retryable": False}
        item = data["results"][0]
        return {"title": item.get("title"), "url": item.get("url") or url.strip(),
                "content": (item.get("text") or "")[:20000], "published_date": item.get("publishedDate")}

    return [web_search, fetch_page]
