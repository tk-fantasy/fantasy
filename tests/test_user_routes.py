"""Tests for user routes (list, switch, me)."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
        current_user = {"user_id": "1", "username": "admin"}
        payload = UserSwitchRequest(username="user2", password="pass123")

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            with patch("app.routes.user_routes.update_memory_config"):
                with patch("app.routes.user_routes.write_secrets"):
                    with patch("app.routes.user_routes.logger"):
                        result = await switch_user(payload, mock_response, current_user, mock_container)
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
        current_user = {"user_id": "1", "username": "admin"}
        payload = UserSwitchRequest(username="nonexistent", password="pass123")

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            with pytest.raises(AppException) as exc_info:
                await switch_user(payload, mock_response, current_user, mock_container)
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
        current_user = {"user_id": "1", "username": "admin"}
        payload = UserSwitchRequest(username="user2", password="wrong-pass")

        with patch("app.routes.user_routes.Database.get", return_value=mock_db):
            with pytest.raises(AppException) as exc_info:
                await switch_user(payload, mock_response, current_user, mock_container)
            assert exc_info.value.code == "invalid_credentials"
            assert exc_info.value.http_status == 401
