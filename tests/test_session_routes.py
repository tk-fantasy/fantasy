"""Tests for session_routes.py - 聊天和会话管理。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestChatRoute:
    """测试 /api/chat 路由。"""

    @pytest.mark.asyncio
    async def test_chat_success(self):
        """正常聊天返回指令列表。"""
        from app.routes.session_routes import chat
        from app.schema.api_schemas import ChatRequest

        mock_container = MagicMock()
        mock_instruction = MagicMock()
        mock_instruction.model_dump.return_value = {"action": "test"}
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch = AsyncMock(return_value=[mock_instruction])
        mock_container.dispatcher = mock_dispatcher

        current_user = {"user_id": "user-123", "username": "tester"}
        payload = ChatRequest(query="你好", session_id="test-session")
        result = await chat(payload, current_user, container=mock_container)

        assert result.code == "ok"
        assert len(result.data) == 1
        mock_dispatcher.dispatch.assert_called_once()
        # 确认 user_id 透传
        assert mock_dispatcher.dispatch.call_args.kwargs.get("user_id") == "user-123"


class TestSessionRoutes:
    """测试会话管理路由。"""

    @pytest.mark.asyncio
    async def test_create_session(self):
        """创建会话返回会话摘要。"""
        from app.routes.session_routes import create_session

        mock_container = MagicMock()
        mock_session = MagicMock()
        mock_session.summary.return_value = {"session_id": "test-id", "created_at": 123456}
        mock_container.session_store.create_session = AsyncMock(return_value=mock_session)

        current_user = {"user_id": "user-123"}
        result = await create_session(current_user, container=mock_container)

        assert result.code == "ok"
        assert result.data["session_id"] == "test-id"
        mock_container.session_store.create_session.assert_called_once_with(user_id="user-123")

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        """列出会话返回会话列表。"""
        from app.routes.session_routes import list_sessions

        mock_container = MagicMock()
        mock_session = MagicMock()
        mock_session.summary.return_value = {"session_id": "test-id"}
        mock_container.session_store.list_summaries = AsyncMock(return_value=[mock_session])

        current_user = {"user_id": "user-123"}
        result = await list_sessions(current_user, container=mock_container)
        assert result.code == "ok"

    @pytest.mark.asyncio
    async def test_delete_session(self):
        """删除会话成功。"""
        from app.routes.session_routes import delete_session

        mock_container = MagicMock()
        mock_container.session_store.delete_session = AsyncMock(return_value=True)
        # 归属校验需要 get_session 返回属于当前用户的 session
        mock_session = MagicMock()
        mock_session.user_id = "user-123"
        mock_container.session_store.get_session = AsyncMock(return_value=mock_session)

        current_user = {"user_id": "user-123"}
        result = await delete_session("test-session-id", current_user, container=mock_container)

        assert result.code == "ok"
        mock_container.session_store.delete_session.assert_called_once()
