"""Tests for user routes (list, switch, me)."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.requests import Request

from app.core.auth import hash_password


class TestUserRoutes:
    """测试用户管理路由。"""

    @pytest.mark.asyncio
    async def test_list_users(self):
        """测试获取用户列表。"""
        from app.routes.user_routes import list_users

        mock_users = [
            {"id": "1", "username": "admin", "display_name": "Admin", "created_at": 1000},
            {"id": "2", "username": "user2", "display_name": "User 2", "created_at": 2000},
        ]

        mock_db = AsyncMock()
        mock_db.user_list_all = AsyncMock(return_value=mock_users)
        # list_users filters by llm_keys — return valid JSON for each user
        mock_db.user_setting_get = AsyncMock(return_value='[{"id": "key1"}]')

        current_user = {"user_id": "1", "username": "admin"}

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            result = await list_users(current_user)
            assert len(result.data) == 2
            assert result.data[0]["username"] == "admin"

    @pytest.mark.asyncio
    async def test_get_current_user_info(self):
        """测试获取当前用户信息。"""
        from app.routes.user_routes import get_current_user_info

        mock_user = {
            "id": "1",
            "username": "admin",
            "display_name": "Admin",
            "created_at": 1000
        }

        mock_db = AsyncMock()
        mock_db.user_get_by_id = AsyncMock(return_value=mock_user)

        current_user = {"user_id": "1", "username": "admin"}

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            result = await get_current_user_info(current_user)
            assert result.data["username"] == "admin"

    @pytest.mark.asyncio
    async def test_get_current_user_info_not_found(self):
        """测试获取不存在的用户信息。"""
        from app.routes.user_routes import get_current_user_info
        from app.core.exceptions import AppException

        mock_db = AsyncMock()
        mock_db.user_get_by_id = AsyncMock(return_value=None)

        current_user = {"user_id": "999", "username": "ghost"}

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            with pytest.raises(AppException) as exc_info:
                await get_current_user_info(current_user)
            assert exc_info.value.code == "user_not_found"

    @pytest.mark.asyncio
    async def test_switch_user_success(self):
        """测试切换用户成功。"""
        from app.routes.user_routes import switch_user
        from app.schema.api_schemas import UserSwitchRequest

        target_user = {
            "id": "2",
            "username": "user2",
            "display_name": "User 2",
            "password_hash": hash_password("pass123"),
            "created_at": 2000
        }

        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=target_user)
        mock_db.user_setting_get = AsyncMock(return_value=None)

        mock_container = MagicMock()
        mock_response = MagicMock()
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
        current_user = {"user_id": "1", "username": "admin"}
        payload = UserSwitchRequest(username="user2", password="pass123")

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            with patch("app.routes.user_routes.update_memory_config"):
                with patch("app.routes.user_routes.write_secrets"):
                    with patch("app.routes.user_routes.logger"):
                        result = await switch_user(mock_request, payload, mock_response, current_user, mock_container)
                        assert result.data["switched_to"] == "user2"

    @pytest.mark.asyncio
    async def test_switch_user_not_found(self):
        """测试切换到不存在的用户。"""
        from app.routes.user_routes import switch_user
        from app.schema.api_schemas import UserSwitchRequest
        from app.core.exceptions import AppException

        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=None)

        mock_container = MagicMock()
        mock_response = MagicMock()
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
        current_user = {"user_id": "1", "username": "admin"}
        payload = UserSwitchRequest(username="nonexistent", password="pass123")

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            with pytest.raises(AppException) as exc_info:
                await switch_user(mock_request, payload, mock_response, current_user, mock_container)
            assert exc_info.value.code == "user_not_found"

    @pytest.mark.asyncio
    async def test_switch_user_empty_username(self):
        """测试切换用户时用户名为空 — Pydantic min_length=1 校验。"""
        from app.schema.api_schemas import UserSwitchRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UserSwitchRequest(username="", password="pass123")

    @pytest.mark.asyncio
    async def test_switch_user_wrong_password(self):
        """测试切换用户密码错误 — 返回 401。"""
        from app.routes.user_routes import switch_user
        from app.schema.api_schemas import UserSwitchRequest
        from app.core.exceptions import AppException

        target_user = {
            "id": "2",
            "username": "user2",
            "password_hash": hash_password("correct-pass"),
            "created_at": 2000,
        }
        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=target_user)

        mock_container = MagicMock()
        mock_response = MagicMock()
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
        current_user = {"user_id": "1", "username": "admin"}
        payload = UserSwitchRequest(username="user2", password="wrong-pass")

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            with pytest.raises(AppException) as exc_info:
                await switch_user(mock_request, payload, mock_response, current_user, mock_container)
            assert exc_info.value.code == "invalid_credentials"
            assert exc_info.value.http_status == 401


class TestSaveUserLlmKeysFiltersGlobalRoles:
    """测试 save_user_llm_keys 按 PER_USER_ROLES 过滤。

    per-user DB 仅存 chat/summary/stt；vision/embed 全局共享不进用户 DB。
    但 update_memory_config / write_secrets 仍用完整 keys，保持全局 CONFIG
    的 embed/vision 不被冲掉。
    """

    @pytest.mark.asyncio
    async def test_filters_embed_vision_from_per_user_db(self):
        from app.routes.user_routes import save_user_llm_keys
        from app.schema.api_schemas import UserLLMKeysRequest

        user = {"id": "u1", "username": "admin"}
        keys = [
            {"id": "k1", "type": "chat", "api_key": "c1", "api_key_env": "CHAT_ENV"},
            {"id": "k2", "type": "embed", "api_key": "e1", "api_key_env": "EMBED_ENV"},
            {"id": "k3", "type": "vision", "api_key": "v1", "api_key_env": "VISION_ENV"},
            {"id": "k4", "type": "summary", "api_key": "s1", "api_key_env": "SUMMARY_ENV"},
        ]
        payload = UserLLMKeysRequest(keys=keys)

        stored = {}
        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=user)

        async def mock_set(uid, key, value):
            stored[key] = value
        mock_db.user_setting_set = AsyncMock(side_effect=mock_set)

        mock_container = MagicMock()
        current_user = {"user_id": "u1", "username": "admin"}

        with patch("app.routes.user_routes.Database.get", return_value=mock_db), \
             patch("app.routes.user_routes.update_memory_config") as mock_mem, \
             patch("app.routes.user_routes.write_secrets") as mock_env, \
             patch("app.routes.user_routes.get_container", return_value=mock_container):
            result = await save_user_llm_keys(
                username="admin", payload=payload,
                current_user=current_user, container=mock_container,
            )

        # per-user DB 只存 chat/summary
        synced = json.loads(stored["llm_keys"])
        types = [k["type"] for k in synced]
        assert "embed" not in types
        assert "vision" not in types
        assert set(types) == {"chat", "summary"}
        # update_memory_config 用完整 keys（含 embed/vision，保持全局 CONFIG）
        mock_mem.assert_called_once()
        mem_arg = mock_mem.call_args[0][1]
        assert {k["type"] for k in mem_arg} == {"chat", "embed", "vision", "summary"}
        # write_secrets 也收到全部 env
        env_arg = mock_env.call_args[0][0]
        assert "EMBED_ENV" in env_arg and "VISION_ENV" in env_arg
        # 返回 count 是 per-user 数量（2）
        assert result.data["count"] == 2

    @pytest.mark.asyncio
    async def test_keeps_all_per_user_roles(self):
        from app.routes.user_routes import save_user_llm_keys
        from app.schema.api_schemas import UserLLMKeysRequest

        user = {"id": "u1", "username": "admin"}
        keys = [
            {"id": "k1", "type": "chat", "api_key": "c1"},
            {"id": "k2", "type": "summary", "api_key": "s1"},
            {"id": "k3", "type": "stt", "api_key": "t1"},
        ]
        payload = UserLLMKeysRequest(keys=keys)

        stored = {}
        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=user)

        async def mock_set(uid, key, value):
            stored[key] = value
        mock_db.user_setting_set = AsyncMock(side_effect=mock_set)

        mock_container = MagicMock()
        current_user = {"user_id": "u1", "username": "admin"}

        with patch("app.routes.user_routes.Database.get", return_value=mock_db), \
             patch("app.routes.user_routes.update_memory_config"), \
             patch("app.routes.user_routes.write_secrets"), \
             patch("app.routes.user_routes.get_container", return_value=mock_container):
            result = await save_user_llm_keys(
                username="admin", payload=payload,
                current_user=current_user, container=mock_container,
            )

        synced = json.loads(stored["llm_keys"])
        types = {k["type"] for k in synced}
        assert types == {"chat", "summary", "stt"}
        assert result.data["count"] == 3
