"""Tests for call_service 消歧闸门 —— classify_target 分层在工具层的落地。

核心约束（每条都有专门用例守着）：
1. ambiguous/category_miss 返回 **need_selection**，且返回体不得含 "error" 键 ——
   否则 langchain_tools 会加 "Error:" 前缀 → is_error → dispatcher 失败重试回路
   会逼模型「修正」自己再猜一个实体，正好是要修的行为。
2. exact/all_marker 把 entity_id **扩展**成整组，但只在 LLM 所选 domain 内扩展
   （「开大门」不得顺手把 lock.大门 一起开了）。
3. none 层放行（保住「太热了→开空调」）。
4. 黑名单实体不进候选。
5. 闸门自身异常一律放行（沿用既有口径）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 与虚拟设备模拟器同名同 id（ha_config/mqtt/lights.yaml + config.json 白名单），
# 让单测口径和 Task 8 的手测清单一致
THREE_LIGHTS = [
    ("床头灯",   [("床头灯", "light.chuang_tou_deng", "卧室", "off")]),
    ("厨房灯",   [("厨房灯", "light.chu_fang_deng", "厨房", "off")]),
    ("客厅吊灯", [("客厅吊灯", "light.ke_ting_diao_deng", "客厅", "off")]),
]

# 一设备多可控子实体（真实 MIoT 命名形态）
A_LAMP = [("A灯", [
    ("A灯 总开关", "switch.a_bk_onoff", "公司", "off"),
    ("A灯 第一键", "switch.a_first_key", "公司", "off"),
    ("A灯 会客厅灯 左键", "switch.a_on_p2", "公司", "off"),
    ("A灯 会客厅灯 右键", "switch.a_on_p3", "公司", "off"),
    ("A灯 第二键", "switch.a_second_key", "公司", "off"),
])]

# 同名跨 domain：设备「大门」下 switch + lock
DA_MEN = [("大门", [
    ("大门", "switch.da_men", "院子", "off"),
    ("大门", "lock.da_men", "院子", "locked"),
])]

# 多意图一轮：灯（歧义）+ 窗帘（唯一）
LIGHTS_AND_COVER = THREE_LIGHTS + [
    ("客厅窗帘", [("客厅窗帘", "cover.ke_ting_chuang_lian", "客厅", "open")]),
]


def _flat(name: str, eid: str, area: str, state: str) -> dict:
    return {"entity_id": eid, "domain": eid.split(".")[0], "name": name,
            "state": state, "attributes": {"friendly_name": name},
            "area_id": "area-1", "area_name": area}


@pytest.fixture(autouse=True)
def _init_db(tmp_path, monkeypatch):
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")


def _build_tool(devices):
    """devices: [(设备名, [(实体名, entity_id, 区域, 状态), ...]), ...] → call_service 工具。"""
    from app.mcp.mcp_client_manager import MCPClientManager
    from app.tools import ToolDeps, _register_ha_call_service

    flat, grouped = [], []
    for dev_name, ents in devices:
        dev_flat = [_flat(*e) for e in ents]
        flat.extend(dev_flat)
        grouped.append({
            "device_id": f"dev-{dev_name}", "name": dev_name, "model": None,
            "manufacturer": None, "sw_version": None,
            "area_id": "area-1", "area_name": ents[0][2], "summary": "",
            "entities": [
                {"entity_id": e["entity_id"], "domain": e["domain"], "name": e["name"],
                 "state": e["state"], "attributes": e["attributes"]} for e in dev_flat
            ],
        })
    states = [{"entity_id": e["entity_id"], "state": e["state"], "attributes": e["attributes"]}
              for e in flat]

    ha_service = MagicMock()
    ha_service.get_all_devices = AsyncMock(return_value=flat)
    ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": grouped})
    ha_service.get_states_snapshot = AsyncMock(return_value=states)
    ha_service.invalidate_states_cache = MagicMock()
    ha_client = MagicMock()
    ha_client.get_states = AsyncMock(return_value=states)

    mgr = MCPClientManager()
    deps = ToolDeps(mcp_client_manager=mgr, vision_client=MagicMock(),
                    ha_service=ha_service, ha_client_ref=[ha_client])
    _register_ha_call_service(deps)
    return mgr.get_tool("ha_devices___call_service")


def _session(query: str):
    session = MagicMock()
    session.current_query = query
    session.pending_confirmations = {}
    return session


@pytest.fixture
def captured():
    """捕获实际下发给 HA 的 (domain, service, entity_id, data)。"""
    box = {}

    async def fake_call(hc, domain, service, eid, data):
        box["domain"] = domain
        box["service"] = service
        box["entity_id"] = eid
        box["data"] = data
        return {}

    box["call"] = fake_call
    return box


async def _run(tool, session, captured, **params):
    with patch("app.tools.call_with_probe", new=captured["call"]):
        return await tool.handler(params, session)


# ---------------------------------------------------------------------------
# ambiguous / category_miss → 转用户选择
# ---------------------------------------------------------------------------

class TestNeedSelection:
    @pytest.mark.asyncio
    async def test_ambiguous_returns_need_selection_without_touching_ha(self, captured):
        """「开灯」命中 3 盏不同名灯 → 不执行、不猜，转用户选择。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        session = _session("开灯")
        result = await _run(tool, session, captured,
                            domain="light", service="turn_on", entity_id="light.chuang_tou_deng")
        assert result["status"] == "need_selection"
        assert result["reason"] == "ambiguous"
        assert result["success"] is False
        assert "entity_id" not in captured          # HA 一次都没被调用
        assert {c["entity_id"] for c in result["candidates"]} == {
            "light.chuang_tou_deng", "light.chu_fang_deng", "light.ke_ting_diao_deng"}
        # 草稿已挂上会话，供 REST/弹框确认
        assert result["pending_id"] in session.pending_confirmations

    @pytest.mark.asyncio
    async def test_need_selection_has_no_error_key(self, captured):
        """哨兵：返回体含 "error" 键就会被判为工具失败 → 触发失败重试回路。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        result = await _run(tool, _session("开灯"), captured,
                            domain="light", service="turn_on", entity_id="light.chuang_tou_deng")
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_need_selection_is_not_tool_error_after_langchain_conversion(self, captured):
        """哨兵（端到端）：过一遍真实的 langchain 转换，输出不得以 "Error:" 开头。

        langchain_tools._coroutine 见 "error" 键就加前缀 → langgraph_agent 判 is_error
        → dispatcher 塞进 failed_tools 触发重试轮，模型会被逼着再猜一个实体。
        """
        from app.core.database import Database
        from app.mcp.langchain_tools import mcp_to_langchain_tool
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        session = _session("开灯")
        lc = mcp_to_langchain_tool(tool)
        with patch("app.tools.call_with_probe", new=captured["call"]):
            out = await lc.coroutine(
                {"configurable": {"session": session}},
                domain="light", service="turn_on", entity_id="light.chuang_tou_deng",
            )
        assert isinstance(out, str)
        assert not out.startswith("Error:")
        assert "need_selection" in out

    @pytest.mark.asyncio
    async def test_ambiguous_even_when_llm_batched_all(self, captured):
        """模型自己把 3 盏灯全批量下发了也要拦 —— 歧义判定依据 query，不是模型的选择。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        result = await _run(tool, _session("开灯"), captured,
                            domain="light", service="turn_on",
                            entity_id="light.chuang_tou_deng,light.chu_fang_deng")
        assert result["status"] == "need_selection"
        assert "entity_id" not in captured

    @pytest.mark.asyncio
    async def test_category_miss_for_nonexistent_device(self, captured):
        """「月球的灯」→ 不许从全量目录里抓一个，如实说没找到 + 给同品类候选。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        result = await _run(tool, _session("打开月球的灯"), captured,
                            domain="light", service="turn_on", entity_id="light.chuang_tou_deng")
        assert result["status"] == "need_selection"
        assert result["reason"] == "category_miss"
        assert "没有找到" in result["notice"]
        assert "entity_id" not in captured

    @pytest.mark.asyncio
    async def test_hint_forbids_guessing_and_retrying(self, captured):
        """hint 是给模型的下一步指引：必须明确禁止自己挑、禁止重试、禁止谎报已执行。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        result = await _run(tool, _session("开灯"), captured,
                            domain="light", service="turn_on", entity_id="light.chuang_tou_deng")
        hint = result["hint"]
        assert "不要" in hint
        assert "已执行" in hint          # 「不要声称已执行」

    @pytest.mark.asyncio
    async def test_prohibited_entity_not_in_candidates(self, captured):
        """禁控设备对 AI 不可见，也不能出现在弹框候选里。"""
        from app.core.database import Database
        await Database.init()
        await Database.get().emoji_pref_upsert("entity_operable", "light.chu_fang_deng", "0")
        tool = _build_tool(THREE_LIGHTS)
        result = await _run(tool, _session("开灯"), captured,
                            domain="light", service="turn_on", entity_id="light.chuang_tou_deng")
        assert result["status"] == "need_selection"
        assert "light.chu_fang_deng" not in {c["entity_id"] for c in result["candidates"]}


