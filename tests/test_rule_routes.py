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
        """创建规则任务成功，user_id 从 current_user 注入。"""
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
        current_user = {"user_id": "u1", "username": "alice"}
        result = await build_rule(payload, container=mock_container, current_user=current_user)
        assert result.code == "ok"

        # build_rule 必须带 user_id（per-user chat key 解析依赖它）
        mock_container.rule_service.build_rule.assert_awaited_once_with("晚上开灯", user_id="u1")
        # add_rule 是同步方法，必须带 user_id（持久化到 rules.user_id 列）
        mock_container.rule_registry_service.add_rule.assert_called_once()
        assert mock_container.rule_registry_service.add_rule.call_args.kwargs.get("user_id") == "u1"

    @pytest.mark.asyncio
    async def test_build_rule_no_condition_returns_error(self):
        """LLM 解析不出 condition → 返回失败，不调 add_rule。"""
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        mock_container = MagicMock()
        mock_container.rule_service.build_rule = AsyncMock(return_value={
            "name": "x", "condition": "", "actions": [],
        })

        payload = RuleCreateRequest(text="无效输入")
        current_user = {"user_id": "u1", "username": "alice"}
        result = await build_rule(payload, container=mock_container, current_user=current_user)
        # 失败时 message 为错误说明（code 仍是 "ok"，data 为 None）
        assert result.data is None
        assert "无法从输入中解析出" in result.message
        mock_container.rule_registry_service.add_rule.assert_not_called()


class TestCreateRuleRoute:
    """测试 POST /api/rules 路由（手动构造规则，不走 LLM）。"""

    @pytest.mark.asyncio
    async def test_create_rule_injects_user_id(self):
        """create_rule 必须把 current_user.user_id 传给 add_rule。"""
        from app.routes.rule_routes import create_rule
        from app.schema.api_schemas import RulePayloadRequest

        mock_container = MagicMock()
        mock_container.rule_registry_service.add_rule.return_value = {"id": "r1"}

        payload = RulePayloadRequest(condition="晚上10点后", actions=[])
        current_user = {"user_id": "u2", "username": "bob"}
        result = await create_rule(payload, container=mock_container, current_user=current_user)
        assert result.code == "ok"
        mock_container.rule_registry_service.add_rule.assert_called_once()
        assert mock_container.rule_registry_service.add_rule.call_args.kwargs.get("user_id") == "u2"

