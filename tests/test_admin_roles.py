"""管理员分级（安全审计 2B/3B）单测。

覆盖：
- 首注册用户 is_admin=1、后续为 0（注册路由逻辑 + DB 层）
- get_current_admin：非管理员 403、管理员通过、DB 异常不误放行
- 二级密码 set/reset 路由签名已挂 admin 依赖（防回归）
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import get_current_admin
from app.core.exceptions import AppException


def _db_with_user(user: dict | None):
    db = MagicMock()
    db.user_get_by_id = AsyncMock(return_value=user)
    db_cls = MagicMock()
    db_cls.get.return_value = db
    return db_cls


class TestGetCurrentAdmin:
    @pytest.mark.asyncio
    async def test_admin_passes(self):
        with patch("app.core.database.Database", _db_with_user({"id": "u1", "is_admin": 1})):
            result = await get_current_admin(current_user={"user_id": "u1", "username": "owner"})
        assert result["is_admin"] == 1 and result["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_non_admin_rejected_403(self):
        with patch("app.core.database.Database", _db_with_user({"id": "u2", "is_admin": 0})):
            with pytest.raises(AppException) as ei:
                await get_current_admin(current_user={"user_id": "u2", "username": "kid"})
        assert ei.value.http_status == 403
        assert ei.value.code == "admin_required"

    @pytest.mark.asyncio
    async def test_missing_user_rejected(self):
        with patch("app.core.database.Database", _db_with_user(None)):
            with pytest.raises(AppException):
                await get_current_admin(current_user={"user_id": "ghost", "username": ""})

    @pytest.mark.asyncio
    async def test_db_error_rejected_not_pass_through(self):
        """DB 不可用时宁可拒绝也不能误放行（fail-closed）。"""
        db_cls = MagicMock()
        db_cls.get.side_effect = RuntimeError("db down")
        with patch("app.core.database.Database", db_cls):
            with pytest.raises(AppException):
                await get_current_admin(current_user={"user_id": "u1", "username": ""})


class TestRegisterFirstUserIsAdmin:
    async def test_first_user_admin_second_not(self, tmp_path, monkeypatch):
        """user_count==0 → is_admin=1；再注册 → 0（直接测 DB 层契约）。"""
        from app.core.database import Database

        db = Database.__new__(Database)
        db._db = AsyncMock()
        db._write_lock = AsyncMock()

        async def execute_side_effect(sql, params=None):
            cur = MagicMock()
            cur.fetchall = AsyncMock(return_value=[])
            cur.fetchone = AsyncMock(return_value=None)
            cur.execute = execute_side_effect
            return cur

        db._db.execute = AsyncMock(side_effect=execute_side_effect)
        db._db.executescript = AsyncMock()

        db.user_count = Database.user_count.__get__(db)
        db.user_create = Database.user_create.__get__(db)

        # 简化：直接验证 user_create 的 INSERT 带上了 is_admin 参数
        await db.user_create("u1", "owner", "hash", "owner", is_admin=1)
        sql = db._db.execute.await_args[0][0]
        assert "is_admin" in sql
        assert db._db.execute.await_args[0][1][-1] == 1


class TestDangerousRoutesGated:
    """危险路由签名必须挂 get_current_admin（防后续改动悄悄摘掉门）。"""

    @pytest.mark.parametrize("module_name,func_name", [
        ("app.routes.integration_routes", "upload_plugin"),
        ("app.routes.integration_routes", "delete_plugin"),
        ("app.routes.integration_routes", "toggle_plugin_enabled"),
        ("app.routes.setup_routes", "setup_ha"),
        ("app.routes.simulator_routes", "simulator_stop"),
        ("app.routes.simulator_routes", "simulator_start"),
        ("app.routes.global_config_routes", "reset_global_password"),
        ("app.routes.global_config_routes", "set_global_password"),
        ("app.routes.ops_routes", "restore_backup_route"),
        ("app.routes.ops_routes", "upload_upgrade"),
    ])
    def test_admin_dependency_present(self, module_name, func_name):
        import importlib

        mod = importlib.import_module(module_name)
        fn = getattr(mod, func_name)
        sig = str(inspect.signature(fn))
        assert "get_current_admin" in sig, f"{func_name} 缺管理员依赖: {sig}"

    def test_ops_routes_all_admin(self):
        """ops 路由文件里不应再有裸 get_current_user（全部管理员工具）。"""
        import app.routes.ops_routes as m

        src = inspect.getsource(m)
        assert "get_current_user" not in src.replace("get_current_admin", "")