# ---------------------------------------------------------------------------
# exact / all_marker → 扩展成整组执行
# ---------------------------------------------------------------------------

class TestExpansion:
    @pytest.mark.asyncio
    async def test_device_level_exact_expands_to_all_sub_entities(self, captured):
        """「开A灯」→ 设备下 5 个子实体一起下发（模型只挑了 1 个也要补全）。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(A_LAMP)
        result = await _run(tool, _session("开A灯"), captured,
                            domain="switch", service="turn_on", entity_id="switch.a_bk_onoff")
        assert result["success"] is True
        assert set(captured["entity_id"].split(",")) == {
            "switch.a_bk_onoff", "switch.a_first_key", "switch.a_on_p2",
            "switch.a_on_p3", "switch.a_second_key",
        }

    @pytest.mark.asyncio
    async def test_expansion_never_crosses_domain(self, captured):
        """「开大门」→ 只开 switch，同名 lock 不得被带上（否则等于替用户解锁）。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(DA_MEN)
        result = await _run(tool, _session("开大门"), captured,
                            domain="switch", service="turn_on", entity_id="switch.da_men")
        assert result["success"] is True
        assert captured["entity_id"] == "switch.da_men"
        assert "lock.da_men" not in captured["entity_id"]

    @pytest.mark.asyncio
    async def test_all_marker_expands_to_whole_matched_set(self, captured):
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        result = await _run(tool, _session("把所有灯关掉"), captured,
                            domain="light", service="turn_off", entity_id="light.chuang_tou_deng")
        assert result["success"] is True
        assert set(captured["entity_id"].split(",")) == {
            "light.chuang_tou_deng", "light.chu_fang_deng", "light.ke_ting_diao_deng"}

    @pytest.mark.asyncio
    async def test_expansion_keeps_semantic_mapping_consensus(self, captured):
        """扩展后的批量仍走「全部实体共识才替换 service」，不得因扩展而误伤。"""
        import json

        from app.core.database import Database
        from app.services.semantic_map import invalidate_cache
        await Database.init()
        payload = json.dumps({"mappings": {
            "turn_on": {"target": "turn_off", "description": "继电器反转"}}})
        # 只给其中一个子实体配映射 → 扩展后无共识 → 保持原 service
        await Database.get().emoji_pref_upsert("entity_action_map", "switch.a_bk_onoff", payload)
        invalidate_cache()
        tool = _build_tool(A_LAMP)
        result = await _run(tool, _session("开A灯"), captured,
                            domain="switch", service="turn_on", entity_id="switch.a_bk_onoff")
        assert result["success"] is True
        assert captured["service"] == "turn_on"
        assert "semantic_mapping" not in result


