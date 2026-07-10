"""Tests for model_test_service with mocked httpx."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.model_test_service import test_model_connection as _test_model_connection


class TestModelConnection:
    @pytest.mark.asyncio
    async def test_chat_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await _test_model_connection(
                base_url="http://localhost:11434/v1",
                model="test-model",
                role="chat",
                api_key="test-key",
            )
            assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_chat_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await _test_model_connection(
                base_url="http://localhost:11434/v1",
                model="test-model",
                role="chat",
            )
            assert result["ok"] is False
            assert "401" in result["error"]

    @pytest.mark.asyncio
    async def test_embed_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await _test_model_connection(
                base_url="http://localhost:11434",
                model="embed-model",
                role="embed",
            )
            assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_connection_error(self):
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await _test_model_connection(
                base_url="http://localhost:9999",
                model="test",
                role="chat",
            )
            assert result["ok"] is False
            assert "连接失败" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await _test_model_connection(
                base_url="http://localhost:11434",
                model="test",
                role="chat",
                timeout=5.0,
            )
            assert result["ok"] is False
            assert "超时" in result["error"]
