import json

import httpx
import pytest

from app.worker.tools.web_search import WebSearchUnavailable, web_search


@pytest.mark.asyncio
async def test_web_search_returns_only_actual_provider_results():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["query"] == "agent orchestration"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "A real title",
                        "url": "https://example.org/article",
                        "content": "A real provider snippet",
                    },
                    {"title": "invalid", "url": "javascript:alert(1)", "content": "x"},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        results = await web_search("  agent   orchestration  ", api_key="test-key", client=client)
    finally:
        await client.aclose()

    assert results == [
        {
            "title": "A real title",
            "url": "https://example.org/article",
            "snippet": "A real provider snippet",
        }
    ]


@pytest.mark.asyncio
async def test_web_search_fails_closed_without_provider_key():
    with pytest.raises(WebSearchUnavailable, match="TAVILY_API_KEY"):
        await web_search("query", api_key="")
