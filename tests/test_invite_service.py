"""注册邀请码门控测试。

覆盖 invite_service 单元行为（安装码/邀请码两级校验）与注册路由的门控效果：
- 首用户必须持安装码（堵新部署抢注管理员的窗口期）
- 后续用户必须持管理员签发的一次性邀请码
- 错误信息不区分码状态（不泄露"已使用/已吊销"侧信道）
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.core.exceptions import AppException
from app.services import invite_service


def _mock_db(user_count: int) -> AsyncMock:
    """带 KV 存储语义的最小 Database mock（kv_set 可回读）。"""
    db = AsyncMock()
    db.user_count = AsyncMock(return_value=user_count)
    store: dict[str, str] = {}

    async def kv_get(key: str) -> str | None:
        return store.get(key)

    async def kv_set(key: str, value: str) -> None:
        store[key] = value

    db.kv_get = kv_get
    db.kv_set = kv_set
    db._store = store  # 测试断言用
    return db


class TestSetupCode:
    @pytest.mark.asyncio
    async def test_generated_and_persisted(self):
        db = _mock_db(user_count=0)
        code = await invite_service.get_setup_code(db)
        assert code
        assert db._store["auth_setup_code"] == code
        # 二次读取返回同一个码
        assert await invite_service.get_setup_code(db) == code

    @pytest.mark.asyncio
    async def test_format_avoids_ambiguous_chars(self):
        db = _mock_db(user_count=0)
        code = await invite_service.get_setup_code(db)
        body = code.replace("-", "")
        assert len(body) == 8
        assert all(c in invite_service._CODE_ALPHABET for c in body)

    @pytest.mark.asyncio
    async def test_regenerate_rejected_when_users_exist(self):
        db = _mock_db(user_count=2)
        with pytest.raises(AppException) as ei:
            await invite_service.regenerate_setup_code(db)
        assert ei.value.http_status == 409


class TestRegistrationGate:
    @pytest.mark.asyncio
    async def test_first_user_requires_setup_code(self):
        db = _mock_db(user_count=0)
        await invite_service.get_setup_code(db)  # 码已生成
        with pytest.raises(AppException) as ei:
            await invite_service.verify_registration_code(db, "WRONG-CODE", username="mallory")
        assert ei.value.http_status == 403
        assert ei.value.code == "registration_code_invalid"

    @pytest.mark.asyncio
    async def test_first_user_with_correct_setup_code_passes(self):
        db = _mock_db(user_count=0)
        code = await invite_service.get_setup_code(db)
        # 小写输入也应通过（归一化）
        await invite_service.verify_registration_code(db, code.lower(), username="owner")

    @pytest.mark.asyncio
    async def test_member_requires_invite_code(self):
        db = _mock_db(user_count=1)
        with pytest.raises(AppException) as ei:
            await invite_service.verify_registration_code(db, "", username="kid")
        assert ei.value.http_status == 403

    @pytest.mark.asyncio
    async def test_valid_invite_marks_used_once(self):
        db = _mock_db(user_count=1)
        entry = await invite_service.create_invite(db, created_by="admin", note="给妈妈")
        await invite_service.verify_registration_code(db, entry["code"], username="mom")
        invites = await invite_service.list_invites(db)
        assert invites[0]["used_by"] == "mom"
        assert invites[0]["used_at"] > 0
        # 同一枚码第二次使用被拒
        with pytest.raises(AppException):
            await invite_service.verify_registration_code(db, entry["code"], username="dad")

    @pytest.mark.asyncio
    async def test_revoked_invite_rejected(self):
        db = _mock_db(user_count=1)
        entry = await invite_service.create_invite(db, created_by="admin")
        await invite_service.revoke_invite(db, entry["code"])
        with pytest.raises(AppException):
            await invite_service.verify_registration_code(db, entry["code"], username="kid")

    @pytest.mark.asyncio
    async def test_invalid_vs_used_same_message(self):
        """不存在的码与已使用的码返回同一错误，避免码状态侧信道。"""
        db = _mock_db(user_count=1)
        entry = await invite_service.create_invite(db, created_by="admin")
        await invite_service.verify_registration_code(db, entry["code"], username="mom")

        with pytest.raises(AppException) as e1:
            await invite_service.verify_registration_code(db, "ZZZZ-ZZZZ", username="x")
        with pytest.raises(AppException) as e2:
            await invite_service.verify_registration_code(db, entry["code"], username="y")
        assert e1.value.message == e2.value.message

    @pytest.mark.asyncio
    async def test_revoke_unknown_code_404(self):
        db = _mock_db(user_count=1)
        with pytest.raises(AppException) as ei:
            await invite_service.revoke_invite(db, "NOPE-NOPE")
        assert ei.value.http_status == 404


class TestRegisterRouteGating:
    """注册路由级：门控先于用户写入生效。"""

    def _make_request(self) -> Request:
        mock_request = AsyncMock(spec=Request)
        mock_request.client = AsyncMock()
        mock_request.client.host = "127.0.0.1"
        mock_request.headers = {}
        mock_request.url = MagicMock(scheme="http")
        return mock_request

    @pytest.fixture(autouse=True)
    def _fresh_rate_limiter(self):
        """注册限流器是 auth_routes 模块级单例：本类内多轮注册会耗尽 3 次/分
        预算并污染后续其他测试文件，替换为独立的大额度限流器。"""
        from app.core.rate_limit import RateLimiter
        from app.routes import auth_routes

        with patch.object(auth_routes, "_register_limiter",
                          RateLimiter(max_requests=100, window_seconds=60)):
            yield

    @pytest.mark.asyncio
    async def test_register_without_code_when_users_exist_rejected(self):
        """已有用户后，不带邀请码的注册被 403 拒绝，且不创建用户。"""
        from app.routes.auth_routes import register
        from app.schema.api_schemas import AuthRegisterRequest

        db = _mock_db(user_count=1)
        db.user_get_by_username = AsyncMock(return_value=None)
        db.user_create = AsyncMock()
        payload = AuthRegisterRequest(username="intruder", password="password123")

        with patch("app.routes.auth_routes.Database.get", return_value=db):
            with pytest.raises(AppException) as ei:
                await register(self._make_request(), Response(), payload)
        assert ei.value.http_status == 403
        db.user_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_with_valid_invite_creates_member(self):
        from app.routes.auth_routes import register
        from app.schema.api_schemas import AuthRegisterRequest

        db = _mock_db(user_count=1)
        entry = await invite_service.create_invite(db, created_by="admin")
        db.user_get_by_username = AsyncMock(return_value=None)
        db.user_create = AsyncMock(return_value={
            "id": "member-id", "username": "mom", "display_name": "mom",
        })
        db.user_setting_set = AsyncMock()
        payload = AuthRegisterRequest(
            username="mom", password="password123", code=entry["code"],
        )

        with patch("app.routes.auth_routes.Database.get", return_value=db):
            result = await register(self._make_request(), Response(), payload)
        assert result.data["user"]["username"] == "mom"
        # 非首用户不是管理员
        _, kwargs = db.user_create.call_args
        assert kwargs.get("is_admin") == 0 or db.user_create.call_args[0][4] == 0

    @pytest.mark.asyncio
    async def test_register_first_user_with_setup_code_is_admin(self):
        from app.routes.auth_routes import register
        from app.schema.api_schemas import AuthRegisterRequest

        db = _mock_db(user_count=0)
        code = await invite_service.get_setup_code(db)
        db.user_get_by_username = AsyncMock(return_value=None)
        db.user_create = AsyncMock(return_value={
            "id": "owner-id", "username": "owner", "display_name": "owner",
        })
        db.user_setting_set = AsyncMock()
        payload = AuthRegisterRequest(
            username="owner", password="password123", code=code,
        )

        with patch("app.routes.auth_routes.Database.get", return_value=db):
            result = await register(self._make_request(), Response(), payload)
        assert result.data["user"]["is_admin"] == 1
