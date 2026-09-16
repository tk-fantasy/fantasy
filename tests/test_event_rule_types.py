"""阶段4：事件驱动规则类型（presence/sun/calendar/helper）+ 模版与路由适配。

覆盖：
- rule_registry_service：RULE_TYPES 常量 / _guess_type 新关键词分支
- rule_service：type 白名单收纳新类型（经 RULE_TYPES）
- pending_rules：is_vision_rule 对新类型的归类（前后端口径一致由 ruleMismatch.js 承担）
- prompt_service：生成模版与讲解模版包含新类型
- EventTriggerService：person/sun/input_boolean/timer.finished 事件 → 对应类型评估；
  未知 domain 忽略；calendar 轮询到点触发 + 去重 + 未配置不轮询
- automation_service.evaluate：presence 规则走 chat 路由；event_context 注入条件上下文
- ha_service：input_boolean 进设备目录（无区域豁免）、移出状态实体目录
- device_event_service：person 到家/离家文案；sun 翻转落时间线
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.event_trigger_service import EventTriggerService


# ===================== 类型枚举与猜测 =====================

class TestRuleTypes:
    def test_rule_types_constant(self):
        from app.services.rule_registry_service import RULE_TYPES, EVENT_RULE_TYPES
        assert set(RULE_TYPES) == {"time", "weather", "vision",
                                   "presence", "sun", "calendar", "helper"}
        assert set(EVENT_RULE_TYPES) == {"presence", "sun", "calendar", "helper"}

    def test_guess_type_new_keywords(self):
        from app.services.rule_registry_service import RuleRegistryService as R
        assert R._guess_type("我到家的时候") == "presence"
        assert R._guess_type("全家人离家后") == "presence"
        assert R._guess_type("日落后半小时") == "sun"
        assert R._guess_type("天黑了") == "sun"
        assert R._guess_type("日历上会议开始时") == "calendar"
        assert R._guess_type("离家开关打开时") == "helper"
        assert R._guess_type("计时器结束后关灯") == "helper"
        # 既有行为不回归
        assert R._guess_type("桌上有杯子") == "vision"
        assert R._guess_type("下雨了") == "weather"
        assert R._guess_type("晚上10点") == "time"

    def test_is_vision_rule_new_types(self):
        from app.services.pending_rules import is_vision_rule, needs_camera
        for t in ("presence", "sun", "calendar", "helper", "time", "weather"):
            assert is_vision_rule({"type": t}) is False
            assert needs_camera({"type": t, "camera_id": ""}) is False
        assert is_vision_rule({"type": "vision"}) is True
        assert is_vision_rule({}) is True  # 缺失兜底 vision
        assert needs_camera({"type": "vision", "camera_id": ""}) is True

    def test_prompt_templates_mention_new_types(self):
        from app.services.prompt_service import (
            RULE_SYSTEM_PROMPT_TEMPLATE, RULE_EXPLAIN_PROMPT)
        for t in ("presence", "sun", "calendar", "helper"):
            assert t in RULE_SYSTEM_PROMPT_TEMPLATE
            assert t in RULE_EXPLAIN_PROMPT


# ===================== EventTriggerService =====================

def _ets() -> tuple[EventTriggerService, AsyncMock]:
    ets = EventTriggerService(ha_service=MagicMock(), automation_service=MagicMock())
    ets._automation_service.evaluate = AsyncMock(return_value=[])
    return ets, ets._automation_service.evaluate


class TestEventTrigger:
    async def test_person_home_fires_presence(self):
        ets, evaluate = _ets()
        await ets._on_event({
            "event_type": "state_changed", "entity_id": "person.zhang",
            "domain": "person",
            "new_state": {"state": "home", "attributes": {"friendly_name": "张三"}},
        })
        evaluate.assert_awaited_once()
        _, kwargs = evaluate.await_args
        assert kwargs["rule_types"] == ("presence",)
        assert "张三 到家" in kwargs["event_context"]["event"]

    async def test_device_tracker_away_fires_presence(self):
        ets, evaluate = _ets()
        await ets._on_event({
            "event_type": "state_changed", "entity_id": "device_tracker.phone",
            "domain": "device_tracker",
            "new_state": {"state": "not_home", "attributes": {}},
        })
        _, kwargs = evaluate.await_args
        assert kwargs["rule_types"] == ("presence",)
        assert "离家" in kwargs["event_context"]["event"]

    async def test_sun_flip(self):
        ets, evaluate = _ets()
        await ets._on_event({
            "event_type": "state_changed", "entity_id": "sun.sun",
            "domain": "sun",
            "new_state": {"state": "below_horizon", "attributes": {}},
        })
        _, kwargs = evaluate.await_args
        assert kwargs["rule_types"] == ("sun",)
        assert "日落" in kwargs["event_context"]["event"]

    async def test_input_boolean_fires_helper(self):
        ets, evaluate = _ets()
        await ets._on_event({
            "event_type": "state_changed", "entity_id": "input_boolean.away",
            "domain": "input_boolean",
            "new_state": {"state": "on", "attributes": {"friendly_name": "离家开关"}},
        })
        _, kwargs = evaluate.await_args
        assert kwargs["rule_types"] == ("helper",)
        assert "离家开关" in kwargs["event_context"]["event"]

    async def test_timer_finished_fires_helper(self):
        ets, evaluate = _ets()
        # HA timer.finished 事件无 new_state（不是 state_changed），名称退实体 id
        await ets._on_event({
            "event_type": "timer.finished", "domain": "timer",
            "entity_id": "timer.ten_sec", "duration": 10,
        })
        _, kwargs = evaluate.await_args
        assert kwargs["rule_types"] == ("helper",)
        assert "timer.ten_sec" in kwargs["event_context"]["event"]
        assert "倒计时结束" in kwargs["event_context"]["event"]

    async def test_unknown_domain_ignored(self):
        ets, evaluate = _ets()
        await ets._on_event({
            "event_type": "state_changed", "entity_id": "light.bed",
            "domain": "light", "new_state": {"state": "on"},
        })
        evaluate.assert_not_awaited()

    async def test_evaluate_failure_isolated(self):
        ets, evaluate = _ets()
        evaluate.side_effect = RuntimeError("boom")
        await ets._on_event({  # 不抛
            "event_type": "state_changed", "entity_id": "sun.sun",
            "domain": "sun", "new_state": {"state": "above_horizon"},
        })


class TestCalendarPoll:
    def _ets_cal(self, monkeypatch, entities, events):
        ets = EventTriggerService(ha_service=MagicMock(), automation_service=MagicMock())
        ets._automation_service.evaluate = AsyncMock(return_value=[])
        client = MagicMock()
        client.calendar_events = AsyncMock(return_value=events)
        ets._ha_service._client = client
        monkeypatch.setattr(
            "app.services.event_trigger_service.get_config",
            lambda key, default=None: entities if key == "automation.calendar_entities" else default)
        return ets, client

    async def test_event_start_fires_once_and_dedup(self, monkeypatch):
        start = (datetime.now() - timedelta(seconds=60)).isoformat()
        # end 在 1 小时后：本轮只应触发 start 相位（start/end 按相位独立去重）
        end = (datetime.now() + timedelta(hours=1)).isoformat()
        ets, client = self._ets_cal(monkeypatch, ["calendar.work"], [
            {"summary": "周会", "uid": "e1",
             "start": {"dateTime": start}, "end": {"dateTime": end}},
        ])
        now = datetime.now()
        await ets._poll_calendars(now=now)
        _, kwargs = ets._automation_service.evaluate.await_args
        assert kwargs["rule_types"] == ("calendar",)
        assert "周会" in kwargs["event_context"]["event"] and "开始" in kwargs["event_context"]["event"]
        # 同一事件第二轮轮询不重复触发
        await ets._poll_calendars(now=now + timedelta(seconds=5))
        assert ets._automation_service.evaluate.await_count == 1

    async def test_not_configured_skips_poll(self, monkeypatch):
        ets, client = self._ets_cal(monkeypatch, [], [])
        await ets._poll_calendars()
        client.calendar_events.assert_not_awaited()

    async def test_out_of_window_not_fired(self, monkeypatch):
        start = (datetime.now() - timedelta(hours=2)).isoformat()
        ets, _ = self._ets_cal(monkeypatch, ["calendar.work"], [
            {"summary": "旧会", "uid": "e2", "start": {"dateTime": start}},
        ])
        await ets._poll_calendars()
        ets._automation_service.evaluate.assert_not_awaited()


# ===================== automation_service 路由 =====================

class TestEvaluateRouting:
    async def test_presence_rule_routes_to_chat_with_event_context(self):
        from app.services.automation_service import AutomationService
        registry = MagicMock()
        registry.list_rules.return_value = [{
            "id": "r1", "name": "到家开灯", "type": "presence",
            "condition": "有人到家且天黑", "enabled": True,
            "cooldown_seconds": 5, "last_triggered_at": 0.0,
            "actions": [], "camera_id": "",
        }]
        svc = AutomationService(rule_registry=registry, ha_service=None)
        svc._evaluate_context_only = AsyncMock(return_value=1)
        svc._apply_results = AsyncMock(return_value=[])

        captured = {}

        async def fake_ctx(event_context=None, state_map=None):
            captured.update(event_context or {})
            return "ctx"
        svc._build_condition_context = fake_ctx

        await svc.evaluate(
            frames=None, camera_id="", rule_types=("presence",),
            event_context={"event": "事件：张三 到家"})
        svc._evaluate_context_only.assert_awaited_once()
        assert "张三 到家" in captured.get("event", "")
        svc._apply_results.assert_awaited_once()  # 条件成立 → 走动作执行

    async def test_sun_rule_counted_as_context_eval(self):
        from app.services.automation_service import AutomationService
        registry = MagicMock()
        registry.list_rules.return_value = []
        svc = AutomationService(rule_registry=registry)
        svc._build_condition_context = AsyncMock(return_value="")
        before = svc._context_eval_count
        await svc.evaluate(rule_types=("sun",))
        assert svc._context_eval_count == before + 1


# ===================== 目录与事件文案 =====================

class TestDomainCatalog:
    async def test_input_boolean_in_device_catalog_without_area(self):
        from app.services.ha_service import HAService
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[
            {"entity_id": "input_boolean.away", "state": "on",
             "attributes": {"friendly_name": "离家开关"}},
        ])
        svc = HAService(client=client)
        svc._area_map = {}
        svc._entity_area_map = {}
        svc._area_cache_at = 9999999999
        devices = await svc.get_all_devices()
        assert [d["entity_id"] for d in devices] == ["input_boolean.away"]  # 无区域也可见

    async def test_input_boolean_not_in_status_entities(self):
        from app.services.ha_service import HAService
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[
            {"entity_id": "input_boolean.away", "state": "on", "attributes": {}},
            {"entity_id": "timer.t", "state": "idle", "attributes": {}},
        ])
        svc = HAService(client=client)
        svc._area_cache_at = 9999999999
        svc._alias_cache_at = 9999999999
        status = await svc.get_status_entities()
        ids = [e["entity_id"] for e in status]
        assert "timer.t" in ids
        assert "input_boolean.away" not in ids  # 已移入设备目录，避免双显


class TestPersonEventWording:
    async def test_person_home_away_wording(self):
        from app.services.device_event_service import DeviceEventService
        svc = DeviceEventService(ha_service=MagicMock())
        with patch("app.services.alert_service.alert_service.record", new=AsyncMock()) as rec, \
             patch("app.core.ws_registry.push_to_events", new=AsyncMock()):
            await svc._on_state_changed(
                "person.zhang",
                {"state": "not_home", "attributes": {}},
                {"state": "home", "attributes": {"friendly_name": "张三"}})
        msg = rec.await_args.args[2]
        assert msg == "张三 到家"

    async def test_sun_flip_recorded(self):
        from app.services.device_event_service import DeviceEventService
        svc = DeviceEventService(ha_service=MagicMock())
        with patch("app.services.alert_service.alert_service.record", new=AsyncMock()) as rec, \
             patch("app.core.ws_registry.push_to_events", new=AsyncMock()):
            await svc._on_state_changed(
                "sun.sun",
                {"state": "above_horizon", "attributes": {"friendly_name": "Sun"}},
                {"state": "below_horizon", "attributes": {"friendly_name": "Sun"}})
        assert "夜间" in rec.await_args.args[2]
