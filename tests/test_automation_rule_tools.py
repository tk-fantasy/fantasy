"""automation_rule_* 聊天工具测试：两段式确认 / 修改 / 触发 / 列表 / 删除。"""

import asyncio
import time
from types import SimpleNamespace

from app.mcp.mcp_client_manager import MCPClientManager
from app.tools import _register_automation_rule_tools

RULE = {
    "name": "高温开空调",
    "condition": "温度高于30度",
    "type": "weather",
    "actions": [{"mcp_tool_name": "ha_devices___call_service",
                 "mcp_tool_input": {"domain": "climate", "service": "turn_on",
                                    "entity_id": "climate.bedroom"}}],
    "action_descriptions": ["打开卧室空调"],
    "cooldown_seconds": 60,
    "summary": "温度高于30度就开空调",
    "camera_id": "",
}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_deps_and_tools(rule_service=None, registry=None, automation=None,
                         snapshot=None, all_devices=None, name_map=None):
    """注册自动化工具到独立 manager，返回 (按名取 handler 的 dict, session 桩)。"""

    class _HAService:
        async def get_states_snapshot(self):
            return snapshot if snapshot is not None else [
                {"entity_id": "climate.bedroom"}]

        async def get_all_devices(self):
            return all_devices or []

        async def get_entity_name_map(self):
            return name_map or {}

    deps = SimpleNamespace(
        mcp_client_manager=MCPClientManager(),
        ha_service=_HAService(),
        ha_client_ref=[AsyncStubClient()],
        rule_service=rule_service,
        rule_registry_service=registry,
        automation_service=automation,
    )
    _register_automation_rule_tools(deps)
    tools = {t.tool_name: t.handler
             for t in deps.mcp_client_manager.list_tools()}
    session = SimpleNamespace(user_id="u1", pending_confirmations={},
                              # create 工具受 wants_rule_creation 门控（读本轮用户
                              # 原话），桩默认给带关键词的 query 走正常创建路径。
                              current_query="帮我创建一条规则：温度高于30度就开空调")
    return tools, session


class AsyncStubClient:
    async def get_states(self):
        return []


class StubRuleService:
    async def build_rule(self, text, user_id="", camera_id=""):
        return {**RULE, "summary": text}

    async def revise_rule(self, rule, instruction, user_id=""):
        new = {**rule, "condition": "温度高于35度"}
        return {"rule": new, "summary": "把阈值改成35度"}


class StubRegistry:
    def __init__(self):
        self.saved: list[dict] = []
        self.rules: list[dict] = []

    def add_rule(self, rule, user_id=""):
        saved = {**rule, "id": "rule-1", "user_id": user_id, "enabled": True}
        self.saved.append(saved)
        self.rules.append(saved)
        return saved

    def list_rules(self):
        return self.rules

    def get_rule(self, rule_id):
        return next((r for r in self.rules if r["id"] == rule_id), None)

    def delete_rule(self, rule_id):
        for i, r in enumerate(self.rules):
            if r["id"] == rule_id:
                return self.rules.pop(i)
        raise RuntimeError("规则不存在: " + rule_id)


class StubAutomation:
    def __init__(self):
        self.triggered: list[str] = []

    async def trigger_rule(self, rule_id):
        self.triggered.append(rule_id)
        return {"rule": "高温开空调", "results": [{"tool": "call_service"}]}


def _create(tools, session, text="温度高于30度就开空调"):
    return _run(tools["automation_rule_create"]({"text": text}, session))


def test_create_returns_pending_confirm_and_stores_pending():
    tools, session = _make_deps_and_tools(rule_service=StubRuleService())

    result = _create(tools, session)

    assert result["status"] == "pending_confirm"
    assert result["pending_id"] in session.pending_confirmations
    assert result["rule"]["actions"]
    assert result["expire_minutes"] == 10
    assert "尚未创建" in result["note"]


def test_create_requires_text():
    tools, session = _make_deps_and_tools(rule_service=StubRuleService())

    result = _run(tools["automation_rule_create"]({"text": ""}, session))

    assert "error" in result


def test_create_rejects_rule_without_actions():
    class _EmptySvc(StubRuleService):
        async def build_rule(self, text, user_id="", camera_id=""):
            return {**RULE, "actions": []}

    tools, session = _make_deps_and_tools(rule_service=_EmptySvc())

    result = _create(tools, session)

    assert "error" in result
    assert "hint" in result


