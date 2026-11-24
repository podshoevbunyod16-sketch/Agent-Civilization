"""Real web search tool for the trusted demo worker process."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

import httpx

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_SEARCH_WINDOW_SECONDS = 60 * 60
_MAX_SEARCH_CALLS_PER_HOUR = 12
_SEARCH_CALLS: deque[float] = deque()
_SEARCH_LOCK = asyncio.Lock()


class WebSearchUnavailable(RuntimeError):
    """Raised when the web-search integration is not configured or fails."""


async def _reserve_search_slot() -> None:
    """Bound demo-provider spend per worker process, including failed attempts."""
    now = time.monotonic()
    async with _SEARCH_LOCK:
        while _SEARCH_CALLS and now - _SEARCH_CALLS[0] >= _SEARCH_WINDOW_SECONDS:
            _SEARCH_CALLS.popleft()
        if len(_SEARCH_CALLS) >= _MAX_SEARCH_CALLS_PER_HOUR:
            raise WebSearchUnavailable("Достигнут лимит демо web_search: 12 запросов в час")
        _SEARCH_CALLS.append(now)


async def web_search(
    query: str,
    *,
    api_key: str,
    client: httpx.AsyncClient | None = None,
    max_results: int = 5,
) -> list[dict[str, str]]:
    """Return provider snippets; never replace errors with invented results."""
    cleaned_query = " ".join(query.split())[:500]
    if not cleaned_query:
        raise WebSearchUnavailable("Для web_search нужен непустой query")
    if not api_key.strip():
        raise WebSearchUnavailable("TAVILY_API_KEY не задан в окружении worker")
    await _reserve_search_slot()
    result_limit = max(1, min(max_results, 5))

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    try:
        response = await http.post(
            TAVILY_SEARCH_URL,
            json={
                "api_key": api_key,
                "query": cleaned_query,
                "search_depth": "basic",
                "max_results": result_limit,
                "include_answer": False,
                "include_raw_content": False,
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WebSearchUnavailable(f"Поиск не выполнен: {type(exc).__name__}") from exc
    finally:
        if owns_client:
            await http.aclose()

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise WebSearchUnavailable("Провайдер вернул ответ без списка results")

    normalized: list[dict[str, str]] = []
    for item in results[:result_limit]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:300]
        url = str(item.get("url") or "").strip()[:2000]
        content = str(item.get("content") or "").strip()[:1200]
        if url.startswith(("https://", "http://")):
            normalized.append({"title": title, "url": url, "snippet": content})
    return normalized
