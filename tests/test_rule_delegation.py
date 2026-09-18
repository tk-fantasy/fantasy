"""阶段8：时间/天气规则委托 HA 触发 —— 编译/双写/跳过/回调/生命周期联动。

委托链路：创建时 compile_trigger 把 NL 条件编译成 HA 触发器（component_ready
先行探测回调组件），确认时先落本地行再写 HA 自动化（动作=fire_rule 回调），
评估管道跳过 trigger_source=ha 的规则（不烧 LLM），HA 到点回调 /api/rules/fire
→ fire_from_ha 走动作全链路；删除/修改双向联动，失败一律降级回本地评估。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.automation_service import AutomationService
from app.services.ha_automation_service import HaAutomationService
from app.services.pending_rules import KIND_AUTOMATION_RULE, confirm_pending
from app.services.rule_registry_service import RuleRegistryService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# HaAutomationService.component_ready / compile_trigger / write_delegated
# ---------------------------------------------------------------------------

def _ha_svc(client, rule_service=None):
    rs = rule_service or MagicMock()
    return HaAutomationService(rule_service=rs, ha_client_ref=[client])


class _RS:
    """compile_trigger 依赖的 rule_service 最小桩：_prepare_rule_context + _parse_json。"""

    def __init__(self, reply: str):
        self._reply = reply
        self.captured_messages = None

    async def _prepare_rule_context(self, text, user_id="", system_template=None):
        client = MagicMock()
        client.chat = AsyncMock(return_value=self._reply)
        return {"client": client, "system_prompt": "SYS",
                "full_devices": [], "services_info": {}}

    def _parse_json(self, content):
        import json
        try:
            return json.loads(content)
        except Exception:
            return None


class TestComponentReady:
    def test_ready_when_service_registered(self, monkeypatch):
        import app.main as app_main
        monkeypatch.setattr(app_main, "APP_TOKEN", "tok")
        client = MagicMock()
        client.get_services = AsyncMock(return_value=[
            {"domain": "light", "services": {"turn_on": {}}},
            {"domain": "aether_conversation", "services": {"fire_rule": {}}},
        ])
        assert _run(_ha_svc(client).component_ready()) is True

    def test_not_ready_when_app_token_unset(self, monkeypatch):
        """APP_TOKEN 未设置时回调必被 401——闸门直接判 False，不出静默死规则。"""
        import app.main as app_main
        monkeypatch.setattr(app_main, "APP_TOKEN", "")
        client = MagicMock()
        client.get_services = AsyncMock(return_value=[
            {"domain": "aether_conversation", "services": {"fire_rule": {}}},
        ])
        assert _run(_ha_svc(client).component_ready()) is False

    def test_not_ready_without_fire_rule(self, monkeypatch):
        import app.main as app_main
        monkeypatch.setattr(app_main, "APP_TOKEN", "tok")
        client = MagicMock()
        client.get_services = AsyncMock(return_value=[
            {"domain": "aether_conversation", "services": {}},
        ])
        assert _run(_ha_svc(client).component_ready()) is False

    def test_not_ready_on_error_or_missing_client(self, monkeypatch):
        import app.main as app_main
        monkeypatch.setattr(app_main, "APP_TOKEN", "tok")
        client = MagicMock()
        client.get_services = AsyncMock(side_effect=RuntimeError("ha down"))
        assert _run(_ha_svc(client).component_ready()) is False
        assert _run(_ha_svc(None).component_ready()) is False


class TestCompileTrigger:
    def test_success(self):
        reply = ('{"trigger": [{"platform": "time", "at": "22:00:00"}], "condition": []}')
        result = _run(_ha_svc(MagicMock(), _RS(reply)).compile_trigger("每天22点", "time"))

        assert result["trigger"] == [{"platform": "time", "at": "22:00:00"}]
        assert result["condition"] == []

    def test_weather_condition_compiles(self):
        reply = ('{"trigger": [{"platform": "state", "entity_id": "weather.home", '
                 '"to": "rainy"}], "condition": []}')
        result = _run(_ha_svc(MagicMock(), _RS(reply)).compile_trigger("下雨时", "weather"))

        assert result["trigger"][0]["to"] == "rainy"

    def test_unsupported_platform_rejected(self):
        """白名单外 platform（calendar 触发不属 time/weather 委托范围）→ error → 走本地。"""
        reply = ('{"trigger": [{"platform": "calendar", "entity_id": "calendar.x"}], "condition": []}')
        result = _run(_ha_svc(MagicMock(), _RS(reply)).compile_trigger("开会时", "time"))

        assert "error" in result

    def test_llm_reports_uncompilable_condition(self):
        """模糊语义（「我快到家的时候」）LLM 按 prompt 输出 error → 调用方走本地兜底。"""
        reply = '{"error": "模糊语义"}'
        result = _run(_ha_svc(MagicMock(), _RS(reply)).compile_trigger("我快到家的时候", "weather"))

        assert "error" in result

    def test_empty_condition(self):
        assert "error" in _run(_ha_svc(MagicMock()).compile_trigger("", "time"))


class TestWriteDelegated:
    def test_config_shape(self):
        client = MagicMock()
        client.create_automation = AsyncMock(return_value={"id": "aid1"})

        result = _run(_ha_svc(client).write_delegated(
            "rule-1", "晚上关灯",
            [{"platform": "time", "at": "22:00:00"}], []))

        assert result == {"id": "aid1", "alias": "Aether·晚上关灯"}
        config = client.create_automation.await_args.args[0]
        assert config["alias"] == "Aether·晚上关灯"
        assert config["action"] == [{"action": "aether_conversation.fire_rule",
                                     "data": {"rule_id": "rule-1"}}]

    def test_overwrite_uses_existing_id(self):
        """revise 重同步走覆盖写：带原 automation_id，不新造第二条。"""
        client = MagicMock()
        client.create_automation = AsyncMock(return_value={"id": "aid1"})

        _run(_ha_svc(client).write_delegated(
            "rule-1", "x", [{"platform": "time", "at": "08:00:00"}], [],
            automation_id="aid1"))

        assert client.create_automation.await_args.kwargs == {"automation_id": "aid1"}

    def test_empty_trigger_rejected(self):
        result = _run(_ha_svc(MagicMock()).write_delegated("r", "n", [], []))
        assert "error" in result


# ---------------------------------------------------------------------------
# confirm_pending 双写：先落本地行（trigger_source=ha）再写 HA，失败降级
# ---------------------------------------------------------------------------

def _session(**drafts):
    return SimpleNamespace(user_id="u1", pending_confirmations=dict(drafts))


def _time_rule_draft(ha_trigger=None):
    rule = {"name": "晚上关灯", "condition": "每天22点", "type": "time",
            "actions": [{"mcp_tool_name": "ha_devices___call_service",
                         "mcp_tool_input": {"domain": "light", "service": "turn_off",
                                            "entity_id": "light.rd"}}],
            "summary": "每天22点关灯"}
    draft = {"kind": KIND_AUTOMATION_RULE, "rule": rule, "created_at": time.time()}
    if ha_trigger is not None:
        draft["ha_trigger"] = ha_trigger
    return draft


class _DualRegistry:
    """带 set_ha_backing 的注册表桩（记录绑定轨迹）。"""

    def __init__(self):
        self.saved: list[dict] = []
        self.bindings: list[tuple] = []

    def add_rule(self, rule, user_id=""):
        saved = {**rule, "id": "rule-1", "enabled": True}
        self.saved.append(saved)
        return saved

    def set_ha_backing(self, rule_id, automation_id, ha_trigger=None):
        rule = self.saved[-1]
        if automation_id:
            rule["trigger_source"] = "ha"
            rule["ha_automation_id"] = str(automation_id)
            if isinstance(ha_trigger, dict):
                rule["ha_trigger"] = ha_trigger
        else:
            rule["trigger_source"] = ""
            rule["ha_automation_id"] = ""
            rule["ha_trigger"] = {}
        self.bindings.append((rule_id, automation_id))
        return rule


class _HaSvcStub:
    def __init__(self, error=None):
        self._error = error
        self.calls = []

    async def write_delegated(self, rule_id, name, trigger, condition,
                              description="", automation_id=""):
        self.calls.append({"rule_id": rule_id, "name": name, "trigger": trigger})
        if self._error:
            return {"error": self._error}
        return {"id": "aid-9", "alias": f"Aether·{name}"}


class _AutoSvcStub:
    def __init__(self, instant=None):
        self._instant = instant or {"checked": False, "fired": False}

    async def instant_hit_check(self, rule):
        return self._instant


TRIGGER = {"trigger": [{"platform": "time", "at": "22:00:00"}], "condition": []}


class TestConfirmDualWrite:
    def test_dual_write_binds_and_notes(self):
        registry = _DualRegistry()
        ha_svc = _HaSvcStub()
        session = _session(p1=_time_rule_draft(ha_trigger=TRIGGER))

        result = _run(confirm_pending(
            session, "p1", registry, None, [MagicMock()],
            ha_automation_service=ha_svc, automation_service=_AutoSvcStub()))

        assert result["ok"] is True
        assert result["delegated"] is True
        assert "Home Assistant" in result["delegation_note"]
        # 双写顺序：本地行先有 trigger_source=ha（等 HA 回调期间评估管道跳过），
        # 写入成功后回写 automation_id + 编译产物快照（本地 JSON 可见真实触发条件）
        assert registry.saved[0]["trigger_source"] == "ha"
        assert registry.saved[0]["ha_automation_id"] == "aid-9"
        assert registry.saved[0]["ha_trigger"] == TRIGGER
        assert ha_svc.calls[0]["rule_id"] == "rule-1"
        assert ha_svc.calls[0]["trigger"] == TRIGGER["trigger"]

    def test_ha_write_failure_degrades_to_local(self):
        """HA 写入失败：规则保留、委托标记清空（回 30s 循环评估），确认整体不失败。"""
        registry = _DualRegistry()
        ha_svc = _HaSvcStub(error="HA 不可达")
        session = _session(p1=_time_rule_draft(ha_trigger=TRIGGER))

        result = _run(confirm_pending(
            session, "p1", registry, None, [MagicMock()],
            ha_automation_service=ha_svc, automation_service=_AutoSvcStub()))

        assert result["ok"] is True
        assert result["delegated"] is False
        assert "降级" in result["delegation_note"] or "失败" in result["delegation_note"]
        assert registry.saved[0]["trigger_source"] == ""
        assert registry.saved[0]["ha_automation_id"] == ""
        assert registry.bindings[-1] == ("rule-1", None)

    def test_instant_hit_reflected_in_note(self):
        registry = _DualRegistry()
        session = _session(p1=_time_rule_draft(ha_trigger=TRIGGER))
        auto = _AutoSvcStub({"checked": True, "fired": True})

        result = _run(confirm_pending(
            session, "p1", registry, None, [MagicMock()],
            ha_automation_service=_HaSvcStub(), automation_service=auto))

        assert "立即执行" in result["delegation_note"]

    def test_no_ha_trigger_unchanged(self):
        """没带 ha_trigger 的草稿行为完全不变（旧路径回归保护）。"""
        registry = _DualRegistry()
        session = _session(p1=_time_rule_draft())

        result = _run(confirm_pending(session, "p1", registry, None, [MagicMock()],
                                      ha_automation_service=_HaSvcStub()))

        assert result["ok"] is True
        assert result["delegated"] is False
        assert registry.saved[0].get("trigger_source") in ("", None)


# ---------------------------------------------------------------------------
# 评估管道跳过 + fire_from_ha 回调 + instant_hit_check
# ---------------------------------------------------------------------------

class _ListRegistry:
    def __init__(self, rules):
        self._rules = list(rules)

    def list_rules(self):
        return list(self._rules)

    def get_rule(self, rule_id):
        return next((r for r in self._rules if r["id"] == rule_id), None)


def _auto_svc(rules):
    return AutomationService(_ListRegistry(rules), tool_executor=MagicMock())


DELEGATED = {"id": "d1", "name": "晚上关灯", "type": "time", "condition": "每天22点",
             "enabled": True, "trigger_source": "ha", "ha_automation_id": "aid-9",
             "actions": [], "cooldown_seconds": 10, "last_triggered_at": 0.0,
             "user_id": ""}
LOCAL = {**DELEGATED, "id": "l1", "trigger_source": "", "condition": "每天21点"}


class TestEvaluateSkipsDelegated:
    def test_delegated_rule_not_evaluated(self):
        """委托规则不进任何评估管道——LLM 判定器一次都不该被调。"""
        svc = _auto_svc([DELEGATED, LOCAL])
        judge = AsyncMock(return_value=0)
        svc._evaluate_context_only = judge
        svc._ha_service = None
        svc._vision_service = None
        # _build_condition_context 依赖 HA 快照/天气，桩掉只验证委托规则的跳过
        svc._build_condition_context = AsyncMock(return_value="CTX")

        applied = _run(svc.evaluate(rule_types=("time", "weather")))

        # 本地规则照常评估；委托规则被跳过（judge 只会收到本地规则的条件）
        judged_conditions = [c.args[0] for c in judge.await_args_list]
        assert judged_conditions == ["每天21点"]
        assert applied == []


class TestFireFromHa:
    def test_disabled_rejected(self):
        svc = _auto_svc([{**DELEGATED, "enabled": False}])
        result = _run(svc.fire_from_ha("d1"))
        assert result == {"fired": False, "reason": "disabled", "rule": "晚上关灯"}

    def test_cooldown_rejected(self):
        svc = _auto_svc([{**DELEGATED, "last_triggered_at": time.time()}])
        result = _run(svc.fire_from_ha("d1"))
        assert result["fired"] is False and result["reason"] == "cooldown"
        assert result["retry_after"] > 0

    def test_fired_runs_actions_and_records(self):
        svc = _auto_svc([DELEGATED])
        svc._run_actions = AsyncMock(return_value=["ok"])
        result = _run(svc.fire_from_ha("d1"))
        assert result["fired"] is True
        assert result["results"] == ["ok"]
        svc._run_actions.assert_awaited_once()

    def test_unknown_rule_raises(self):
        import pytest
        with pytest.raises(ValueError):
            _run(_auto_svc([]).fire_from_ha("ghost"))


class TestInstantHitCheck:
    def test_time_type_skipped(self):
        svc = _auto_svc([])
        result = _run(svc.instant_hit_check({**DELEGATED, "type": "time"}))
        assert result == {"checked": False, "fired": False}

    def test_weather_not_hit(self):
        svc = _auto_svc([])
        svc._build_condition_context = AsyncMock(return_value="CTX")
        svc._evaluate_context_only = AsyncMock(return_value=0)

        result = _run(svc.instant_hit_check({**DELEGATED, "type": "weather"}))

        assert result == {"checked": True, "fired": False}

    def test_weather_hit_fires_immediately(self):
        """建规则时正下着雨：补一发判定后立即执行，保持「新建即生效」体感。"""
        svc = _auto_svc([DELEGATED])
        svc._build_condition_context = AsyncMock(return_value="CTX")
        svc._evaluate_context_only = AsyncMock(return_value=1)
        svc.fire_from_ha = AsyncMock(return_value={"fired": True, "results": ["ok"]})

        result = _run(svc.instant_hit_check({**DELEGATED, "type": "weather"}))

        assert result["fired"] is True
        svc.fire_from_ha.assert_awaited_once_with("d1")


# ---------------------------------------------------------------------------
# 注册表：绑定/解绑/反查
# ---------------------------------------------------------------------------

class TestRegistryHaBacking:
    def setup_method(self):
        self.reg = RuleRegistryService()
        with patch.object(self.reg, "_insert_rule_async"):
            self.rule = self.reg.add_rule({"name": "关灯", "condition": "22点",
                                           "type": "time", "actions": []})

    def test_bind_and_unbind(self):
        with patch.object(self.reg, "_save_rule_async"):
            bound = self.reg.set_ha_backing(
                self.rule["id"], "aid-1",
                ha_trigger={"trigger": [{"platform": "time", "at": "22:00:00"}], "condition": []})
            assert bound["trigger_source"] == "ha"
            assert bound["ha_automation_id"] == "aid-1"
            # 编译产物快照进规则 JSON：本地可见真实触发条件
            assert bound["ha_trigger"]["trigger"] == [{"platform": "time", "at": "22:00:00"}]

            unbound = self.reg.set_ha_backing(self.rule["id"], None)
            assert unbound["trigger_source"] == ""
            assert unbound["ha_automation_id"] == ""
            assert unbound["ha_trigger"] == {}

    def test_find_by_ha_automation_id(self):
        with patch.object(self.reg, "_save_rule_async"):
            self.reg.set_ha_backing(self.rule["id"], "aid-2")

        assert self.reg.find_by_ha_automation_id("aid-2")["id"] == self.rule["id"]
        assert self.reg.find_by_ha_automation_id("other") is None
        assert self.reg.find_by_ha_automation_id("") is None

    def test_bind_nonexistent_raises(self):
        import pytest
        from app.core.exceptions import AppException
        with pytest.raises(AppException):
            self.reg.set_ha_backing("ghost", "aid-x")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
