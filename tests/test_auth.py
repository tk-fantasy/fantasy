"""Tests for JWT authentication."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.core.auth import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    verify_token,
)


class TestPasswordHashing:
    def test_hash_password(self):
        """测试密码哈希。"""
        password = "testpassword123"
        hashed = hash_password(password)
        assert hashed != password
        assert len(hashed) > 0

    def test_verify_password_correct(self):
        """测试正确密码验证。"""
        password = "testpassword123"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True

    def test_verify_password_incorrect(self):
        """测试错误密码验证。"""
        password = "testpassword123"
        hashed = hash_password(password)
        assert verify_password("wrongpassword", hashed) is False


class TestJWTTokens:
    def test_create_access_token(self):
        """测试创建访问 token。"""
        user_id = "test-user-id"
        username = "testuser"
        token = create_access_token(user_id, username)
        assert token is not None
        assert len(token) > 0

    def test_create_refresh_token(self):
        """测试创建刷新 token。"""
        user_id = "test-user-id"
        token = create_refresh_token(user_id)
        assert token is not None
        assert len(token) > 0

    def test_verify_access_token(self):
        """测试验证访问 token。"""
        user_id = "test-user-id"
        username = "testuser"
        token = create_access_token(user_id, username)
        payload = verify_token(token)
        assert payload["sub"] == user_id
        assert payload["username"] == username
        assert payload["type"] == "access"

    def test_verify_refresh_token(self):
        """测试验证刷新 token。"""
        user_id = "test-user-id"
        token = create_refresh_token(user_id)
        payload = verify_token(token)
        assert payload["sub"] == user_id
        assert payload["type"] == "refresh"

    def test_verify_invalid_token(self):
        """测试验证无效 token。"""
        from app.core.exceptions import AppException
        with pytest.raises(AppException) as exc_info:
            verify_token("invalid-token")
        assert exc_info.value.http_status == 401


class TestAuthRoutes:
    @pytest.mark.asyncio
    async def test_register_user(self):
        """测试用户注册。"""
        from app.routes.auth_routes import register
        from app.core.database import Database
        from app.schema.api_schemas import AuthRegisterRequest
        from starlette.requests import Request
        from starlette.responses import Response

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
        mock_response = Response()
        payload = AuthRegisterRequest(username="newuser", password="password123")

        with patch("app.routes.auth_routes.Database.get", return_value=mock_db):
            result = await register(mock_request, mock_response, payload)
            assert result.data["user"]["username"] == "newuser"
            # Token 通过 httpOnly cookie 设置，验证 cookie 确实写入 response
            assert "set-cookie" in mock_response.headers

    @pytest.mark.asyncio
    async def test_login_user(self):
        """测试用户登录。"""
        from app.routes.auth_routes import login
        from app.core.database import Database
        from app.core.auth import hash_password
        from app.schema.api_schemas import AuthLoginRequest
        from starlette.requests import Request
        from starlette.responses import Response

        password = "password123"
        hashed = hash_password(password)
        mock_user = {
            "id": "test-user-id",
            "username": "testuser",
            "password_hash": hashed,
            "display_name": "Test User"
        }

        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=mock_user)

        mock_request = AsyncMock(spec=Request)
        mock_request.client = AsyncMock()
        mock_request.client.host = "127.0.0.1"
        mock_response = Response()
        payload = AuthLoginRequest(username="testuser", password=password)

        with patch("app.routes.auth_routes.Database.get", return_value=mock_db):
            result = await login(mock_request, mock_response, payload)
            assert result.data["user"]["username"] == "testuser"
            # Token 通过 httpOnly cookie 设置，验证 cookie 确实写入 response
            assert "set-cookie" in mock_response.headers

    @pytest.mark.asyncio
    async def test_login_wrong_password(self):
        """测试错误密码登录。"""
        from app.routes.auth_routes import login
        from app.core.database import Database
        from app.core.auth import hash_password
        from app.schema.api_schemas import AuthLoginRequest
        from app.core.exceptions import AppException
        from starlette.requests import Request
        from starlette.responses import Response

        password = "password123"
        hashed = hash_password(password)
        mock_user = {
            "id": "test-user-id",
            "username": "testuser",
            "password_hash": hashed,
            "display_name": "Test User"
        }

        mock_db = AsyncMock()
        mock_db.user_get_by_username = AsyncMock(return_value=mock_user)

        mock_request = AsyncMock(spec=Request)
        mock_request.client = AsyncMock()
        mock_request.client.host = "127.0.0.1"
        mock_response = Response()
        payload = AuthLoginRequest(username="testuser", password="wrongpassword")

        with patch("app.routes.auth_routes.Database.get", return_value=mock_db):
            with pytest.raises(AppException) as exc_info:
                await login(mock_request, mock_response, payload)
            assert exc_info.value.http_status == 401

    @pytest.mark.asyncio
    async def test_get_me(self):
        """测试获取当前用户信息。"""
        from app.routes.auth_routes import get_me
        from app.core.database import Database

        mock_user = {
            "id": "test-user-id",
            "username": "testuser",
            "display_name": "Test User"
        }

        mock_db = AsyncMock()
        mock_db.user_get_by_id = AsyncMock(return_value=mock_user)

        current_user = {"user_id": "test-user-id", "username": "testuser"}

        with patch("app.routes.auth_routes.Database.get", return_value=mock_db):
            result = await get_me(current_user)
            assert result.data["username"] == "testuser"
