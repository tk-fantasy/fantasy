"""Tests for RuleService.build_rule with mocked LLM client."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.rule_service import RuleService


class TestRuleServiceBuildRule:
    @pytest.mark.asyncio
    async def test_build_valid_rule(self, _patch_config):
        client = MagicMock()
        client.enabled = True
        client.chat = AsyncMock(return_value=json.dumps({
            "condition": "画面里有人",
            "actions": [{"tool_name": "ha_devices___call_service", "parameters": {"domain": "light", "service": "turn_on", "entity_id": "light.bed"}}],
        }))
        svc = RuleService(client=client)
        result = await svc.build_rule("有人时开灯")
        assert "condition" in result

    @pytest.mark.asyncio
    async def test_build_invalid_json_fallback(self, _patch_config):
        client = MagicMock()
        client.enabled = True
        client.chat = AsyncMock(return_value="not json")
        svc = RuleService(client=client)
        result = await svc.build_rule("有人时开灯")
        assert "condition" in result

    @pytest.mark.asyncio
    async def test_disabled_client_fallback(self, _patch_config):
        client = MagicMock()
        client.enabled = False
        svc = RuleService(client=client)
        result = await svc.build_rule("有人时开灯")
        assert "condition" in result