# ---------------------------------------------------------------------------
# unique / none → 照旧执行
# ---------------------------------------------------------------------------

class TestPassThrough:
    @pytest.mark.asyncio
    async def test_unique_executes_as_is(self, captured):
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        result = await _run(tool, _session("开客厅吊灯"), captured,
                            domain="light", service="turn_on", entity_id="light.ke_ting_diao_deng")
        assert result["success"] is True
        assert "status" not in result
        assert captured["entity_id"] == "light.ke_ting_diao_deng"

    @pytest.mark.asyncio
    async def test_area_qualified_query_narrowed_to_unique(self, captured):
        """「把客厅的灯关了」→ area 兜底轮命中整个客厅，品类尾词收窄回吊灯一个。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(LIGHTS_AND_COVER)
        result = await _run(tool, _session("把客厅的灯关了"), captured,
                            domain="light", service="turn_off", entity_id="light.ke_ting_diao_deng")
        assert result["success"] is True
        assert captured["entity_id"] == "light.ke_ting_diao_deng"

    @pytest.mark.asyncio
    async def test_wrong_domain_still_rejected(self, captured):
        """用户说灯、模型去开风扇 → 品类尾词收窄后 target 不在候选内，按语义错配拒绝。"""
        from app.core.database import Database
        await Database.init()
        devices = THREE_LIGHTS + [("客厅风扇", [("客厅风扇", "fan.ke_ting_feng_shan", "客厅", "off")])]
        tool = _build_tool(devices)
        result = await _run(tool, _session("把客厅的灯关了"), captured,
                            domain="fan", service="turn_on", entity_id="fan.ke_ting_feng_shan")
        assert result["success"] is False
        assert "error" in result                 # 这条是真失败，该走重试回路
        assert "entity_id" not in captured

    @pytest.mark.asyncio
    async def test_implicit_intent_passes_through(self, captured):
        """「太热了」无任何设备线索 → 放行给模型推断空调（不得因为消歧而失能）。"""
        from app.core.database import Database
        await Database.init()
        devices = THREE_LIGHTS + [("中央空调", [("中央空调", "climate.zhong_yang_kong_diao", "客厅", "off")])]
        tool = _build_tool(devices)
        result = await _run(tool, _session("太热了"), captured,
                            domain="climate", service="turn_on",
                            entity_id="climate.zhong_yang_kong_diao")
        assert result["success"] is True
        assert "entity_id" in captured

    @pytest.mark.asyncio
    async def test_gate_failure_fails_open(self, captured):
        """索引/判定抛异常 → 放行（沿用既有「校验失败放行」口径，不因闸门故障锁死全屋）。"""
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        with patch("app.services.device_registry.build_match_index",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await _run(tool, _session("开灯"), captured,
                                domain="light", service="turn_on",
                                entity_id="light.chuang_tou_deng")
        assert result["success"] is True
        assert captured["entity_id"] == "light.chuang_tou_deng"


# ---------------------------------------------------------------------------
# 草稿清场
# ---------------------------------------------------------------------------

class TestDraftLifecycle:
    @pytest.mark.asyncio
    async def test_stale_draft_dropped_on_clean_resolution(self, captured):
        """语音用户被问「要开哪个」后直接说设备名 → 上一轮的草稿要被清掉。"""
        from app.core.database import Database
        from app.services.pending_selections import KIND_DEVICE_SELECTION
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        session = _session("开灯")
        await _run(tool, session, captured,
                   domain="light", service="turn_on", entity_id="light.chuang_tou_deng")
        stale = list(session.pending_confirmations)[0]
        assert session.pending_confirmations[stale]["kind"] == KIND_DEVICE_SELECTION

        # 用户下一轮直接说设备名
        session.current_query = "客厅吊灯"
        result = await _run(tool, session, captured,
                            domain="light", service="turn_on", entity_id="light.ke_ting_diao_deng")
        assert result["success"] is True
        assert stale not in session.pending_confirmations

    @pytest.mark.asyncio
    async def test_compound_sentence_is_not_gated(self, captured):
        """已知局限：复合句整句放行，句内的歧义不受闸门保护。

        「开灯关窗帘」含 1 个「开」+ 1 个「关」→ classify_target 判为复合句 →
        none 层放行。归一化是按单设备词设计的，硬判会剥成「灯关窗帘」（两轮子串
        匹配全空）→ 误落 category_miss，弹框文案还会把归一化中间态念给用户听。
        逐子句切分是另一件事，本闸门不做；此处断言的是当前**有意**的取舍：
        复合句里的「开灯」仍由模型自己挑一盏（旧行为），不弹框。
        """
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(LIGHTS_AND_COVER)
        session = _session("开灯关窗帘")
        result = await _run(tool, session, captured,
                            domain="light", service="turn_on", entity_id="light.chuang_tou_deng")
        assert "status" not in result
        assert result["success"] is True
        assert captured["entity_id"] == "light.chuang_tou_deng"

    @pytest.mark.asyncio
    async def test_repeated_ambiguous_call_keeps_both_drafts(self, captured):
        """模型就同一句歧义指令连调两次 → 两份草稿都在，前端拿到的 pending_id 不会失效。

        同轮内「建草稿后又干净解决」的路径当前不可达（tier 只由 query 决定，
        歧义就一定提前 return），drop_selection_drafts 的 except_query 保护由
        tests/test_pending_selections.py 直接对函数做单测覆盖。
        """
        from app.core.database import Database
        await Database.init()
        tool = _build_tool(THREE_LIGHTS)
        session = _session("开灯")
        first = await _run(tool, session, captured,
                           domain="light", service="turn_on", entity_id="light.chuang_tou_deng")
        second = await _run(tool, session, captured,
                            domain="light", service="turn_on", entity_id="light.chu_fang_deng")
        assert first["status"] == "need_selection"
        assert second["status"] == "need_selection"
        assert first["pending_id"] in session.pending_confirmations
        assert second["pending_id"] in session.pending_confirmations
        assert "entity_id" not in captured
