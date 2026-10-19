"""Exa request/response contracts without external API calls or credentials."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import app.runtime.tools.web as module
from app.runtime.tools import tools_for
from app.persistence.models import AgentTask


def search_tool():
    task = AgentTask(id="search-test", source_key="search-test", channel_id=1,
                     agent_id=1, room_id="test", prompt="Search the web")
    return next(tool for tool in tools_for(task) if tool.name == "web_search")


def fake_exa(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(module, "settings", SimpleNamespace(exa_api_key="test-secret"))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))


def test_exa_authentication_content_request_and_bounded_results(monkeypatch):
    def handle(request):
        assert str(request.url) == "https://api.exa.ai/search"
        assert request.headers["x-api-key"] == "test-secret"
        body = json.loads(request.content)
        assert body["type"] == "auto" and body["numResults"] == 5
        assert body["contents"] == {"text": {"maxCharacters": 6000}}
        assert len(body["query"]) == 1000
        assert "test-secret" not in request.content.decode()
        return httpx.Response(200, json={"results": [
            {"title": "Source", "url": "https://example.com", "text": "x" * 7000,
             "publishedDate": "2026-10-16"} for _ in range(8)]})
    fake_exa(monkeypatch, handle)
    result = asyncio.run(search_tool().ainvoke({"query": "query " * 250}))
    assert len(result["results"]) == 5
    assert len(result["results"][0]["content"]) == 6000
    assert result["results"][0]["published_date"] == "2026-10-16"


def test_exa_highlights_fallback(monkeypatch):
    fake_exa(monkeypatch, lambda request: httpx.Response(200, json={"results": [
        {"title": "Source", "url": "https://example.com", "highlights": ["First", "Second"]}]}))
    result = asyncio.run(search_tool().ainvoke({"query": "current information"}))
    assert result["results"][0]["content"] == "First\nSecond"


@pytest.mark.parametrize("status,retryable", [(401, False), (429, True), (503, True)])
def test_exa_errors_are_actionable_without_leaking_response_body(monkeypatch, status, retryable):
    fake_exa(monkeypatch, lambda request: httpx.Response(status, text="private error test-secret"))
    result = asyncio.run(search_tool().ainvoke({"query": "test"}))
    assert result["retryable"] is retryable
    assert f"HTTP {status}" in result["error"]
    assert "test-secret" not in str(result)


def test_exa_timeout(monkeypatch):
    def handle(request):
        raise httpx.ReadTimeout("secret response", request=request)
    fake_exa(monkeypatch, handle)
    result = asyncio.run(search_tool().ainvoke({"query": "test"}))
    assert result["retryable"] is True
    assert "timed out" in result["error"]
    assert "secret" not in str(result)


def test_empty_query_does_not_call_exa(monkeypatch):
    def handle(request):
        pytest.fail("Empty queries must not make API calls")
    fake_exa(monkeypatch, handle)
    assert "nonempty" in asyncio.run(search_tool().ainvoke({"query": "  "}))["error"]


def fetch_tool():
    task = AgentTask(id="fetch-test", source_key="fetch-test", channel_id=1,
                     agent_id=1, room_id="test", prompt="Read a page")
    return next(tool for tool in tools_for(task) if tool.name == "fetch_page")


def test_fetch_page_requests_one_url_and_bounds_text(monkeypatch):
    def handle(request):
        assert str(request.url) == "https://api.exa.ai/contents"
        assert request.headers["x-api-key"] == "test-secret"
        body = json.loads(request.content)
        assert body == {"urls": ["https://example.com/a"], "text": {"maxCharacters": 20000},
                        "livecrawlTimeout": 15000}
        return httpx.Response(200, json={
            "results": [{"title": "Example", "url": "https://example.com/a", "text": "x" * 25000,
                         "publishedDate": "2026-10-16"}],
            "statuses": [{"id": "https://example.com/a", "status": "success", "source": "crawled"}]})
    fake_exa(monkeypatch, handle)
    result = asyncio.run(fetch_tool().ainvoke({"url": " https://example.com/a "}))
    assert result["title"] == "Example" and result["url"] == "https://example.com/a"
    assert len(result["content"]) == 20000
    assert result["published_date"] == "2026-10-16"


def test_fetch_page_reports_per_url_failure(monkeypatch):
    fake_exa(monkeypatch, lambda request: httpx.Response(200, json={"results": [], "statuses": [
        {"id": "https://example.com", "status": "error",
         "error": {"tag": "CRAWL_NOT_FOUND", "httpStatusCode": 404}}]}))
    result = asyncio.run(fetch_tool().ainvoke({"url": "https://example.com"}))
    assert "CRAWL_NOT_FOUND" in result["error"] and result["retryable"] is False


@pytest.mark.parametrize("url", ["example.com", "file:///etc/passwd", "ftp://example.com/x",
                                 "https://", "https://example.com/" + "a" * 2048])
def test_fetch_page_rejects_invalid_urls_without_calling_exa(monkeypatch, url):
    def handle(request):
        pytest.fail("Invalid URLs must not make API calls")
    fake_exa(monkeypatch, handle)
    assert "http(s) URL" in asyncio.run(fetch_tool().ainvoke({"url": url}))["error"]


def test_fetch_page_http_errors_do_not_leak_response_body(monkeypatch):
    fake_exa(monkeypatch, lambda request: httpx.Response(503, text="private error test-secret"))
    result = asyncio.run(fetch_tool().ainvoke({"url": "https://example.com"}))
    assert "page fetch failed (HTTP 503)" in result["error"] and result["retryable"] is True
    assert "test-secret" not in str(result)


def test_fetch_page_unconfigured(monkeypatch):
    monkeypatch.setattr(module, "settings", SimpleNamespace(exa_api_key=""))
    assert "not configured" in asyncio.run(fetch_tool().ainvoke({"url": "https://example.com"}))["error"]
