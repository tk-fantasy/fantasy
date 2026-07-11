"""Tests for per-user settings route — provider save split and GET merge."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_current_user(user_id="user-1", username="tester"):
    return {"user_id": user_id, "username": username}


class TestPostLlmSettingsPerUser:
    """测试 POST /llm/settings 按 role 分流。"""

    @pytest.mark.asyncio
    async def test_chat_role_writes_to_user_db(self):
        """chat 角色保存写入用户 DB，不调 llm_settings_service.apply。"""
        from app.routes.settings_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        saved = {}

        async def mock_save(uid, role, key_id, values):
            saved["user_id"] = uid
            saved["role"] = role
            saved["key_id"] = key_id

        mock_container = MagicMock()
        mock_container.dispatcher = MagicMock()
        mock_container.dispatcher.invalidate_user_agent = MagicMock()
        mock_container.llm_settings_service = MagicMock()
        mock_container.llm_settings_service.apply = MagicMock(return_value={})

        payload = LLMSettingsRequest(role="chat", key_id="key-1")

        with patch("app.routes.settings_routes._save_user_provider", new=AsyncMock(side_effect=mock_save)):
            result = await set_llm_settings(
                payload, current_user=_mock_current_user(), container=mock_container
            )

        # 应写入用户 DB
        assert saved["user_id"] == "user-1"
        assert saved["role"] == "chat"
        assert saved["key_id"] == "key-1"
        # 不应调全局 apply
        mock_container.llm_settings_service.apply.assert_not_called()
        # 应清除 agent 缓存
        mock_container.dispatcher.invalidate_user_agent.assert_called_once_with("user-1")

    @pytest.mark.asyncio
    async def test_vision_role_writes_to_global_config(self):
        """vision 角色保存走全局 config.json（现有逻辑不变）。"""
        from app.routes.settings_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        mock_container = MagicMock()
        mock_container.dispatcher = MagicMock()
        mock_container.llm_settings_service = MagicMock()
        mock_container.llm_settings_service.apply = MagicMock(return_value={"role": "vision", "applied": {}})

        payload = LLMSettingsRequest(role="vision", key_id="key-v")

        with patch("app.routes.settings_routes._save_user_provider", new=AsyncMock()) as mock_save:
            await set_llm_settings(
                payload, current_user=_mock_current_user(), container=mock_container
            )

        # 应调全局 apply
        mock_container.llm_settings_service.apply.assert_called_once()
        # 不应写 per-user DB
        mock_save.assert_not_called()


class TestGetLlmSettingsMerge:
    """测试 GET /llm/settings 合并全局和 per-user。"""

    @pytest.mark.asyncio
    async def test_chat_uses_user_binding_vision_uses_global(self):
        from app.routes.settings_routes import get_llm_settings

        global_settings = {
            "chat": {"key_id": "global-chat", "max_concurrency": 8},
            "summary": {"key_id": "global-sum", "max_concurrency": 8},
            "vision": {"key_id": "global-vis", "max_concurrency": 8},
            "embed": {"key_id": "global-emb", "max_concurrency": 8},
            "stt": {"key_id": "", "max_concurrency": 8},
        }
        user_providers = {
            "chat": {"key_id": "user-chat", "max_concurrency": 4, "enabled": True},
            "stt": {"key_id": "user-stt", "max_concurrency": 8, "enabled": True},
        }

        mock_container = MagicMock()
        mock_container.llm_settings_service = MagicMock()
        mock_container.llm_settings_service.current_settings = MagicMock(return_value=dict(global_settings))
        mock_container.llm_settings_service.warnings = MagicMock(return_value=[])

        with patch("app.routes.settings_routes._get_user_providers", new=AsyncMock(return_value=user_providers)):
            result = await get_llm_settings(
                current_user=_mock_current_user(), container=mock_container
            )

        current = result.data["current"]
        # chat/stt 用用户 DB 覆盖
        assert current["chat"]["key_id"] == "user-chat"
        assert current["stt"]["key_id"] == "user-stt"
        # vision/embed 保持全局
        assert current["vision"]["key_id"] == "global-vis"
        assert current["embed"]["key_id"] == "global-emb"
        # summary 无 per-user 覆盖，保持全局
        assert current["summary"]["key_id"] == "global-sum"


class TestSwitchUserSimplified:
    """测试 switch_user 不再覆盖全局 CONFIG。"""

    @pytest.mark.asyncio
    async def test_switch_user_does_not_touch_global_config(self):
        from app.routes.user_routes import switch_user
        from app.schema.api_schemas import UserSwitchRequest
        from app.core.auth import set_auth_cookies

        target_user = {
            "id": "user-2",
            "username": "alice",
            "password_hash": "$pbkdf2$fake",
            "display_name": "Alice",
        }

        mock_db = MagicMock()
        mock_db.user_get_by_username = AsyncMock(return_value=target_user)

        mock_response = MagicMock()
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")

        payload = UserSwitchRequest(username="alice", password="pass")

        with patch("app.core.database.Database.get", return_value=mock_db), \
             patch("app.routes.user_routes.verify_password", return_value=True), \
             patch("app.routes.user_routes.create_access_token", return_value="token"), \
             patch("app.routes.user_routes.create_refresh_token", return_value="refresh"), \
             patch("app.routes.user_routes.set_auth_cookies") as mock_set_cookies, \
             patch("app.core.config.update_memory_config") as mock_update, \
             patch("app.core.config.write_secrets") as mock_write:
            result = await switch_user(
                mock_request, payload, response=mock_response,
                current_user=_mock_current_user(),
                container=MagicMock(),
            )

        # 不应调 update_memory_config / write_secrets
        mock_update.assert_not_called()
        mock_write.assert_not_called()
        # 应设置 cookie
        mock_set_cookies.assert_called_once()
        # 返回切换信息
        assert result.data["switched_to"] == "alice"
