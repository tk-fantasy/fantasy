"""Tests for device_registry — 统一设备注册表（AI 视图与闸门候选同源）。

覆盖：
- sub_name（子实体短名）注入三视图（catalog/controls/get_entities brief）
- 单可控实体设备不带 sub_name（MIoT 噪声回归面最小化）
- 禁止设备（entity_operable 黑名单）三视图全部隐藏
- match_devices 候选反查能按子功能名命中（"打开会客厅的灯"→ 会客厅灯 左键）
- call_service 拒绝编造 entity_id 时报错附候选（自愈回路）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _init_db(tmp_path, monkeypatch):
    """每个测试一个临时 DB 单例（entity_operable/entity_note 写入用）。"""
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")


# ckcper 五开关：A灯，多可控实体，子实体名带父设备名前缀（真实命名形态）
A_LAMP_SUBS = [
    ("switch.a_bk_onoff", "A灯 总开关"),
    ("switch.a_first_key", "A灯 第一键"),
    ("switch.a_on_p2", "A灯 会客厅灯 左键"),
    ("switch.a_on_p3", "A灯 会客厅灯 右键"),
    ("switch.a_second_key", "A灯 第二键"),
]


def _flat(eid: str, name: str, area: str = "公司") -> dict:
    return {
        "entity_id": eid, "domain": eid.split(".")[0], "name": name,
        "state": "off", "attributes": {"friendly_name": name},
        "area_id": "area-1", "area_name": area,
    }


def _make_ha_service(devices: list[tuple[str, list[tuple[str, str]], str | None]]) -> MagicMock:
    """devices: [(设备名, [(entity_id, friendly_name), ...], model), ...] → flat+grouped mock。"""
    flat: list[dict] = []
    grouped: list[dict] = []
    for dev_name, ents, model in devices:
        dev_flat = [_flat(eid, name) for eid, name in ents]
        flat.extend(dev_flat)
        grouped.append({
            "device_id": f"dev-{dev_name}", "name": dev_name, "model": model,
            "manufacturer": None, "sw_version": None,
            "area_id": "area-1", "area_name": "公司",
            "summary": f"{len(ents)}个可控功能",
            "entities": [
                {**e, "domain": e["entity_id"].split(".")[0]} for e in dev_flat
            ],
        })
    ha_service = MagicMock()
    ha_service.get_all_devices = AsyncMock(return_value=flat)
    ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": grouped})
    ha_service.get_service_defs = AsyncMock(return_value={
        "switch": {"turn_on": {"fields": ["entity_id"]}, "turn_off": {"fields": ["entity_id"]}},
        "light": {"turn_on": {"fields": ["entity_id"]}, "turn_off": {"fields": ["entity_id"]}},
    })
    return ha_service


async def _build(devices):
    from app.services.device_registry import build_device_snapshot
    ha_service = _make_ha_service(devices)
    return await build_device_snapshot(ha_service, MagicMock()), ha_service


class TestSubNameInjection:
    """多可控实体设备：子实体短名进三视图，用户可用子功能名指称。"""

    @pytest.mark.asyncio
    async def test_controls_text_has_sub_name(self):
        snapshot, _ = await _build([("A灯", A_LAMP_SUBS, "ckcper.switch.bln002")])
        from app.services.device_registry import render_controls_text
        text = render_controls_text(snapshot)
        assert "子功能 switch.a_on_p2（会客厅灯 左键）:" in text
        assert "子功能 switch.a_on_p3（会客厅灯 右键）:" in text

    @pytest.mark.asyncio
    async def test_catalog_has_sub_name(self):
        snapshot, _ = await _build([("A灯", A_LAMP_SUBS, "ckcper.switch.bln002")])
        from app.services.device_registry import render_catalog_text
        catalog = render_catalog_text(snapshot)
        assert "- switch.a_on_p2 (类型:switch, 状态:off) 名称:A灯 会客厅灯 左键" in catalog
        # 行格式是 rule_service._parse_ha_catalog 的正则契约
        import re
        assert re.search(r"- (\S+) \(类型:(\w+), 状态:[^)]+\) 名称:(.+)", catalog)

    @pytest.mark.asyncio
    async def test_brief_entity_labels(self):
        snapshot, _ = await _build([("A灯", A_LAMP_SUBS, "ckcper.switch.bln002")])
        from app.services.device_registry import render_devices_brief
        brief = render_devices_brief(snapshot)
        a_lamp = next(d for d in brief if d["name"] == "A灯")
        assert a_lamp["entity_labels"]["switch.a_on_p2"] == "A灯 会客厅灯 左键"
        assert set(a_lamp["entity_ids"]) == {eid for eid, _ in A_LAMP_SUBS}

    @pytest.mark.asyncio
    async def test_single_controllable_no_sub_name(self):
        """单可控实体设备：名称仍是纯设备名（无子功能后缀，零回归）。"""
        snapshot, _ = await _build([("床头灯", [("light.bed", "床头灯")], None)])
        from app.services.device_registry import render_catalog_text, render_controls_text
        catalog = render_catalog_text(snapshot)
        assert "名称:床头灯" in catalog
        assert "床头灯 床头灯" not in catalog
        controls = render_controls_text(snapshot)
        assert "（" not in controls.split("子功能")[-1] if "子功能" in controls else True

    @pytest.mark.asyncio
    async def test_alias_without_prefix_is_sub_name(self):
        """别名不含父名（如直接叫"会客厅灯"）→ 整名作短名，有区分度。"""
        snapshot, _ = await _build([
            ("A灯", [("switch.a_on_p2", "会客厅灯"), ("switch.a_on_p3", "A灯 右键")], None),
        ])
        from app.services.device_registry import render_devices_brief
        brief = render_devices_brief(snapshot)
        labels = brief[0]["entity_labels"]
        assert labels["switch.a_on_p2"] == "A灯 会客厅灯"
        assert labels["switch.a_on_p3"] == "A灯 右键"


class TestProhibitedHidden:
    """禁止（entity_operable 黑名单）= 对 AI 不可见：三视图全排除。"""

    @pytest.mark.asyncio
    async def test_prohibited_hidden_in_all_views(self, tmp_path):
        from app.core.database import Database
        await Database.init()
        await Database.get().emoji_pref_upsert("entity_operable", "switch.a_on_p2", "0")

        snapshot, _ = await _build([("A灯", A_LAMP_SUBS, None)])
        from app.services.device_registry import (
            render_catalog_text, render_controls_text, render_devices_brief, render_entities_flat,
        )
        assert all(e["entity_id"] != "switch.a_on_p2" for e in snapshot["entries"])
        catalog = render_catalog_text(snapshot)
        assert "switch.a_on_p2" not in catalog
        assert "⛔" not in catalog  # 禁止即隐藏，不再输出标记
        controls = render_controls_text(snapshot)
        assert "switch.a_on_p2" not in controls
        brief = render_devices_brief(snapshot)
        a_lamp = next(d for d in brief if d["name"] == "A灯")
        assert "switch.a_on_p2" not in a_lamp["entity_ids"]
        assert "switch.a_on_p2" not in a_lamp["entity_labels"]
        flat = render_entities_flat(snapshot)
        assert all(e["entity_id"] != "switch.a_on_p2" for e in flat)


class TestCandidateLookup:
    """闸门候选反查与模型视野同源：子功能名可命中。"""

    @pytest.mark.asyncio
    async def test_match_devices_finds_sub_entity(self):
        from app.utils.text_match import match_devices
        snapshot, _ = await _build([("A灯", A_LAMP_SUBS, None)])
        matched = match_devices("打开会客厅的灯", snapshot["entries"])
        eids = {d["entity_id"] for d in matched}
        assert "switch.a_on_p2" in eids or "switch.a_on_p3" in eids

    @pytest.mark.asyncio
    async def test_call_service_rejection_includes_candidates(self, tmp_path):
        """编造 entity_id 被拒时，报错附注册表反查出的真实候选（自愈回路）。"""
        from app.core.database import Database
        await Database.init()
        ha_service = _make_ha_service([("A灯", A_LAMP_SUBS, None)])
        states = [{"entity_id": eid, "state": "off", "attributes": {}}
                  for eid, _ in A_LAMP_SUBS]
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=states)

        from app.tools import ToolDeps, _register_ha_call_service
        from app.mcp.mcp_client_manager import MCPClientManager
        mgr = MCPClientManager()
        deps = ToolDeps(
            mcp_client_manager=mgr, vision_client=MagicMock(),
            ha_service=ha_service, ha_client_ref=[ha_client],
        )
        _register_ha_call_service(deps)
        tool = mgr.get_tool("ha_devices___call_service")

        session = MagicMock()
        session.current_query = "打开会客厅的灯"
        result = await tool.handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.hall"}, session
        )
        assert result["success"] is False
        assert "switch.a_on_p2（A灯 会客厅灯 左键）" in result["error"]
        assert "重试一次" in result["error"]

    @pytest.mark.asyncio
    async def test_call_service_rejection_no_match_hint(self, tmp_path):
        """无候选时报"没有匹配到任何真实设备"，不再引导编造。"""
        from app.core.database import Database
        await Database.init()
        ha_service = _make_ha_service([("A灯", A_LAMP_SUBS, None)])
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[
            {"entity_id": eid, "state": "off", "attributes": {}} for eid, _ in A_LAMP_SUBS
        ])

        from app.tools import ToolDeps, _register_ha_call_service
        from app.mcp.mcp_client_manager import MCPClientManager
        mgr = MCPClientManager()
        deps = ToolDeps(
            mcp_client_manager=mgr, vision_client=MagicMock(),
            ha_service=ha_service, ha_client_ref=[ha_client],
        )
        _register_ha_call_service(deps)
        tool = mgr.get_tool("ha_devices___call_service")

        session = MagicMock()
        session.current_query = "打开火星基地的灯"
        result = await tool.handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.hall"}, session
        )
        assert result["success"] is False
        assert "没有匹配到任何真实设备" in result["error"]

    @pytest.mark.asyncio
    async def test_call_service_prohibited_not_in_candidates(self, tmp_path):
        """禁止设备不进候选（与模型视野一致）。"""
        from app.core.database import Database
        await Database.init()
        await Database.get().emoji_pref_upsert("entity_operable", "switch.a_on_p2", "0")
        ha_service = _make_ha_service([("A灯", A_LAMP_SUBS, None)])
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[
            {"entity_id": eid, "state": "off", "attributes": {}} for eid, _ in A_LAMP_SUBS
        ])

        from app.tools import ToolDeps, _register_ha_call_service
        from app.mcp.mcp_client_manager import MCPClientManager
        mgr = MCPClientManager()
        deps = ToolDeps(
            mcp_client_manager=mgr, vision_client=MagicMock(),
            ha_service=ha_service, ha_client_ref=[ha_client],
        )
        _register_ha_call_service(deps)
        tool = mgr.get_tool("ha_devices___call_service")

        session = MagicMock()
        session.current_query = "打开会客厅的灯"
        result = await tool.handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.hall"}, session
        )
        assert result["success"] is False
        assert "switch.a_on_p2" not in result["error"]
        # 右键未被禁止，仍是候选
        assert "switch.a_on_p3（A灯 会客厅灯 右键）" in result["error"]
