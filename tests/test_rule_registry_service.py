"""Tests for RuleRegistryService in-memory CRUD."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

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
