"""Tests for RuleService pure methods."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.rule_service import RuleService


class TestParseJson:
    def setup_method(self):
        self.svc = RuleService.__new__(RuleService)

    def test_valid(self):
        result = self.svc._parse_json('{"condition": "test"}')
        assert result == {"condition": "test"}

    def test_invalid(self):
        result = self.svc._parse_json("not json")
        assert result == {}


class TestParseHaCatalog:
    def setup_method(self):
        self.svc = RuleService.__new__(RuleService)

    def test_basic(self):
        catalog = "- light.bed (类型:light, 状态:on) 名称:床头灯\n- light.kitchen (类型:light, 状态:off) 名称:厨房灯"
        devices = self.svc._parse_ha_catalog(catalog)
        assert len(devices) == 2
        assert devices[0]["entity_id"] == "light.bed"
        assert devices[0]["name"] == "床头灯"

    def test_empty(self):
        assert self.svc._parse_ha_catalog("") == []
        assert self.svc._parse_ha_catalog("(暂无 HA 设备)") == []


class TestFindMatchingEntity:
    def setup_method(self):
        self.svc = RuleService.__new__(RuleService)
        self.devices = [
            {"entity_id": "light.chuang_tou_deng", "name": "床头灯", "domain": "light"},
            {"entity_id": "light.chu_fang_deng", "name": "厨房灯", "domain": "light"},
            {"entity_id": "climate.ke_ting_kong_tiao", "name": "客厅空调", "domain": "climate"},
        ]


# ---------------------------------------------------------------------------
# build_rule per-user 化：按 user_id 解析 chat key，无配置回退全局
# ---------------------------------------------------------------------------

class TestRuleServicePerUser:
    """build_rule per-user 化：与 scheduler_service._resolve_reminder_client 同一模式。"""

    def _make_svc(self, global_enabled=True):
        """构造带 mock 全局 client 的 RuleService。"""
        global_client = MagicMock()
        global_client.enabled = global_enabled
        global_client.chat = AsyncMock(return_value='{"condition":"晚上","actions":[],"name":"r"}')
        svc = RuleService(client=global_client)
        return svc, global_client

    @pytest.mark.asyncio
    async def test_build_rule_uses_per_user_chat_key(self):
        """有 user_id 且用户有 per-user chat key → 构造 per-user LlmChatClient，全局 client 不被调。"""
        svc, global_client = self._make_svc()

        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)):
            with patch("app.clients.llm_chat_client.LlmChatClient") as MockClient:
                mock_instance = MagicMock()
                mock_instance.chat = AsyncMock(return_value='{"condition":"晚上","actions":[],"name":"r"}')
                MockClient.return_value = mock_instance

                await svc.build_rule("晚上开灯", user_id="u-per-user")

        # per-user client 被构造（role=chat）
        MockClient.assert_called_with(role="chat")
        # per-user client 的 chat 被调，全局 client 不该被调
        mock_instance.chat.assert_awaited()
        global_client.chat.assert_not_awaited()
        # per-user key 覆盖了私有字段
        assert mock_instance._api_key == "per-user-secret"
        assert mock_instance._base_url == "https://per-user.example.com/v1"
        assert mock_instance._model == "per-user-model"
        assert mock_instance._enabled is True  # 关键坑：_enabled 必须覆盖

    @pytest.mark.asyncio
    async def test_build_rule_falls_back_to_global_when_no_per_user_key(self):
        """有 user_id 但用户无 per-user chat key → 回退全局 self._client。"""
        svc, global_client = self._make_svc()

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)):
            result = await svc.build_rule("晚上开灯", user_id="u-no-config")

        global_client.chat.assert_awaited()
        assert "condition" in result

    @pytest.mark.asyncio
    async def test_build_rule_no_user_id_uses_global(self):
        """无 user_id（老调用）→ 直接走全局 self._client，resolve 不被调。"""
        svc, global_client = self._make_svc()

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)) as mock_resolve:
            await svc.build_rule("晚上开灯")

        mock_resolve.assert_not_awaited()
        global_client.chat.assert_awaited()

    @pytest.mark.asyncio
    async def test_build_rule_per_user_key_overrides_disabled_global(self):
        """全局 client.enabled=False，但有 per-user key → 仍走 per-user client。

        验证"先 resolve 再检查 enabled"的坑：build_rule 第一行不再硬看全局 enabled，
        而是先解析 per-user client（_enabled=True），绕过全局占位符禁用态。
        """
        svc, global_client = self._make_svc(global_enabled=False)

        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)):
            with patch("app.clients.llm_chat_client.LlmChatClient") as MockClient:
                mock_instance = MagicMock()
                mock_instance.chat = AsyncMock(return_value='{"condition":"晚上","actions":[],"name":"r"}')
                MockClient.return_value = mock_instance

                result = await svc.build_rule("晚上开灯", user_id="u-per-user")

        # per-user client 被调（绕过了全局 disabled 的早返回）
        mock_instance.chat.assert_awaited()
        # 不会走 fallback（fallback 的 condition 是空字符串）
        assert result.get("condition") == "晚上"