def test_confirm_persists_with_user_id_and_clears_pending():
    registry = StubRegistry()
    tools, session = _make_deps_and_tools(rule_service=StubRuleService(),
                                          registry=registry)
    created = _create(tools, session)

    result = _run(tools["automation_rule_confirm"](
        {"pending_id": created["pending_id"]}, session))

    assert result["success"] is True
    assert result["rule_id"] == "rule-1"
    assert registry.saved[0]["user_id"] == "u1"
    assert created["pending_id"] not in session.pending_confirmations


def test_confirm_expired_pending_rejected():
    tools, session = _make_deps_and_tools(rule_service=StubRuleService(),
                                          registry=StubRegistry())
    created = _create(tools, session)
    session.pending_confirmations[created["pending_id"]]["created_at"] = (
        time.time() - 601)

    result = _run(tools["automation_rule_confirm"](
        {"pending_id": created["pending_id"]}, session))

    assert "error" in result
    assert "过期" in result["error"]


def test_confirm_missing_entity_rejected():
    """confirm 前轻量校验：实体已消失 → 拒绝并提示 revise。"""
    tools, session = _make_deps_and_tools(
        rule_service=StubRuleService(), registry=StubRegistry(), snapshot=[])
    created = _create(tools, session)

    result = _run(tools["automation_rule_confirm"](
        {"pending_id": created["pending_id"]}, session))

    assert "error" in result
    assert "climate.bedroom" in result["error"]


def test_revise_updates_pending_rule():
    svc = StubRuleService()
    tools, session = _make_deps_and_tools(rule_service=svc, registry=StubRegistry())
    created = _create(tools, session)

    result = _run(tools["automation_rule_revise"](
        {"pending_id": created["pending_id"], "instruction": "改成35度"}, session))

    assert result["status"] == "pending_confirm"
    assert result["rule"]["condition"] == "温度高于35度"
    assert result["change_summary"] == "把阈值改成35度"
    # 落库前规则已在 pending 里更新（confirm 会用新规则）
    pending = session.pending_confirmations[created["pending_id"]]
    assert pending["rule"]["condition"] == "温度高于35度"


# ---------------------------------------------------------------------------
# 口头确认兜底 — 会话历史不持久化 tool 消息，跨轮后模型手里没有 pending_id
# ---------------------------------------------------------------------------

def test_confirm_without_pending_id_uses_only_draft():
    """用户第二轮只说「确认」：模型无 id 可传，会话内唯一草稿自动采用。"""
    registry = StubRegistry()
    tools, session = _make_deps_and_tools(rule_service=StubRuleService(),
                                          registry=registry)
    created = _create(tools, session)

    result = _run(tools["automation_rule_confirm"]({}, session))

    assert result["success"] is True
    assert result["rule_id"] == "rule-1"
    assert created["pending_id"] not in session.pending_confirmations


def test_confirm_with_stale_pending_id_falls_back_to_only_draft():
    registry = StubRegistry()
    tools, session = _make_deps_and_tools(rule_service=StubRuleService(),
                                          registry=registry)
    _create(tools, session)

    result = _run(tools["automation_rule_confirm"](
        {"pending_id": "编出来的id"}, session))

    assert result["success"] is True


def test_confirm_ambiguous_drafts_asks_user_to_choose():
    """两个草稿都在：不能替用户猜，报错要求指明。"""
    registry = StubRegistry()
    tools, session = _make_deps_and_tools(rule_service=StubRuleService(),
                                          registry=registry)
    _create(tools, session, text="温度高于30度就开空调")
    _create(tools, session, text="有人就开灯")

    result = _run(tools["automation_rule_confirm"]({}, session))

    assert "error" in result
    assert "2 个" in result["error"]
    assert registry.saved == []


def test_confirm_without_draft_reports_missing():
    registry = StubRegistry()
    tools, session = _make_deps_and_tools(rule_service=StubRuleService(),
                                          registry=registry)

    result = _run(tools["automation_rule_confirm"]({}, session))

    assert "error" in result
    assert "没有待确认" in result["error"]


def test_revise_without_pending_id_uses_only_draft():
    svc = StubRuleService()
    tools, session = _make_deps_and_tools(rule_service=svc, registry=StubRegistry())
    created = _create(tools, session)

    result = _run(tools["automation_rule_revise"]({"instruction": "改成35度"}, session))

    assert result["status"] == "pending_confirm"
    assert result["pending_id"] == created["pending_id"]
    assert result["rule"]["condition"] == "温度高于35度"


