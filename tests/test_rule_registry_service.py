"""Tests for RuleRegistryService in-memory CRUD."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.rule_registry_service import RuleRegistryService


class TestRuleRegistryCRUD:
    def setup_method(self):
        self.svc = RuleRegistryService()

    def test_add_rule(self):
        rule = {"name": "test", "condition": "有人", "actions": [], "enabled": True, "cooldown_seconds": 10}
        with patch.object(self.svc, "_insert_rule_async"):
            result = self.svc.add_rule(rule)
        assert result["name"] == "test"
        assert "id" in result

    def test_list_rules(self):
        rule = {"name": "test", "condition": "有人", "actions": []}
        with patch.object(self.svc, "_insert_rule_async"):
            self.svc.add_rule(rule)
        rules = self.svc.list_rules()
        assert len(rules) == 1

    def test_delete_rule(self):
        rule = {"name": "test", "condition": "有人", "actions": []}
        with patch.object(self.svc, "_insert_rule_async"):
            added = self.svc.add_rule(rule)
        with patch.object(self.svc, "_delete_rule_async"):
            result = self.svc.delete_rule(added["id"])
        assert isinstance(result, dict)
        assert result["name"] == "test"
        assert len(self.svc.list_rules()) == 0

    def test_delete_nonexistent(self):
        from app.core.exceptions import AppException
        with patch.object(self.svc, "_delete_rule_async"):
            with pytest.raises(AppException):
                self.svc.delete_rule("nonexistent")

    def test_set_enabled(self):
        rule = {"name": "test", "condition": "有人", "actions": []}
        with patch.object(self.svc, "_insert_rule_async"):
            added = self.svc.add_rule(rule)
        with patch.object(self.svc, "_save_rule_async"):
            result = self.svc.set_enabled(added["id"], False)
        assert result["enabled"] is False

    def test_set_enabled_nonexistent(self):
        from app.core.exceptions import AppException
        with patch.object(self.svc, "_save_rule_async"):
            with pytest.raises(AppException):
                self.svc.set_enabled("nonexistent", False)


class TestRuleRegistryUserId:
    """user_id 持久化链路：add_rule → AutomationRule.user_id → to_dict → _insert_rule_async。"""

    def setup_method(self):
        self.svc = RuleRegistryService()

    def test_add_rule_carries_user_id_param(self):
        """add_rule(rule, user_id=...) → 生成的 AutomationRule 带 user_id，to_dict 输出 user_id。"""
        rule = {"name": "test", "condition": "有人", "actions": []}
        with patch.object(self.svc, "_insert_rule_async") as mock_insert:
            result = self.svc.add_rule(rule, user_id="u1")
        assert result["user_id"] == "u1"
        # _insert_rule_async 接收的 AutomationRule.user_id 必须是 u1
        inserted_rule = mock_insert.call_args.args[0]
        assert inserted_rule.user_id == "u1"

    def test_add_rule_user_id_falls_back_to_rule_dict(self):
        """不传 user_id 参数时，回退读 rule dict 里的 user_id（兼容老调用方）。"""
        rule = {"name": "test", "condition": "有人", "actions": [], "user_id": "from-dict"}
        with patch.object(self.svc, "_insert_rule_async") as mock_insert:
            result = self.svc.add_rule(rule)
        assert result["user_id"] == "from-dict"
        assert mock_insert.call_args.args[0].user_id == "from-dict"

    def test_add_rule_no_user_id_defaults_empty(self):
        """无 user_id（老调用）→ user_id=''，不破坏现有行为。"""
        rule = {"name": "test", "condition": "有人", "actions": []}
        with patch.object(self.svc, "_insert_rule_async"):
            result = self.svc.add_rule(rule)
        assert result["user_id"] == ""

    def test_to_dict_includes_user_id(self):
        """to_dict 必须输出 user_id，否则 load_from_db 读不回来。"""
        from app.services.rule_registry_service import AutomationRule
        rule = AutomationRule(
            id="r1", trigger={}, conditions=[], actions=[], summary="",
            enabled=True, created_at=0, updated_at=0, user_id="u-to-dict",
        )
        d = rule.to_dict()
        assert d["user_id"] == "u-to-dict"

    @pytest.mark.asyncio
    async def test_insert_rule_async_passes_user_id_to_db(self):
        """_insert_rule_async 必须把 rule.user_id 传给 db.rules_insert 第三参数。"""
        from app.services.rule_registry_service import AutomationRule
        rule = AutomationRule(
            id="r1", trigger={}, conditions=[], actions=[], summary="",
            enabled=True, created_at=0, updated_at=0, user_id="u-db",
        )
        with patch("app.services.rule_registry_service.Database") as MockDB:
            mock_db = MagicMock()
            mock_db.rules_insert = AsyncMock()
            MockDB.get.return_value = mock_db
            # _spawn_task 会试图创建 asyncio task，需要 running loop
            with patch.object(self.svc, "_spawn_task") as mock_spawn:
                self.svc._insert_rule_async(rule)
                # _spawn_task 被调，参数是 db.rules_insert(...) coroutine
                mock_spawn.assert_called_once()
                coro = mock_spawn.call_args.args[0]
                # 跑完 coroutine 验证 rules_insert 被调时带 user_id="u-db"
                await coro
                mock_db.rules_insert.assert_awaited_once()
                args = mock_db.rules_insert.call_args.args
                assert args[0] == "r1"
                assert args[2] == "u-db"  # 第三参数 user_id

