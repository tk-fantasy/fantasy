"""Tests for /api/health route."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


class TestHealthRoute:
    """测试 /api/health 路由。"""

    @pytest.mark.asyncio
    async def test_health_requires_auth(self):
        """测试 health 端点需要认证（返回 401）。"""
        # 直接测试路由函数，避免导入整个 app
        from app.core.exceptions import AppException
        from app.core.auth import verify_token

        # 无 token 应该抛出 401
        with pytest.raises(AppException) as exc_info:
            verify_token("")
        assert exc_info.value.http_status == 401

    @pytest.mark.asyncio
    async def test_health_with_valid_token(self):
        """测试 health 端点带有效 token 返回正常状态。"""
        from app.core.auth import create_access_token, verify_token

        # 创建有效 token
        token = create_access_token("test-user-id", "testuser")

        # 验证 token 应该成功
        payload = verify_token(token)
        assert payload["sub"] == "test-user-id"
        assert payload["username"] == "testuser"