def test_revise_still_requires_instruction():
    tools, session = _make_deps_and_tools(rule_service=StubRuleService(),
                                          registry=StubRegistry())
    _create(tools, session)

    result = _run(tools["automation_rule_revise"]({"instruction": "  "}, session))

    assert "error" in result
    assert "instruction" in result["error"]


def test_confirm_and_revise_no_longer_require_pending_id():
    """schema 里 pending_id 必须非必填，否则模型只能编一个 id 出来。"""
    deps = SimpleNamespace(
        mcp_client_manager=MCPClientManager(),
        ha_service=None, ha_client_ref=[AsyncStubClient()],
        rule_service=None, rule_registry_service=None, automation_service=None,
    )
    _register_automation_rule_tools(deps)
    schemas = {t.tool_name: t.parameters
               for t in deps.mcp_client_manager.list_tools()}

    assert "pending_id" not in schemas["automation_rule_confirm"]["required"]
    assert schemas["automation_rule_revise"]["required"] == ["instruction"]


def test_create_note_is_channel_neutral():
    """网页端会自动弹确认框，note 不能只教模型等用户口头确认。"""
    tools, session = _make_deps_and_tools(rule_service=StubRuleService())

    note = _create(tools, session)["note"]

    assert "尚未创建" in note
    assert "网页端" in note


# ---------------------------------------------------------------------------
# needs_camera — 视觉规则缺摄像头绑定时的信号与措辞
# ---------------------------------------------------------------------------

class _TypedRuleService(StubRuleService):
    """按指定规则返回的桩（模块级 RULE 是 type=weather 的高温开空调，测不了视觉分支）。"""

    def __init__(self, rule):
        self._rule = rule

    async def build_rule(self, text, user_id="", camera_id=""):
        return {**self._rule, "summary": text, "camera_id": camera_id}

    async def revise_rule(self, rule, instruction, user_id=""):
        return {"rule": dict(self._rule), "summary": "改了"}


class _VisionToWeatherService(StubRuleService):
    """建出来是未绑定的视觉规则，改完变成天气规则。

    revise 返回值里 camera_id 已清空 —— 那是 rule_service._resolve_revised_camera
    的职责（type 转成 time/weather 时清掉无意义的绑定），工具层只如实上报。
    """

    async def build_rule(self, text, user_id="", camera_id=""):
        return {**RULE, "type": "vision", "condition": "画面里有人",
                "summary": text, "camera_id": camera_id}

    async def revise_rule(self, rule, instruction, user_id=""):
        return {"rule": {**rule, "type": "weather", "condition": "下雨", "camera_id": ""},
                "summary": "改成下雨触发"}


VISION_UNBOUND = {**RULE, "type": "vision", "condition": "画面里有人"}


def test_create_flags_needs_camera_for_unbound_vision_rule():
    tools, session = _make_deps_and_tools(rule_service=_TypedRuleService(VISION_UNBOUND))

    result = _create(tools, session, text="有人就开灯")

    assert result["needs_camera"] is True


def test_create_needs_camera_note_does_not_list_cameras():
    """刻意不给模型摄像头清单：网页弹窗自带选择器，念一遍只会和界面重复。"""
    tools, session = _make_deps_and_tools(rule_service=_TypedRuleService(VISION_UNBOUND))

    note = _create(tools, session, text="有人就开灯")["note"]

    assert "needs_camera" in note
    assert "不必追问" in note
    assert "不要逐个念摄像头名字" in note


def test_create_weather_rule_needs_no_camera():
    """模块级 RULE 就是 type=weather：非视觉规则不该被要求绑摄像头。"""
    tools, session = _make_deps_and_tools(rule_service=StubRuleService())

    result = _create(tools, session)

    assert result["needs_camera"] is False
    assert "needs_camera" not in result["note"]


def test_create_with_camera_id_already_bound():
    tools, session = _make_deps_and_tools(rule_service=_TypedRuleService(VISION_UNBOUND))

    result = _run(tools["automation_rule_create"](
        {"text": "有人就开灯", "camera_id": "cam_2"}, session))

    assert result["needs_camera"] is False
    assert result["rule"]["camera_id"] == "cam_2"


def test_revise_flips_needs_camera_when_type_leaves_vision():
    """视觉 → 天气：绑定随之失去意义，needs_camera 应从 True 翻到 False。"""
    tools, session = _make_deps_and_tools(rule_service=_VisionToWeatherService())
    created = _create(tools, session, text="有人就开灯")
    assert created["needs_camera"] is True

    result = _run(tools["automation_rule_revise"]({"instruction": "改成下雨天"}, session))

    assert result["needs_camera"] is False
    assert result["rule"]["type"] == "weather"
    assert result["rule"]["camera_id"] == ""


