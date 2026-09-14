"""创建规则关键词门控测试：wants_rule_creation 判定 / 工具层硬拦 / 提示词软推。

背景：glm-4-flash 对「如果…就…」条件式话术经常误触 automation_rule_create
（或零工具调用幻觉"已创建"）。产品决策：只有用户消息明确出现「创建规则」类
字样才创建；普通条件式描述按普通指令执行。实现分两层，共用同一个判定函数：
- 硬门：automation_rule_create handler 读 session.current_query，未命中直接拒绝；
- 软推：prompt_service 在命中时注入「本轮必须走工具、别直接执行」的指令。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.pending_rules import wants_rule_creation
from app.services.prompt_service import build_system_prompt
from app.mcp.mcp_client_manager import MCPClientManager
from app.tools import _register_automation_rule_tools

from tests.test_automation_rule_tools import (
    AsyncStubClient,
    StubRegistry,
    StubRuleService,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------- wants_rule_creation 判定 ----------

def test_gate_hits_explicit_creation_phrases():
    for text in (
        "创建一条规则：温度高于30度就开空调",
        "帮我创建规则，有人就开灯",
        "新建一个规则：日落开灯",
        "给厨房灯建个规则",
        "添加规则：下雨关窗",
        "帮我设个规则，有人就开灯",
        "建条规则 有人回来就开灯",
    ):
        assert wants_rule_creation(text), text


def test_gate_skips_plain_conditional_and_other_features():
    for text in (
        "如果有人就打开客厅吊灯",
        "温度高于30度就开空调",
        "有人经过就开灯",
        "把灯都关了",
        "现在几点了",
        "创建场景：回家模式",              # 别的功能的「创建」，不能抢
        "创建一个定时任务，每天八点提醒",  # 定时任务是另一条路径
    ):
        assert not wants_rule_creation(text), text


def test_gate_empty_and_none_safe():
    assert not wants_rule_creation("")
    assert not wants_rule_creation(None)


# ---------- 工具层硬门 ----------

def _make_tools(session_query: str):
    class _HAService:
        async def get_states_snapshot(self):
            return [{"entity_id": "climate.bedroom"}]

    deps = SimpleNamespace(
        mcp_client_manager=MCPClientManager(),
        ha_service=_HAService(),
        ha_client_ref=[AsyncStubClient()],
        rule_service=StubRuleService(),
        rule_registry_service=StubRegistry(),
        automation_service=None,
    )
    _register_automation_rule_tools(deps)
    tools = {t.tool_name: t.handler for t in deps.mcp_client_manager.list_tools()}
    session = SimpleNamespace(user_id="u1", pending_confirmations={},
                              current_query=session_query)
    return tools, session


def test_create_rejected_without_keyword_in_current_query():
    tools, session = _make_tools("如果有人就打开厨房灯")

    result = _run(tools["automation_rule_create"](
        {"text": "有人就开厨房灯"}, session))

    assert "error" in result
    assert "没有明确要求创建规则" in result["error"]
    assert "创建规则" in result.get("hint", "")
    assert session.pending_confirmations == {}


def test_create_rejected_when_current_query_missing():
    # 没经过 dispatch 的异常路径拿不到 current_query，按未命中处理（宁可拒）
    session = SimpleNamespace(user_id="u1", pending_confirmations={})
    tools, _ = _make_tools("x")

    result = _run(tools["automation_rule_create"]({"text": "有人就开灯"}, session))

    assert "error" in result


def test_create_allowed_with_keyword_in_current_query():
    tools, session = _make_tools("帮我创建一条规则：有人就开厨房灯")

    result = _run(tools["automation_rule_create"](
        {"text": "有人就开厨房灯"}, session))

    assert result["status"] == "pending_confirm"
    assert result["pending_id"] in session.pending_confirmations


def test_revise_not_gated_by_keyword():
    # 草稿存在后，口头「改成…」走 revise，不要求当前消息带创建关键词
    tools, session = _make_tools("帮我创建一条规则：温度高于30度就开空调")
    created = _run(tools["automation_rule_create"](
        {"text": "温度高于30度就开空调"}, session))

    session.current_query = "把阈值改成35度"  # 无创建关键词
    revised = _run(tools["automation_rule_revise"](
        {"pending_id": created["pending_id"], "instruction": "改成35度"}, session))

    assert revised["status"] == "pending_confirm"
    assert revised["rule"]["condition"] == "温度高于35度"


# ---------- 提示词软推 ----------

def _build_prompt(query):
    async def _go():
        with patch("app.services.weather_service.get_weather", new=AsyncMock(return_value=None)), \
             patch("app.services.weather_service.format_weather_detail", create=True,
                   new=lambda *_a, **_k: ""):
            return await build_system_prompt(query=query)
    return _run(_go())


def test_prompt_injects_creation_directive_on_keyword():
    prompt = _build_prompt("帮我创建一条规则：有人就开厨房灯")
    assert "本轮指令" in prompt
    assert "automation_rule_create" in prompt
    assert "不要直接执行设备动作" in prompt


def test_prompt_no_creation_directive_on_plain_conditional():
    # 无关键词回合：系统提示对"规则创建"零注入——连「不要创建规则」这种
    # 反向提示都不许有（实测它会反向教会模型输出工具调用文本）。
    # 只断言本模块的注入段不存在；persona/guidelines 是用户配置，不管。
    prompt = _build_prompt("如果有人就打开厨房灯")
    assert "本轮指令" not in prompt


# ---------- dispatcher 变体：无关键词且无草稿的回合，规则概念整族不可见 ----------

def _mk_dispatcher():
    from app.agents.dispatcher import Dispatcher
    session_store = SimpleNamespace()
    dispatcher = Dispatcher(session_store=session_store, agent=object())
    dispatcher._tools = [
        SimpleNamespace(name="automation_rule_create"),
        SimpleNamespace(name="automation_rule_confirm"),
        SimpleNamespace(name="automation_rule_list"),
        SimpleNamespace(name="get_entities"),
    ]
    return dispatcher


def test_variant_tools_clean_strips_whole_family():
    dispatcher = _mk_dispatcher()
    kept = [t.name for t in dispatcher._tools_for_variant("clean")]
    assert kept == ["get_entities"]           # automation_rule_* 整族消失
    assert [t.name for t in dispatcher._tools_for_variant("no_create")] == [
        "automation_rule_confirm", "automation_rule_list", "get_entities"]
    assert dispatcher._tools_for_variant("full") is dispatcher._tools


def test_pick_variant_keyword_draft_clean():
    dispatcher = _mk_dispatcher()
    live = SimpleNamespace(pending_confirmations={
        "abc": {"kind": "automation_rule", "rule": {"name": "x"}, "created_at": 1 << 40},
    })
    assert dispatcher._pick_variant(live, "创建一条规则：有人开灯") == "full"
    assert dispatcher._pick_variant(live, "确认") == "no_create"      # 有活草稿
    assert dispatcher._pick_variant(live, "改成35度") == "no_create"
    empty = SimpleNamespace(pending_confirmations={})
    assert dispatcher._pick_variant(empty, "如果有人就打开厨房灯") == "clean"
    assert dispatcher._pick_variant(empty, "今天天气怎样") == "clean"
    assert dispatcher._pick_variant(empty, "把灯都关了") == "clean"


def test_global_clean_agent_lazy_built_and_cached():
    dispatcher = _mk_dispatcher()
    variant = object()
    builds = []

    def fake_build(tools, model_config=None):
        builds.append(list(tools))
        return variant, (SimpleNamespace(), SimpleNamespace())

    async def _go():
        with patch("app.agents.dispatcher.build_chat_agent", side_effect=fake_build):
            first = await dispatcher._get_agent("", variant="clean")
            second = await dispatcher._get_agent("", variant="clean")
            full = await dispatcher._get_agent("", variant="full")
        return first, second, full

    first, second, full = _run(_go())
    assert first is variant and second is variant   # 第二次命中缓存
    assert len(builds) == 1
    assert [t.name for t in builds[0]] == ["get_entities"]  # automation_rule_* 整族剔除
    assert full is dispatcher._agent                 # 关键词回合仍用全量全局


def test_user_clean_agent_uses_filtered_tools():
    dispatcher = _mk_dispatcher()
    variant = object()
    captured = {}

    def fake_build(tools, model_config=None):
        captured["tools"] = list(tools)
        return variant, (SimpleNamespace(), SimpleNamespace())

    async def _go():
        cfg = {"base_url": "http://x", "model": "m", "api_key": "k"}
        with patch("app.agents.dispatcher.load_model_config_for_user",
                   new=AsyncMock(return_value=cfg)), \
             patch("app.agents.dispatcher.build_chat_agent", side_effect=fake_build):
            return await dispatcher._get_agent("u1", variant="clean")

    assert _run(_go()) is variant
    assert [t.name for t in captured["tools"]] == ["get_entities"]
    assert ("u1", "clean") in dispatcher._user_agents


def test_user_clean_build_failure_falls_back_without_deadlock():
    # 回归：per-user 裁剪变体构建失败时，旧实现持锁递归 _get_agent，
    # 非重入锁自锁死（整轮聊天挂起）。修复后必须立即回全量全局。
    dispatcher = _mk_dispatcher()
    cfg = {"base_url": "http://x", "model": "m", "api_key": "k"}

    def broken_build(tools, model_config=None):
        raise RuntimeError("no key")

    async def _go():
        with patch("app.agents.dispatcher.load_model_config_for_user",
                   new=AsyncMock(return_value=cfg)), \
             patch("app.agents.dispatcher.build_chat_agent", side_effect=broken_build):
            return await dispatcher._get_agent("u1", variant="clean")

    assert _run(_go()) is dispatcher._agent


# ---------- validator 声明核查：声称建了规则但没调创建工具 = 必然撒谎 ----------

def test_rule_create_claim_regex_hits_completion_phrases():
    from app.agents.validator_agent import ValidatorAgent
    for text in (
        "自动化规则已创建成功，当有人进入时，阳台灯会自动打开。",
        "好的，我已为您创建了一条自动化规则：有人时开灯。请确认。",
        "规则创建成功了",
        "已经创建好规则",
        'automation_rule_create\n{"instruction": {"description": "如果有人就打开阳台灯"}}',
    ):
        assert ValidatorAgent.has_rule_create_claim(text), text


def test_rule_create_claim_regex_spares_teaching_and_blocked_replies():
    from app.agents.validator_agent import ValidatorAgent
    for text in (
        "这条规则靠摄像头画面判断，但还没绑定看哪一路，不能创建。",
        "要创建规则的话，可以说『创建规则：有人就开灯』。",
        "规则页里可以新建规则，点右上角按钮即可。",
        "这条规则还没有创建，确认后才生效。",
    ):
        assert not ValidatorAgent.has_rule_create_claim(text), text


def test_rule_create_claim_retry_message_content():
    from app.agents.validator_agent import ValidatorAgent
    msg = ValidatorAgent.build_rule_create_claim_retry_message("自动化规则已创建成功")
    assert "没有任何规则被创建" in msg.content
    assert "创建规则" in msg.content
    assert "不要" in msg.content or "绝对不要" in msg.content
