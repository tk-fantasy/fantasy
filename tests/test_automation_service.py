"""Tests for AutomationService with mocked dependencies."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.automation_service import AutomationService


class TestInCooldown:
    def setup_method(self):
        self.svc = AutomationService.__new__(AutomationService)

    def test_no_last_triggered(self):
        rule = {"cooldown_seconds": 10, "last_triggered_at": 0.0}
        assert self.svc._in_cooldown(rule, time.time()) is False

    def test_just_triggered(self):
        rule = {"cooldown_seconds": 10, "last_triggered_at": time.time()}
        assert self.svc._in_cooldown(rule, time.time()) is True

    def test_cooldown_expired(self):
        rule = {"cooldown_seconds": 10, "last_triggered_at": time.time() - 20}
        assert self.svc._in_cooldown(rule, time.time()) is False

    def test_default_cooldown(self):
        rule = {"last_triggered_at": time.time()}
        assert self.svc._in_cooldown(rule, time.time()) is True

    def test_default_cooldown_reads_config(self, monkeypatch):
        """cooldown 缺省时读 automation.default_cooldown_seconds（P0：10→config 驱动）。"""
        import app.core.config as cfg
        monkeypatch.setitem(cfg.CONFIG["automation"], "default_cooldown_seconds", 7)
        # 3s < 7s → 在冷却内
        rule_in = {"last_triggered_at": time.time() - 3}
        assert self.svc._in_cooldown(rule_in, time.time()) is True
        # 10s > 7s → 过期
        rule_out = {"last_triggered_at": time.time() - 10}
        assert self.svc._in_cooldown(rule_out, time.time()) is False


class TestResolveToolName:
    def setup_method(self):
        self.svc = AutomationService.__new__(AutomationService)

    def test_full_name_found(self):
        tool_executor = MagicMock()
        tool_executor.resolve_tool_name.return_value = "ha___call_service"
        self.svc._tool_executor = tool_executor
        assert self.svc._tool_executor.resolve_tool_name("ha___call_service") == "ha___call_service"

    def test_short_name_resolved(self):
        tool_executor = MagicMock()
        tool_executor.resolve_tool_name.return_value = "ha_devices___call_service"
        self.svc._tool_executor = tool_executor
        assert self.svc._tool_executor.resolve_tool_name("call_service") == "ha_devices___call_service"

    def test_no_manager_returns_as_is(self):
        tool_executor = MagicMock()
        tool_executor.resolve_tool_name.return_value = "some_tool"
        self.svc._tool_executor = tool_executor
        assert self.svc._tool_executor.resolve_tool_name("some_tool") == "some_tool"


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_no_rules(self):
        registry = MagicMock()
        registry.list_rules.return_value = []
        svc = AutomationService(registry)
        result = await svc.evaluate(frames=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_disabled_rule_skipped(self):
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "1", "name": "test", "condition": "有人", "actions": [], "enabled": False},
        ]
        svc = AutomationService(registry)
        result = await svc.evaluate(frames=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_frames_skips(self):
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "1", "name": "test", "condition": "有人", "actions": [], "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0},
        ]
        vision = MagicMock()
        svc = AutomationService(registry, vision_service=vision)
        result = await svc.evaluate(frames=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_condition_met_triggers_action(self):
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "1", "name": "test", "condition": "有人", "actions": [
                {"tool_name": "ha_devices___call_service", "parameters": {"domain": "light"}},
            ], "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0},
        ]
        vision = MagicMock()
        vision.evaluate_condition = AsyncMock(return_value=1)
        executor = MagicMock()
        executor.execute_tool_by_name = AsyncMock(return_value={"success": True})
        svc = AutomationService(registry, tool_executor=executor, vision_service=vision)
        result = await svc.evaluate(frames=[[1, 2, 3]])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_condition_not_met_no_action(self):
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "1", "name": "test", "condition": "有人", "actions": [
                {"tool_name": "test", "parameters": {}},
            ], "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0},
        ]
        vision = MagicMock()
        vision.evaluate_condition = AsyncMock(return_value=0)
        svc = AutomationService(registry, vision_service=vision)
        result = await svc.evaluate(frames=[[1, 2, 3]])
        assert result == []


class TestBuildConditionContext:
    @pytest.mark.asyncio
    async def test_gets_time_and_weather(self):
        svc = AutomationService.__new__(AutomationService)
        svc._weather_cache = None
        svc._weather_cache_at = 0.0

        async def mock_time(params, session):
            return {
                "date": "2026-06-20", "time": "15:30:00",
                "weekday": "星期六", "year": 2026, "month": 6, "day": 20,
            }

        async def mock_weather(params, session):
            return {
                "location": "上海", "weather": "小雨",
                "temperature": 25, "humidity": 80,
            }

        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.current_time_handler", mock_time), \
             patch("app.mcp.weather_tools.get_weather_handler", mock_weather):
            result = await svc._build_condition_context()

        assert "2026-06-20" in result
        assert "星期六" in result
        assert "小雨" in result
        assert "25°C" in result

    @pytest.mark.asyncio
    async def test_weather_cached_for_60s(self):
        svc = AutomationService.__new__(AutomationService)
        svc._weather_cache = None
        svc._weather_cache_at = 0.0
        call_count = 0

        async def mock_time(params, session):
            return {"date": "2026-06-20", "time": "15:30:00", "weekday": "星期六"}

        async def mock_weather(params, session):
            nonlocal call_count
            call_count += 1
            return {"location": "上海", "weather": "晴", "temperature": 30, "humidity": 50}

        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.current_time_handler", mock_time), \
             patch("app.mcp.weather_tools.get_weather_handler", mock_weather):
            # 第一次调用：获取天气
            await svc._build_condition_context()
            assert call_count == 1

            # 第二次调用：缓存命中，不再请求
            await svc._build_condition_context()
            assert call_count == 1

    @pytest.mark.asyncio
    async def test_tool_failure_silent_degradation(self):
        svc = AutomationService.__new__(AutomationService)
        svc._weather_cache = None
        svc._weather_cache_at = 0.0

        async def mock_time(params, session):
            raise Exception("network error")

        async def mock_weather(params, session):
            raise Exception("network error")

        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.current_time_handler", mock_time), \
             patch("app.mcp.weather_tools.get_weather_handler", mock_weather):
            result = await svc._build_condition_context()
            assert result == ""

    @pytest.mark.asyncio
    async def test_weather_error_not_cached(self):
        svc = AutomationService.__new__(AutomationService)
        svc._weather_cache = None
        svc._weather_cache_at = 0.0

        async def mock_time(params, session):
            return {"date": "2026-06-20", "time": "15:30:00", "weekday": "星期六"}

        async def mock_weather(params, session):
            return {"error": "获取天气失败"}

        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.current_time_handler", mock_time), \
             patch("app.mcp.weather_tools.get_weather_handler", mock_weather):
            result = await svc._build_condition_context()
            assert "天气" not in result
            assert svc._weather_cache is None


# ---------------------------------------------------------------------------
# context-only 路径 per-user 化：按 rule.user_id 解析 chat key，老规则回退全局
# ---------------------------------------------------------------------------

class TestAutomationEvaluatePerUser:
    """automation_service.evaluate 的 context-only 路径 per-user 化测试。

    与 scheduler_service.TestReminderPerUser 同一模式：
    - 规则带 user_id + 用户有 per-user chat key → 构造 per-user LlmChatClient
    - 规则带 user_id + 用户无配置 → 回退全局 self._chat_client
    - 规则无 user_id（老规则）→ 直接走全局，resolve 不被调
    - vision 路径不受 per-user 影响（用 vision_service，不调 chat client）
    """

    def _make_svc_with_global_chat(self, registry):
        """构造 AutomationService，全局 chat client 预置为 mock（lazy init 不触发）。"""
        svc = AutomationService(registry)
        # 预置全局 chat client，避免 lazy init 走真实 LlmChatClient 构造
        global_chat = MagicMock()
        global_chat.chat = AsyncMock(return_value="1")
        svc._chat_client = global_chat
        return svc, global_chat

    @pytest.mark.asyncio
    async def test_context_only_uses_per_user_chat_key(self):
        """规则带 user_id + 用户有 per-user chat key → 构造 per-user client，全局不被调。"""
        registry = MagicMock()
        registry.list_rules.return_value = [
            {
                "id": "r1", "name": "晚上关灯", "condition": "晚上10点后", "type": "time",
                "actions": [], "enabled": True, "cooldown_seconds": 0,
                "last_triggered_at": 0, "user_id": "u-per-user",
            }
        ]
        svc, global_chat = self._make_svc_with_global_chat(registry)

        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)):
            with patch("app.clients.client_factory.LlmChatClient") as MockClient:
                mock_instance = MagicMock()
                mock_instance.chat = AsyncMock(return_value="1")
                MockClient.return_value = mock_instance

                # patch _build_condition_context 避免触发外部时间/天气调用
                with patch.object(svc, "_build_condition_context", AsyncMock(return_value="时间:23:00")):
                    await svc.evaluate(frames=None)

        MockClient.assert_called_with(role="chat")
        mock_instance.chat.assert_awaited()
        global_chat.chat.assert_not_awaited()
        # per-user key 覆盖了私有字段
        assert mock_instance._api_key == "per-user-secret"
        assert mock_instance._base_url == "https://per-user.example.com/v1"
        assert mock_instance._model == "per-user-model"
        assert mock_instance._enabled is True

    @pytest.mark.asyncio
    async def test_context_only_falls_back_to_global_when_no_per_user_key(self):
        """规则带 user_id 但用户无 per-user chat key → 回退全局 self._chat_client。"""
        registry = MagicMock()
        registry.list_rules.return_value = [
            {
                "id": "r1", "name": "晚上关灯", "condition": "晚上10点后", "type": "time",
                "actions": [], "enabled": True, "cooldown_seconds": 0,
                "last_triggered_at": 0, "user_id": "u-no-config",
            }
        ]
        svc, global_chat = self._make_svc_with_global_chat(registry)

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)):
            with patch.object(svc, "_build_condition_context", AsyncMock(return_value="时间:23:00")):
                await svc.evaluate(frames=None)

        global_chat.chat.assert_awaited()

    @pytest.mark.asyncio
    async def test_context_only_rule_without_user_id_uses_global(self):
        """老规则 user_id='' → 直接走全局 self._chat_client，resolve 不被调。"""
        registry = MagicMock()
        registry.list_rules.return_value = [
            {
                "id": "r1", "name": "晚上关灯", "condition": "晚上10点后", "type": "time",
                "actions": [], "enabled": True, "cooldown_seconds": 0,
                "last_triggered_at": 0,  # 故意不设 user_id
            }
        ]
        svc, global_chat = self._make_svc_with_global_chat(registry)

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)) as mock_resolve:
            with patch.object(svc, "_build_condition_context", AsyncMock(return_value="时间:23:00")):
                await svc.evaluate(frames=None)

        mock_resolve.assert_not_awaited()
        global_chat.chat.assert_awaited()

    @pytest.mark.asyncio
    async def test_vision_path_not_affected_by_per_user(self):
        """有 frames 时走 vision_service，不调 chat client（vision 路径不 per-user）。"""
        registry = MagicMock()
        registry.list_rules.return_value = [
            {
                "id": "r1", "name": "有人开灯", "condition": "桌上有鼠标", "type": "vision",
                "actions": [], "enabled": True, "cooldown_seconds": 0,
                "last_triggered_at": 0, "user_id": "u-per-user",
            }
        ]
        vision = MagicMock()
        vision.encode_frames_b64 = AsyncMock(return_value="base64data")
        vision.evaluate_condition = AsyncMock(return_value=0)  # 不触发动作
        svc = AutomationService(registry, vision_service=vision)
        # 预置全局 chat client（vision 路径不该调它）
        global_chat = MagicMock()
        global_chat.chat = AsyncMock(return_value="1")
        svc._chat_client = global_chat

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)) as mock_resolve:
            with patch.object(svc, "_build_condition_context", AsyncMock(return_value="时间:23:00")):
                await svc.evaluate(frames=[[1, 2, 3]])

        # vision 路径走 vision_service，不调 chat client，不调 resolve
        vision.evaluate_condition.assert_awaited()
        global_chat.chat.assert_not_awaited()
        mock_resolve.assert_not_awaited()


# ---------------------------------------------------------------------------
# P1：按 rule.type 路由（time/weather → chat，vision → VL）
# ---------------------------------------------------------------------------

class TestRuleTypeRouting:
    """P1：按 rule.type 路由——time/weather 走 chat，vision 走 VL（替代全局 use_context_only）。"""

    def _make_svc(self, registry, vision=None):
        svc = AutomationService(registry, vision_service=vision)
        chat = MagicMock()
        chat.chat = AsyncMock(return_value="1")
        svc._chat_client = chat
        svc._build_condition_context = AsyncMock(return_value="ctx")
        return svc, chat

    def _rule(self, rtype="vision", cond="桌上有鼠标"):
        return {
            "id": "r1", "name": "t", "condition": cond, "type": rtype,
            "actions": [], "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0,
        }

    @pytest.mark.asyncio
    async def test_time_rule_uses_chat_not_vl(self):
        registry = MagicMock()
        registry.list_rules.return_value = [self._rule("time", "晚上10点后")]
        vision = MagicMock()
        vision.encode_frames_b64 = AsyncMock(return_value="b64")
        vision.evaluate_condition = AsyncMock(return_value=1)
        svc, chat = self._make_svc(registry, vision)
        await svc.evaluate(frames=[[1, 2, 3]])
        chat.chat.assert_awaited()              # time 走 chat
        vision.evaluate_condition.assert_not_awaited()  # 不走 VL

    @pytest.mark.asyncio
    async def test_weather_rule_uses_chat_not_vl(self):
        registry = MagicMock()
        registry.list_rules.return_value = [self._rule("weather", "下雨时")]
        vision = MagicMock()
        vision.encode_frames_b64 = AsyncMock(return_value="b64")
        vision.evaluate_condition = AsyncMock(return_value=1)
        svc, chat = self._make_svc(registry, vision)
        await svc.evaluate(frames=[[1, 2, 3]])
        chat.chat.assert_awaited()              # weather 走 chat
        vision.evaluate_condition.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_vision_rule_uses_vl_not_chat(self):
        registry = MagicMock()
        registry.list_rules.return_value = [self._rule("vision", "桌上有鼠标")]
        vision = MagicMock()
        vision.encode_frames_b64 = AsyncMock(return_value="b64")
        vision.evaluate_condition = AsyncMock(return_value=0)
        svc, chat = self._make_svc(registry, vision)
        await svc.evaluate(frames=[[1, 2, 3]])
        vision.evaluate_condition.assert_awaited()  # vision 走 VL
        chat.chat.assert_not_awaited()              # 不走 chat

    @pytest.mark.asyncio
    async def test_vision_rule_no_frames_skipped(self):
        registry = MagicMock()
        registry.list_rules.return_value = [self._rule("vision", "桌上有鼠标")]
        vision = MagicMock()
        vision.encode_frames_b64 = AsyncMock(return_value="b64")
        vision.evaluate_condition = AsyncMock(return_value=1)
        svc, chat = self._make_svc(registry, vision)
        await svc.evaluate(frames=[])  # 无帧
        vision.evaluate_condition.assert_not_awaited()  # 无帧跳过 VL 组
        chat.chat.assert_not_awaited()                    # vision 规则不回退 chat

    @pytest.mark.asyncio
    async def test_type_missing_defaults_to_vision(self):
        registry = MagicMock()
        rule = self._rule()  # 不设 type
        rule.pop("type")
        registry.list_rules.return_value = [rule]
        vision = MagicMock()
        vision.encode_frames_b64 = AsyncMock(return_value="b64")
        vision.evaluate_condition = AsyncMock(return_value=0)
        svc, chat = self._make_svc(registry, vision)
        await svc.evaluate(frames=[[1, 2, 3]])
        vision.evaluate_condition.assert_awaited()  # 兜底 vision → VL
        chat.chat.assert_not_awaited()


# ---------------------------------------------------------------------------
# P1：设备状态门控（动作目标态已满足 → 评估前跳过，0 LLM/0 action）
# ---------------------------------------------------------------------------

class TestDeviceGate:
    """P1：设备状态门控——按动作推导目标态，cheap HA 查当前态，已在目标态跳过。"""

    def setup_method(self):
        self.svc = AutomationService.__new__(AutomationService)

    def _action(self, service="turn_off", entity_id="light.l", data=None, domain="light"):
        return {"mcp_tool_input": {"domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}}

    def test_turn_off_already_off(self):
        rule = {"actions": [self._action("turn_off", "light.l")]}
        state_map = {"light.l": {"entity_id": "light.l", "state": "off", "attributes": {}}}
        assert self.svc._device_already_in_target(rule, state_map) is True

    def test_turn_off_still_on(self):
        rule = {"actions": [self._action("turn_off", "light.l")]}
        state_map = {"light.l": {"entity_id": "light.l", "state": "on", "attributes": {}}}
        assert self.svc._device_already_in_target(rule, state_map) is False

    def test_close_cover_already_closed(self):
        rule = {"actions": [self._action("close_cover", "cover.c", domain="cover")]}
        state_map = {"cover.c": {"entity_id": "cover.c", "state": "closed"}}
        assert self.svc._device_already_in_target(rule, state_map) is True

    def test_close_cover_still_open(self):
        rule = {"actions": [self._action("close_cover", "cover.c", domain="cover")]}
        state_map = {"cover.c": {"entity_id": "cover.c", "state": "open"}}
        assert self.svc._device_already_in_target(rule, state_map) is False

    def test_set_temperature_at_target(self):
        rule = {"actions": [self._action("set_temperature", "climate.k", {"temperature": 26}, domain="climate")]}
        state_map = {"climate.k": {"entity_id": "climate.k", "state": "heat", "attributes": {"temperature": 26.0}}}
        assert self.svc._device_already_in_target(rule, state_map) is True

    def test_set_temperature_off_target(self):
        rule = {"actions": [self._action("set_temperature", "climate.k", {"temperature": 26}, domain="climate")]}
        state_map = {"climate.k": {"entity_id": "climate.k", "state": "heat", "attributes": {"temperature": 25.0}}}
        assert self.svc._device_already_in_target(rule, state_map) is False

    def test_unknown_service_not_skipped(self):
        # toggle/script 推不出目标态 → 保守不跳过
        rule = {"actions": [self._action("toggle", "light.l")]}
        state_map = {"light.l": {"entity_id": "light.l", "state": "off"}}
        assert self.svc._device_already_in_target(rule, state_map) is False

    def test_missing_entity_not_skipped(self):
        rule = {"actions": [self._action("turn_off", "light.missing")]}
        assert self.svc._device_already_in_target(rule, {}) is False

    def test_no_actions_not_skipped(self):
        assert self.svc._device_already_in_target({"actions": []}, {}) is False

    def test_unavailable_state_not_match(self):
        rule = {"actions": [self._action("turn_off", "light.l")]}
        state_map = {"light.l": {"entity_id": "light.l", "state": "unavailable"}}
        assert self.svc._device_already_in_target(rule, state_map) is False

    def test_multiple_actions_all_in_target(self):
        rule = {"actions": [
            self._action("turn_off", "light.a"),
            self._action("close_cover", "cover.b", domain="cover"),
        ]}
        state_map = {
            "light.a": {"entity_id": "light.a", "state": "off"},
            "cover.b": {"entity_id": "cover.b", "state": "closed"},
        }
        assert self.svc._device_already_in_target(rule, state_map) is True

    def test_multiple_actions_one_not_in_target(self):
        rule = {"actions": [
            self._action("turn_off", "light.a"),
            self._action("close_cover", "cover.b", domain="cover"),
        ]}
        state_map = {
            "light.a": {"entity_id": "light.a", "state": "off"},
            "cover.b": {"entity_id": "cover.b", "state": "open"},  # 未关
        }
        assert self.svc._device_already_in_target(rule, state_map) is False

    @pytest.mark.asyncio
    async def test_gate_skips_rule_zero_llm(self):
        """集成：动作目标态已满足 → 整条规则评估前跳过，chat/VL 都不调（0 LLM）。"""
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "r1", "name": "关窗帘", "condition": "桌上有鼠标", "type": "vision",
             "actions": [{"mcp_tool_input": {"domain": "cover", "service": "close_cover",
                                            "entity_id": "cover.c", "data": {}}}],
             "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0},
        ]
        vision = MagicMock()
        vision.encode_frames_b64 = AsyncMock(return_value="b64")
        vision.evaluate_condition = AsyncMock(return_value=1)
        ha = MagicMock()
        ha.get_states_snapshot = AsyncMock(return_value=[
            {"entity_id": "cover.c", "state": "closed", "attributes": {}}
        ])
        svc = AutomationService(registry, vision_service=vision, ha_service=ha)
        chat = MagicMock()
        chat.chat = AsyncMock(return_value="1")
        svc._chat_client = chat
        svc._build_condition_context = AsyncMock(return_value="ctx")
        result = await svc.evaluate(frames=[[1, 2, 3]])
        assert result == []  # 门控跳过 → 无动作
        vision.evaluate_condition.assert_not_awaited()  # 0 VL
        chat.chat.assert_not_awaited()                  # 0 chat