def test_trigger_by_name_executes_without_condition():
    automation = StubAutomation()
    registry = StubRegistry()
    registry.add_rule(RULE, user_id="u1")
    tools, session = _make_deps_and_tools(automation=automation, registry=registry)

    result = _run(tools["automation_rule_trigger"]({"name": "高温开空调"}, session))

    assert result["success"] is True
    assert result["executed"] == 1
    assert automation.triggered == ["rule-1"]


def test_trigger_unknown_name_lists_candidates():
    registry = StubRegistry()
    registry.add_rule(RULE, user_id="u1")
    tools, session = _make_deps_and_tools(automation=StubAutomation(),
                                          registry=registry)

    result = _run(tools["automation_rule_trigger"]({"name": "不存在的"}, session))

    assert "error" in result
    assert "高温开空调" in result["candidates"]


def test_list_returns_brief_entries():
    registry = StubRegistry()
    registry.add_rule(RULE, user_id="u1")
    tools, session = _make_deps_and_tools(
        registry=registry,
        name_map={"climate.bedroom": "卧室空调"})

    result = _run(tools["automation_rule_list"]({}, session))

    assert result["count"] == 1
    assert result["rules"][0]["name"] == "高温开空调"
    # actions 只回摘要（entity_id/设备名/描述，供模型答"控制的设备/id 是哪个"），
    # 不回完整 mcp_tool_input
    actions = result["rules"][0]["actions"]
    assert actions[0]["entity_id"] == "climate.bedroom"
    assert actions[0]["device_name"] == "卧室空调"
    assert actions[0]["description"] == "打开卧室空调"
    assert "mcp_tool_input" not in str(actions)


def test_delete_removes_rule():
    registry = StubRegistry()
    registry.add_rule(RULE, user_id="u1")
    tools, session = _make_deps_and_tools(registry=registry)

    result = _run(tools["automation_rule_delete"]({"rule_id": "rule-1"}, session))
    missing = _run(tools["automation_rule_delete"]({"rule_id": "rule-1"}, session))

    assert result["success"] is True
    assert "error" in missing


# ---------------------------------------------------------------------------
# 幻觉设备的自动修复边界：修好了透明告知，修不好拦截不出死局草稿
# ---------------------------------------------------------------------------

def test_create_blocks_when_auto_match_finds_nothing():
    """零匹配（validation_errors）→ tool_error 附候选，不建草稿。"""

    class _UnfixableSvc(StubRuleService):
        async def build_rule(self, text, user_id="", camera_id=""):
            return {**RULE, "validation_errors": [
                "动作1: entity_id 'cover.front_door' 不存在"]}

    registry = StubRegistry()
    tools, session = _make_deps_and_tools(
        rule_service=_UnfixableSvc(), registry=registry,
        all_devices=[{"entity_id": "switch.da_men", "name": "大门开关"},
                     {"entity_id": "sensor.men_ci", "name": "门磁"}])

    result = _create(tools, session, text="有人就开大门")

    assert "error" in result
    assert "不存在" in result["error"]
    # 候选只给主控设备（传感器不念），供模型转告用户
    assert "大门开关" in result["candidates"]
    assert all("传感器" not in c for c in result["candidates"])
    assert registry.saved == []  # 死局草稿不落暂存区
    assert session.pending_confirmations == {}


def test_create_notes_auto_correction_for_review():
    """自动替换成功 → 草稿照常生成，note 必须点明替换让用户核对。"""

    class _RepairedSvc(StubRuleService):
        async def build_rule(self, text, user_id="", camera_id=""):
            return {**RULE, "auto_corrections": [
                {"action_index": 0, "from": "cover.front_door",
                 "to": "switch.da_men", "to_name": "大门开关", "query": "打开大门"}]}

    tools, session = _make_deps_and_tools(rule_service=_RepairedSvc(),
                                          registry=StubRegistry())

    result = _create(tools, session, text="创建规则：有人就开大门")

    assert result["status"] == "pending_confirm"
    assert "自动替换" in result["note"]
    assert "大门开关" in result["note"]
    assert "cover.front_door" in result["note"]
    # auto_corrections 随 rule 进入草稿，弹窗据此渲染核对横幅
    assert session.pending_confirmations[result["pending_id"]]["rule"]["auto_corrections"]
