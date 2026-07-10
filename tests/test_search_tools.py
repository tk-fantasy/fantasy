"""Tests for search tools (Exa MCP backend)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.search_tools import web_search_handler


def _mcp_response(text: str = "") -> str:
    """构造 Exa MCP JSON-RPC 响应体。text 为空时返回空 content。"""
    return json.dumps({
        "result": {
            "content": [{"type": "text", "text": text}] if text else []
        }
    })


class TestWebSearchHandler:
    @pytest.mark.asyncio
    async def test_empty_query(self):
        result = await web_search_handler({"query": ""}, None)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_successful_search(self):
        with patch("app.mcp.search_tools._get_shared_client") as mock_client:
            mock_response = MagicMock()
            mock_response.text = _mcp_response("Title: Result 1\nURL: http://example.com/1")
            mock_response.raise_for_status = MagicMock()
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            result = await web_search_handler({"query": "test query"}, None)
            assert result["text"] == "Title: Result 1\nURL: http://example.com/1"
            assert result["query"] == "test query"

    @pytest.mark.asyncio
    async def test_exa_http_error(self):
        import httpx
        with patch("app.mcp.search_tools._get_shared_client") as mock_client:
            mock_client.return_value.post = AsyncMock(side_effect=httpx.HTTPError("timeout"))

            result = await web_search_handler({"query": "test"}, None)
            assert "error" in result
            assert "Exa" in result["error"]

    @pytest.mark.asyncio
    async def test_max_results_passed_to_exa(self):
        with patch("app.mcp.search_tools._get_shared_client") as mock_client:
            mock_response = MagicMock()
            mock_response.text = _mcp_response("some text")
            mock_response.raise_for_status = MagicMock()
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            await web_search_handler({"query": "test", "max_results": 3}, None)
            payload = mock_client.return_value.post.call_args.kwargs["json"]
            assert payload["params"]["arguments"]["numResults"] == 3

    @pytest.mark.asyncio
    async def test_no_results(self):
        with patch("app.mcp.search_tools._get_shared_client") as mock_client:
            mock_response = MagicMock()
            mock_response.text = _mcp_response("")  # 空 content
            mock_response.raise_for_status = MagicMock()
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            result = await web_search_handler({"query": "nonexistent"}, None)
            assert result["results"] == []
            assert "message" in result

    @pytest.mark.asyncio
    async def test_max_results_bounds(self):
        with patch("app.mcp.search_tools._get_shared_client") as mock_client:
            mock_response = MagicMock()
            mock_response.text = _mcp_response("some text")
            mock_response.raise_for_status = MagicMock()
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            # max_results > 10 应被截断到 10
            await web_search_handler({"query": "test", "max_results": 20}, None)
            payload = mock_client.return_value.post.call_args.kwargs["json"]
            assert payload["params"]["arguments"]["numResults"] == 10
