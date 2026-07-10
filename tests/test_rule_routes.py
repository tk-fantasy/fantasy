"""Tests for rule_routes.py - 规则 CRUD。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestRuleRoutes:
    """测试规则管理路由。"""

    @pytest.mark.asyncio
    async def test_list_rules(self):
        """列出规则返回规则列表。"""
        from app.routes.rule_routes import list_rules

        mock_container = MagicMock()
        mock_container.rule_registry_service.list_rules.return_value = [
            {"id": "rule-1", "name": "测试规则"}
        ]

        result = await list_rules(container=mock_container)
        assert result.code == "ok"

    @pytest.mark.asyncio
    async def test_delete_rule(self):
        """删除规则成功。"""
        from app.routes.rule_routes import delete_rule

        mock_container = MagicMock()
        mock_container.rule_registry_service.delete_rule.return_value = True

        result = await delete_rule("rule-123", container=mock_container)
        assert result.code == "ok"

    @pytest.mark.asyncio
    async def test_set_rule_enabled(self):
        """切换规则启用状态。"""
        from app.routes.rule_routes import set_rule_enabled
        from app.schema.api_schemas import RuleEnabledRequest

        mock_container = MagicMock()
        mock_container.rule_registry_service.set_enabled.return_value = True

        payload = RuleEnabledRequest(enabled=True)
        result = await set_rule_enabled("rule-123", payload, container=mock_container)
        assert result.code == "ok"


class TestBuildRuleRoute:
    """测试 /api/task/rule 路由。"""

    @pytest.mark.asyncio
    async def test_build_rule_success(self):
        """创建规则任务成功。"""
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        mock_container = MagicMock()
        mock_container.rule_service.build_rule = AsyncMock(return_value={
            "name": "测试规则",
            "condition": "晚上",
            "actions": [],
        })
        mock_container.rule_registry_service.add_rule.return_value = {
            "id": "rule-1",
            "name": "测试规则",
        }

        payload = RuleCreateRequest(text="晚上开灯")
        result = await build_rule(payload, container=mock_container)
        assert result.code == "ok"
