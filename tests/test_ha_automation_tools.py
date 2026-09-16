"""阶段5：聊天创建 HA 原生自动化——生成/校验/写入 + 两段式确认工具。

覆盖：
- 动作格式转换：_to_mcp_actions（包装给校验链）/ _to_ha_actions（现代 service-call 语法）
- trigger/condition 结构校验（缺 platform / 非列表）
- HaAutomationService.build_from_text：LLM 解析 → 草稿；校验失败带 error；
  动作经防幻觉链（validate → repair → 复验）
- create：config 装配（alias/trigger/condition/action）；list 轻量化；delete
- 聊天工具：create 落 pending（kind=ha_automation）→ confirm 写入并弹出草稿；
  list/delete handler；服务未就绪降级
- REST：GET /ha/automations 503/200 两分支
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ha_automation_service import (
    HaAutomationService, _to_ha_actions, _to_mcp_actions,
    _validate_conditions, _validate_triggers,
)


class TestConversions:
    def test_to_mcp_actions_wraps_for_validation(self):
        out = _to_mcp_actions([{"domain": "light", "service": "turn_on",
                                "entity_id": "light.a", "data": {"brightness": 200}}])
        assert out == [{
            "mcp_tool_name": "ha_devices___call_service",
            "mcp_tool_input": {"domain": "light", "service": "turn_on",
                               "entity_id": "light.a", "data": {"brightness": 200}},
        }]

    def test_to_ha_actions_modern_syntax(self):
        mcp = _to_mcp_actions([{"domain": "light", "service": "turn_on",
                                "entity_id": "light.a", "data": {"brightness": 200}},
                               {"domain": "switch", "service": "turn_off",
                                "entity_id": "switch.b", "data": {}}])
        ha = _to_ha_actions(mcp)
        assert ha[0] == {"action": "light.turn_on",
                         "target": {"entity_id": "light.a"}, "data": {"brightness": 200}}
        # 空 data 不带 key
        assert ha[1] == {"action": "switch.turn_off", "target": {"entity_id": "switch.b"}}

    def test_validate_triggers(self):
        assert _validate_triggers([]) != []
        assert _validate_triggers("x") != []
        assert _validate_triggers([{"platform": "sun", "event": "sunset"}]) == []
        assert _validate_triggers([{"event": "sunset"}]) != []  # 缺 platform

    def test_validate_conditions(self):
        assert _validate_conditions(None) == []
        assert _validate_conditions([]) == []
        assert _validate_conditions([{"condition": "state", "entity_id": "sun.sun"}]) == []
        assert _validate_conditions([{"entity_id": "sun.sun"}]) != []
        assert _validate_conditions({"condition": "state"}) != []  # 非列表


def _rule_service_stub(llm_payload: dict | None = None, chat_side_effect=None):
    rs = MagicMock()
    client = MagicMock()
    if chat_side_effect is not None:
        client.chat = AsyncMock(side_effect=chat_side_effect)
    else:
        client.chat = AsyncMock(return_value=json.dumps(llm_payload, ensure_ascii=False))
    rs._prepare_rule_context = AsyncMock(return_value={
        "client": client, "system_prompt": "SP {controls_text} {device_list_text}",
        "full_devices": [{"entity_id": "light.hall"}],
        "services_info": {"light": {"turn_on": []}},
    })
    rs._parse_json = staticmethod(lambda s: json.loads(s))
    rs._validate_actions = MagicMock(return_value=[])
    rs._auto_repair_actions = MagicMock(
        side_effect=lambda parsed, devices, services: parsed)
    return rs


class TestBuildFromText:
    async def test_happy_path_draft(self):
        rs = _rule_service_stub({
            "alias": "到家开玄关灯",
            "description": "",
            "trigger": [{"platform": "state", "entity_id": "person.z", "to": "home"}],
            "condition": [],
            "actions": [{"domain": "light", "service": "turn_on",
                         "entity_id": "light.hall", "data": {}}],
            "action_descriptions": ["开玄关灯"],
        })
        svc = HaAutomationService(rule_service=rs, ha_client_ref=[MagicMock()])
        result = await svc.build_from_text("我到家开玄关灯")
        draft = result["draft"]
        assert draft["alias"] == "到家开玄关灯"
        assert draft["trigger"][0]["platform"] == "state"
        assert draft["actions"][0]["mcp_tool_input"]["entity_id"] == "light.hall"

    async def test_missing_trigger_rejected(self):
        rs = _rule_service_stub({
            "alias": "x", "trigger": [], "condition": [],
            "actions": [{"domain": "light", "service": "turn_on",
                         "entity_id": "light.hall", "data": {}}],
        })
        svc = HaAutomationService(rule_service=rs)
        result = await svc.build_from_text("开玄关灯")
        assert "触发" in result["error"]

    async def test_no_actions_rejected(self):
        rs = _rule_service_stub({
            "alias": "x", "trigger": [{"platform": "time", "at": "08:00:00"}],
            "condition": [], "actions": [],
        })
        result = await HaAutomationService(rule_service=rs).build_from_text("每天8点")
        assert "动作" in result["error"]

    async def test_validation_failure_blocks_draft(self):
        rs = _rule_service_stub({
            "alias": "x", "trigger": [{"platform": "time", "at": "08:00:00"}],
            "condition": [],
            "actions": [{"domain": "light", "service": "turn_on",
                         "entity_id": "light.ghost", "data": {}}],
        })
        # 校验两次都失败（修复救不回来）→ 拒绝出草稿
        rs._validate_actions = MagicMock(return_value=["entity_id 'light.ghost' 不存在"])
        result = await HaAutomationService(rule_service=rs).build_from_text("开幽灵灯")
        assert "校验未通过" in result["error"]
        assert rs._auto_repair_actions.called  # 尝试过修复

    async def test_llm_garbage_retries_then_error(self):
        rs = _rule_service_stub(chat_side_effect=ValueError("bad json"))
        result = await HaAutomationService(rule_service=rs).build_from_text("随便")
        assert "解析失败" in result["error"]
        assert rs._prepare_rule_context.await_count == 1
        assert rs._prepare_rule_context.await_args.kwargs.get("system_template") is not None


class TestWriteAndManage:
    def _draft(self) -> dict:
        return {
            "alias": "到家开玄关灯", "description": "d",
            "trigger": [{"platform": "state", "entity_id": "person.z", "to": "home"}],
            "condition": [],
            "actions": _to_mcp_actions([
                {"domain": "light", "service": "turn_on", "entity_id": "light.hall", "data": {}}]),
        }

    async def test_create_posts_config(self):
        client = MagicMock()
        client.create_automation = AsyncMock(return_value={"id": "auto1"})
        svc = HaAutomationService(rule_service=MagicMock(), ha_client_ref=[client])
        result = await svc.create(self._draft())
        assert result == {"id": "auto1", "alias": "到家开玄关灯"}
        config = client.create_automation.await_args.args[0]
        assert config["alias"] == "到家开玄关灯"
        assert config["action"] == [{"action": "light.turn_on",
                                     "target": {"entity_id": "light.hall"}}]
        assert config["trigger"][0]["platform"] == "state"

    async def test_list_lightweight(self):
        """HA 2026 起 automations 是实体：列表数据来自 domain=automation 的 states。"""
        client = MagicMock()
        client.list_automations = AsyncMock(return_value=[
            {"id": "a1", "entity_id": "automation.chen_deng", "alias": "晨灯",
             "state": "on", "last_triggered": "2026-09-15T06:30:00"},
        ])
        svc = HaAutomationService(rule_service=MagicMock(), ha_client_ref=[client])
        items = await svc.list_automations()
        assert items[0]["id"] == "a1"
        assert items[0]["alias"] == "晨灯"
        assert items[0]["entity_id"] == "automation.chen_deng"
        assert items[0]["last_triggered"] == "2026-09-15T06:30:00"

    async def test_delete(self):
        client = MagicMock()
        client.delete_automation = AsyncMock(return_value=None)
        # 删 config 后实体残留为幽灵 → 补 entity_registry 移除
        client.list_automations = AsyncMock(return_value=[
            {"id": "a1", "entity_id": "automation.a1", "state": "unavailable"}])
        client.remove_entity = AsyncMock(return_value=True)
        svc = HaAutomationService(rule_service=MagicMock(), ha_client_ref=[client])
        assert await svc.delete("a1") == {"deleted": True, "id": "a1"}
        client.remove_entity.assert_awaited_once_with("automation.a1")
        # 实体状态正常（非幽灵）时不移除
        client.list_automations = AsyncMock(return_value=[
            {"id": "a1", "entity_id": "automation.a1", "state": "on"}])
        client.remove_entity.reset_mock()
        await svc.delete("a1")
        client.remove_entity.assert_not_awaited()
        client.delete_automation = AsyncMock(side_effect=RuntimeError("404"))
        assert "error" in await svc.delete("a1")


class TestChatTools:
    def _register(self, svc):
        """注册工具并捕获 handler。handler 运行时动态读 container，
        故每个测试用 _with_svc 上下文包住 handler 调用。"""
        from app.tools import _register_ha_automation_tools, ToolDeps
        self._svc = svc
        tools: dict = {}

        def _capture(tool):
            tools[tool.tool_name] = tool
            return True
        mgr = MagicMock()
        mgr.register_tool = _capture
        deps = MagicMock(spec=ToolDeps)
        deps.mcp_client_manager = mgr
        _register_ha_automation_tools(deps)
        return tools

    def _with_svc(self):
        """handler 运行时动态读 container，故调用要包在 patch 里。"""
        return patch("app.container.get_container",
                     return_value=SimpleNamespace(ha_automation_service=self._svc))

    async def test_create_confirm_flow(self):
        svc = MagicMock()
        svc.build_from_text = AsyncMock(return_value={"draft": {
            "alias": "到家开玄关灯", "description": "", "trigger": [{"platform": "state"}],
            "condition": [], "action_descriptions": [],
            "actions": [{"mcp_tool_name": "ha_devices___call_service",
                         "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                            "entity_id": "light.hall", "data": {}}}],
            "auto_corrections": [],
        }})
        svc.create = AsyncMock(return_value={"id": "auto9", "alias": "到家开玄关灯"})
        tools = self._register(svc)
        session = SimpleNamespace()

        with self._with_svc():
            ret = await tools["ha_automation_create"].handler(
                {"text": "我到家开玄关灯"}, session)
        assert ret["status"] == "pending_confirm"
        pending_id = ret["pending_id"]
        store = session.pending_confirmations
        assert store[pending_id]["kind"] == "ha_automation"

        with self._with_svc():
            ret2 = await tools["ha_automation_confirm"].handler(
                {"pending_id": pending_id}, session)
        assert ret2["success"] is True and ret2["id"] == "auto9"
        svc.create.assert_awaited_once()
        assert pending_id not in store  # 确认成功后草稿弹出

    async def test_confirm_missing_pending(self):
        tools = self._register(MagicMock())
        with self._with_svc():
            ret = await tools["ha_automation_confirm"].handler({}, SimpleNamespace())
        assert "error" in ret

    async def test_list_and_delete(self):
        svc = MagicMock()
        svc.list_automations = AsyncMock(return_value=[{"id": "a1", "alias": "x"}])
        svc.delete = AsyncMock(return_value={"deleted": True, "id": "a1"})
        tools = self._register(svc)
        with self._with_svc():
            ret = await tools["ha_automation_list"].handler({}, SimpleNamespace())
        assert ret["count"] == 1
        with self._with_svc():
            ret = await tools["ha_automation_delete"].handler(
                {"automation_id": "a1"}, SimpleNamespace())
        assert ret["success"] is True

    async def test_service_unavailable(self):
        tools = self._register(None)
        with self._with_svc():
            ret = await tools["ha_automation_list"].handler({}, SimpleNamespace())
        assert "未就绪" in ret["error"]


class TestRestEndpoints:
    async def test_list_ok(self):
        from app.routes import ha_routes
        c = MagicMock()
        c.ha_automation_service.list_automations = AsyncMock(return_value=[{"id": "a1"}])
        with patch.object(ha_routes, "get_container", return_value=c):
            res = await ha_routes.list_ha_automations(container=c)
        assert res.data == [{"id": "a1"}]

    async def test_list_no_service_503(self):
        from app.routes import ha_routes
        from app.core.exceptions import AppException
        c = SimpleNamespace(ha_automation_service=None)
        with patch.object(ha_routes, "get_container", return_value=c), \
             pytest.raises(AppException) as exc_info:
            await ha_routes.list_ha_automations(container=c)
        assert exc_info.value.http_status == 503
