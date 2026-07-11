"""Tests for /api/setup/status route with multi-user support."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.auth import create_access_token, hash_password


class TestSetupStatus:
    """测试 /api/setup/status 路由的多用户支持。"""

    @pytest.mark.asyncio
    async def test_setup_status_new_user_incomplete(self):
        """测试新用户的 setup status 应该返回不完整（引导页应该出现）。"""
        from app.routes.auth_routes import register
        from app.core.database import Database
        from app.schema.api_schemas import AuthRegisterRequest
        from starlette.requests import Request

        # Mock DB
        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=None)
        mock_db.user_create = AsyncMock(return_value={
            "id": "new-user-id",
            "username": "newuser",
            "display_name": "New User"
        })
        mock_db.user_setting_set = AsyncMock()

        mock_request = AsyncMock(spec=Request)
        mock_request.client = AsyncMock()
        mock_request.client.host = "127.0.0.1"
        # is_secure_request 读 headers 和 url.scheme；测试按 HTTP 场景
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
        mock_response = AsyncMock()
        payload = AuthRegisterRequest(username="newuser", password="password123")

        with patch("app.routes.auth_routes.Database.get", return_value=mock_db):
            result = await register(mock_request, mock_response, payload)
            # 验证注册时初始化了 user_settings
            assert mock_db.user_setting_set.call_count == 2
            # 第一次调用是 llm_keys
            first_call_args = mock_db.user_setting_set.call_args_list[0]
            assert first_call_args[0][1] == "llm_keys"
            assert first_call_args[0][2] == "[]"
            # 第二次调用是 providers
            second_call_args = mock_db.user_setting_set.call_args_list[1]
            assert second_call_args[0][1] == "providers"
            assert second_call_args[0][2] == "{}"

    @pytest.mark.asyncio
    async def test_register_initializes_user_settings(self):
        """测试注册时正确初始化 user_settings。"""
        from app.routes.auth_routes import register
        from app.schema.api_schemas import AuthRegisterRequest
        from starlette.requests import Request

        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=None)
        mock_db.user_create = AsyncMock(return_value={
            "id": "test-id",
            "username": "testuser",
            "display_name": "Test"
        })
        mock_db.user_setting_set = AsyncMock()

        mock_request = AsyncMock(spec=Request)
        mock_request.client = AsyncMock()
        mock_request.client.host = "127.0.0.1"
        # is_secure_request 读 headers 和 url.scheme；测试按 HTTP 场景
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
        mock_response = AsyncMock()
        payload = AuthRegisterRequest(username="testuser", password="password123")

        with patch("app.routes.auth_routes.Database.get", return_value=mock_db):
            await register(mock_request, mock_response, payload)

            # 验证调用了 user_setting_set 两次
            assert mock_db.user_setting_set.call_count == 2

            # 验证 llm_keys 初始化为空数组
            llm_keys_call = mock_db.user_setting_set.call_args_list[0]
            assert llm_keys_call[0][1] == "llm_keys"
            parsed = json.loads(llm_keys_call[0][2])
            assert parsed == []

            # 验证 providers 初始化为空对象
            providers_call = mock_db.user_setting_set.call_args_list[1]
            assert providers_call[0][1] == "providers"
            parsed = json.loads(providers_call[0][2])
            assert parsed == {}
