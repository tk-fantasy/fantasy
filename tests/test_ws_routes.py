"""Tests for ws_routes.py - WebSocket 路由。"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestWebSocketChat:
    """测试 /ws/chat WebSocket。"""

    @pytest.mark.asyncio
    async def test_chat_ws_verify_token_failure(self):
        """WebSocket 聊天 token 验证失败时不 accept。"""
        from app.routes.ws_routes import chat_ws
        from fastapi import WebSocket

        mock_ws = MagicMock(spec=WebSocket)
        mock_ws.accept = AsyncMock()

        with patch("app.main._ws_verify_token", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = None  # Token 验证失败

            await chat_ws(mock_ws)
            mock_ws.accept.assert_not_called()


class TestWebSocketDocChat:
    """测试 /ws/doc/chat WebSocket。"""

    @pytest.mark.asyncio
    async def test_doc_chat_ws_verify_token_failure(self):
        """WebSocket 文档聊天 token 验证失败时不 accept。"""
        from app.routes.ws_routes import doc_chat_ws
        from fastapi import WebSocket

        mock_ws = MagicMock(spec=WebSocket)
        mock_ws.accept = AsyncMock()

        with patch("app.main._ws_verify_token", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = None  # Token 验证失败

            await doc_chat_ws(mock_ws)
            mock_ws.accept.assert_not_called()
