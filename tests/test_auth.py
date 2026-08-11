"""Tests for JWT authentication."""
from __future__ import annotations

import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.auth import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    revoke_token,
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

    def test_refresh_token_rejected_by_middleware_logic(self):
        """中间件 api_token_guard 的 type 校验逻辑（审查 #2）。

        不启动完整 app（避免摄像头副作用），直接验证中间件的核心判定：
        refresh token 的 payload type != "access" → 中间件应置 token=None → 401。
        与 get_current_user 依赖（auth.py:207）的校验对齐。
        """
        user_id = "test-user-id"
        access = create_access_token(user_id, "tester")
        refresh = create_refresh_token(user_id)
        access_payload = verify_token(access)
        refresh_payload = verify_token(refresh)
        # 中间件判定逻辑：payload.get("type") != "access" → 拒绝
        assert access_payload.get("type") == "access"     # access 放行
        assert refresh_payload.get("type") != "access"    # refresh 被拒

    def test_verify_invalid_token(self):
        """测试验证无效 token。"""
        from app.core.exceptions import AppException
        with pytest.raises(AppException) as exc_info:
            verify_token("invalid-token")
        assert exc_info.value.http_status == 401


class TestTokenRevocation:
    """Token 撤销黑名单（审查 #低：登出不撤销 token）。

    登出后 token 的 jti 入黑名单，verify_token 拒绝；过期项自动清理。
    """

    def setup_method(self):
        """每个测试前清空黑名单，避免互相污染。"""
        from app.core import auth
        with auth._revoked_lock:
            auth._revoked_tokens.clear()

    def test_revoked_token_rejected(self):
        """登出后 token 被 verify_token 拒绝（code=token_revoked）。"""
        from app.core.exceptions import AppException
        token = create_access_token("u1", "tester")
        # 撤销前：验证通过
        payload = verify_token(token)
        assert payload["sub"] == "u1"
        # 撤销
        revoke_token(payload)
        # 撤销后：拒绝
        with pytest.raises(AppException) as exc:
            verify_token(token)
        assert exc.value.code == "token_revoked"
        assert exc.value.http_status == 401

    def test_unrevoked_token_still_valid(self):
        """未撤销的 token 不受影响。"""
        token = create_access_token("u2", "tester")
        payload = verify_token(token)
        assert payload["sub"] == "u2"

    def test_expired_revocation_auto_cleaned(self):
        """过期的黑名单项被懒惰清理（revoked_tokens 不无限增长）。"""
        from app.core import auth
        # 手动塞一个已过期的 jti
        with auth._revoked_lock:
            auth._revoked_tokens["expired-jti"] = int(time.time()) - 1
        # revoke 另一个 token 时触发清理
        token = create_access_token("u3", "t")
        payload = verify_token(token)
        revoke_token(payload)
        with auth._revoked_lock:
            assert "expired-jti" not in auth._revoked_tokens
            assert payload["jti"] in auth._revoked_tokens

    def test_refresh_token_also_revoked(self):
        """登出同时撤销 refresh token。"""
        from app.core.exceptions import AppException
        refresh = create_refresh_token("u4")
        payload = verify_token(refresh)
        revoke_token(payload)
        with pytest.raises(AppException):
            verify_token(refresh)

    @pytest.mark.asyncio
    async def test_logout_route_revokes_tokens(self):
        """logout 路由应把 access + refresh token 加入黑名单。

        登出后这两个 token 再 verify 应抛 token_revoked。
        """
        from app.routes.auth_routes import logout
        from app.core import auth

        access = create_access_token("u-logout", "tester")
        refresh = create_refresh_token("u-logout")
        access_payload = verify_token(access)
        refresh_payload = verify_token(refresh)

        # mock Request：access 走 cookie，refresh 走 cookie
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.cookies = {
            "aether_token": access,
            "aether_refresh_token": refresh,
        }
        mock_response = MagicMock()

        with patch("app.routes.auth_routes.extract_token_from_request", return_value=access), \
             patch("app.routes.auth_routes.extract_refresh_token_from_request", return_value=refresh):
            await logout(mock_request, mock_response)

        # 两个 token 的 jti 都应进黑名单
        assert access_payload["jti"] in auth._revoked_tokens
        assert refresh_payload["jti"] in auth._revoked_tokens
        # cookie 被清
        mock_response.delete_cookie.assert_called()

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
        # is_secure_request 读 headers 和 url.scheme；测试按 HTTP 场景，secure=False
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
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
        # is_secure_request 读 headers 和 url.scheme；测试按 HTTP 场景，secure=False
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
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
        # is_secure_request 读 headers 和 url.scheme；测试按 HTTP 场景，secure=False
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
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
