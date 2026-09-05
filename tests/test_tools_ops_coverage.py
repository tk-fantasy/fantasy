"""Coverage-gap tests for tools / ops / ptz / discovery / vision-log / migrations etc.

Every test asserts real behavior (dispatch results, file creation/restore, probe
outcomes, command building). Boundaries mocked: network/RTSP/HTTP, subprocess,
ONVIF, LLM calls, Database (tmp_path or mock).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================================
# app/tools.py
# ============================================================================

class TestToolError:
    def test_extra_fields_included(self):
        from app.tools import tool_error
        err = tool_error("boom", hint="do x", candidates=["a"],
                         blocked_entities=["light.x"])
        assert err == {"error": "boom", "hint": "do x", "candidates": ["a"],
                       "blocked_entities": ["light.x"]}


class TestVerifyReadback:
    @pytest.mark.asyncio  # noqa: plain function is fine too
    async def _none(self):
        pass

    def test_no_new_state_is_none(self):
        from app.tools import _verify_readback
        assert _verify_readback("turn_on", {}, None) == (None, "")

    def test_turn_on_ok_and_fail(self):
        from app.tools import _verify_readback
        assert _verify_readback("turn_on", {}, {"state": "on"}) == (True, "")
        verified, detail = _verify_readback("turn_on", {}, {"state": "off"})
        assert verified is False and "期望开启" in detail

    def test_turn_off_fail_on_on_state(self):
        from app.tools import _verify_readback
        verified, detail = _verify_readback("turn_off", {}, {"state": "on"})
        assert verified is False and "期望关闭" in detail
        assert _verify_readback("turn_off", {}, {"state": "off"}) == (True, "")

    def test_unreliable_state_counts_as_failure(self):
        from app.tools import _verify_readback
        verified, _ = _verify_readback("turn_on", {}, {"state": "unavailable"})
        assert verified is False

    def test_data_key_matches_attribute(self):
        from app.tools import _verify_readback
        st = {"state": "on", "attributes": {"temperature": 25}}
        assert _verify_readback("set_temperature", {"temperature": 25}, st) == (True, "")
        verified, detail = _verify_readback(
            "set_temperature", {"temperature": 25}, {"state": "on", "attributes": {"temperature": 19}})
        assert verified is False and "temperature" in detail

    def test_data_key_string_compare_mismatch(self):
        from app.tools import _verify_readback
        st = {"state": "cool", "attributes": {"mode": "heat"}}
        verified, detail = _verify_readback("set_mode", {"mode": "cool"}, st)
        assert verified is False and "mode" in detail

    def test_data_key_missing_and_current_prefix_missing_skips(self):
        from app.tools import _verify_readback
        # 非开关类服务 + data 键在回读 attributes（含 current_ 前缀）中都不存在
        # → 跳过不下结论
        st = {"state": "heat", "attributes": {"friendly_name": "x"}}
        assert _verify_readback("set_temperature", {"brightness_pct": 50}, st) == (None, "")

    def test_current_prefix_fallback_matches(self):
        from app.tools import _verify_readback
        st = {"state": "20", "attributes": {"current_temperature": 25}}
        assert _verify_readback("set_temperature", {"temperature": 25}, st) == (True, "")


def _make_mgr() -> "object":
    from app.mcp.mcp_client_manager import MCPClientManager
    return MCPClientManager()


def _deps(mgr, states=None, devices=None, ha_client=None):
    from app.tools import ToolDeps
    ha_client = ha_client or MagicMock()
    if not isinstance(getattr(ha_client, "get_states", None), AsyncMock):
        # 测试预配置的 get_states（如 side_effect）保持原样，不被覆盖
        ha_client.get_states = AsyncMock(return_value=states or [])
    ha_service = MagicMock()
    ha_service.get_all_devices = AsyncMock(return_value=devices if devices is not None else [])
    ha_service.get_service_defs = AsyncMock(return_value={})
    return ToolDeps(
        mcp_client_manager=mgr, vision_client=MagicMock(),
        ha_service=ha_service, ha_client_ref=[ha_client],
    )


class TestRegisterAllTools:
    def test_registers_every_builtin_tool(self):
        from app.tools import register_all_tools
        mgr = _make_mgr()
        deps = _deps(mgr)
        deps.camera_manager = None
        register_all_tools(deps)
        names = {"local___vision_chat", "ha_devices___get_entities",
                 "ha_devices___get_device_manual", "ha_devices___call_service",
                 "local___verify_condition", "local___verify_action",
                 "local___scheduled_task_create", "local___scheduled_task_list",
                 "local___scheduled_task_delete", "local___scene_list",
                 "local___scene_apply", "local___scene_create"}
        for n in names:
            assert mgr.get_tool(n) is not None, n


class TestVisionChatHandler:
    @staticmethod
    def _tool(mgr, camera_manager):
        from app.tools import _register_vision_chat
        deps = _deps(mgr)
        deps.camera_manager = camera_manager
        _register_vision_chat(deps)
        return mgr.get_tool("local___vision_chat")

    @pytest.mark.asyncio
    async def test_no_camera_manager(self):
        mgr = _make_mgr()
        tool = self._tool(mgr, None)
        ret = await tool.handler({"question": "看到了啥"}, MagicMock())
        assert ret["has_frame"] is False and "摄像头未配置" in ret["answer"]

    @pytest.mark.asyncio
    async def test_no_camera_available(self):
        mgr = _make_mgr()
        cm = MagicMock()
        cm._active_display_id = ""
        cm.list_cameras.return_value = []
        tool = self._tool(mgr, cm)
        ret = await tool.handler({}, MagicMock())
        assert ret["has_frame"] is False and "没有画面" in ret["answer"]

    @pytest.mark.asyncio
    async def test_empty_buffer_falls_back_to_latest_frame(self):
        mgr = _make_mgr()
        cm = MagicMock()
        cm._active_display_id = "cam1"
        cm.get_state.return_value = {"camera_opened": True}
        cm.get_recent_frames.return_value = []
        frame = object()
        cm.get_frame.return_value = frame
        deps = _deps(mgr)
        deps.camera_manager = cm
        deps.vision_client.ask_about_frames = AsyncMock(return_value="有人在沙发上")
        deps.vision_client.model = "test-vision"
        from app.tools import _register_vision_chat
        _register_vision_chat(deps)
        tool = mgr.get_tool("local___vision_chat")
        ret = await tool.handler({"question": "谁在客厅"}, MagicMock())
        assert ret["has_frame"] is True and ret["answer"] == "有人在沙发上"
        assert ret["frames_used"] == 1 and ret["camera_id"] == "cam1"

    @pytest.mark.asyncio
    async def test_no_frames_at_all(self):
        mgr = _make_mgr()
        cm = MagicMock()
        cm._active_display_id = "cam1"
        cm.get_state.return_value = {"camera_opened": True}
        cm.get_recent_frames.return_value = []
        cm.get_frame.return_value = None
        tool = self._tool(mgr, cm)
        ret = await tool.handler({}, MagicMock())
        assert ret["has_frame"] is False and "没有画面" in ret["answer"]


class TestGetEntitiesHandler:
    @pytest.mark.asyncio
    async def test_snapshot_failure_returns_structured_error(self, monkeypatch):
        import app.services.device_registry as dr
        mgr = _make_mgr()
        from app.tools import _register_ha_get_entities
        deps = _deps(mgr)
        _register_ha_get_entities(deps)
        tool = mgr.get_tool("ha_devices___get_entities")
        monkeypatch.setattr(dr, "build_device_snapshot",
                            AsyncMock(side_effect=RuntimeError("ha down")))
        ret = await tool.handler({}, MagicMock())
        assert ret["error"] and ret["entities"] == [] and ret["devices"] == []
        assert ret["count"] == 0 and "稍后重试" in ret["hint"]


class TestGetDeviceManualHandler:
    @staticmethod
    def _tool(mgr, states_devices):
        from app.tools import _register_ha_get_device_manual
        deps = _deps(mgr)
        deps.ha_service.get_all_devices = AsyncMock(return_value=states_devices)
        _register_ha_get_device_manual(deps)
        return mgr.get_tool("ha_devices___get_device_manual")

    @pytest.mark.asyncio
    async def test_empty_entity_ids_rejected(self):
        mgr = _make_mgr()
        tool = self._tool(mgr, [])
        ret = await tool.handler({"entity_ids": "  "}, MagicMock())
        assert ret["error"] and "entity_ids 不能为空" in ret["error"]
        assert ret["manuals"] == ""

    @pytest.mark.asyncio
    async def test_notes_read_failure_still_returns_manual(self, monkeypatch):
        from app.core.database import Database
        mgr = _make_mgr()
        dev = {"entity_id": "light.bed", "domain": "light", "state": "on",
               "attributes": {"friendly_name": "床头灯"}}
        tool = self._tool(mgr, [dev])
        monkeypatch.setattr(Database, "get",
                            MagicMock(side_effect=RuntimeError("no db")))
        monkeypatch.setattr("app.services.semantic_map.flip_state_value",
                            AsyncMock(side_effect=RuntimeError("flip boom")))
        ret = await tool.handler({"entity_ids": "light.bed"}, MagicMock())
        assert ret["found"] == ["light.bed"] and ret["missing"] == []
        assert "床头灯" in ret["manuals"]

    @pytest.mark.asyncio
    async def test_general_failure_returns_error(self, monkeypatch):
        mgr = _make_mgr()
        tool = self._tool(mgr, [])
        tool  # handler raises via deps.ha_service
        mgr2 = _make_mgr()
        from app.tools import _register_ha_get_device_manual
        deps = _deps(mgr2)
        deps.ha_service.get_all_devices = AsyncMock(side_effect=RuntimeError("ha down"))
        _register_ha_get_device_manual(deps)
        ret = mgr2.get_tool("ha_devices___get_device_manual").handler(
            {"entity_ids": "light.bed"}, MagicMock())
        ret = await ret
        assert "ha down" in ret["error"] and ret["found"] == []


def _call_tool(mgr, parameters, session, states, devices=None, ha_client=None):
    """注册仅 call_service 并调用 handler。"""
    from app.tools import _register_ha_call_service
    deps = _deps(mgr, states=states, devices=devices, ha_client=ha_client)
    _register_ha_call_service(deps)
    return mgr.get_tool("ha_devices___call_service"), deps


@pytest.fixture()
def _fast_readback(monkeypatch):
    monkeypatch.setattr("app.tools._CALL_SERVICE_READBACK_DELAY", 0)


@pytest.fixture()
def _stub_db(monkeypatch):
    """Database.get → mock（prefs_get_by_scope 空 = 无黑名单）。"""
    from app.core.database import Database
    db = MagicMock()
    db.prefs_get_by_scope = AsyncMock(return_value={})
    monkeypatch.setattr(Database, "get", staticmethod(lambda: db))
    return db


@pytest.fixture()
def _stub_semantic(monkeypatch):
    monkeypatch.setattr("app.services.semantic_map.get_action_map",
                        AsyncMock(return_value={"mappings": {}}))


class TestCallServiceHandler:
    @pytest.mark.asyncio
    async def test_data_json_string_and_domain_derived(self, _fast_readback, _stub_db,
                                                       _stub_semantic, monkeypatch):
        """data 传 JSON 字符串要解析；domain 缺省从 entity_id 前缀推导。"""
        import app.services.device_event_service as des
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on",
                   "attributes": {"brightness_pct": 40, "friendly_name": "床头灯"}}]
        rec = AsyncMock()
        monkeypatch.setattr(des, "record_device_op", rec)
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, _ = _call_tool(mgr, {"service": "turn_on", "entity_id": "light.bed",
                                       "data": '{"brightness_pct": 40}'},
                                 MagicMock(), states)
            ret = await tool.handler({"service": "turn_on", "entity_id": "light.bed",
                                      "data": '{"brightness_pct": 40}'}, MagicMock())
        assert ret["success"] is True and ret["verified"] is True
        assert ret["new_state"]["attributes"]["friendly_name"] == "床头灯"
        rec.assert_awaited_once()
        args = rec.await_args
        assert args.args[0] == ["light.bed"] and "light.bed" in args.args[3]

    @pytest.mark.asyncio
    async def test_data_invalid_json_becomes_empty(self, _fast_readback, _stub_db,
                                                   _stub_semantic):
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on", "attributes": {}}]
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, _ = _call_tool(mgr, {}, MagicMock(), states)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed", "data": "{not json"},
                                     MagicMock())
        assert ret["success"] is True  # 坏 JSON 只当空 data，不失败

    @pytest.mark.asyncio
    async def test_entity_id_without_dot_gets_domain_prefix(self, _fast_readback,
                                                            _stub_db, _stub_semantic):
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on", "attributes": {}}]
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, _ = _call_tool(mgr, {}, MagicMock(), states)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "bed"}, MagicMock())
        assert ret["success"] is True

    @pytest.mark.asyncio
    async def test_semantic_check_failure_passes_through(self, _fast_readback, _stub_db,
                                                         _stub_semantic):
        """语义校验环节异常（如设备列表拉取失败）→ 放行不阻断控制。"""
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on", "attributes": {}}]
        session = MagicMock()
        session.current_query = "开灯"
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, deps = _call_tool(mgr, {}, MagicMock(), states)
            deps.ha_service.get_all_devices = AsyncMock(
                side_effect=RuntimeError("device list boom"))
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed"}, session)
        assert ret["success"] is True

    @pytest.mark.asyncio
    async def test_missing_entity_candidates_lookup_failure(self, _fast_readback,
                                                            _stub_db, monkeypatch):
        """编造的 entity_id 被拒；候选反查失败时只留基础报错，不崩。"""
        import app.services.device_registry as dr
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on", "attributes": {}}]
        session = MagicMock()
        session.current_query = "打开床头灯"
        monkeypatch.setattr(dr, "build_device_snapshot",
                            AsyncMock(side_effect=RuntimeError("registry boom")))
        with patch("app.tools.call_with_probe", new=AsyncMock()) as probe:
            tool, _ = _call_tool(mgr, {}, MagicMock(), states)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.fake"}, session)
        assert ret["success"] is False and "不存在" in ret["error"]
        assert "candidates" not in ret
        probe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_states_check_failure_allows_call(self, _fast_readback, _stub_db,
                                                    _stub_semantic):
        """get_states 抛异常 → 校验放行，指令仍下发。"""
        mgr = _make_mgr()
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(side_effect=RuntimeError("conn reset"))
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={"ok": 1})):
            tool, _ = _call_tool(mgr, {}, MagicMock(), [], ha_client=ha_client)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed"}, MagicMock())
        assert ret["success"] is True and ret["result"] == {"ok": 1}

    @pytest.mark.asyncio
    async def test_auth_check_failure_passes_through(self, _fast_readback,
                                                     _stub_semantic, monkeypatch):
        """授权黑名单读取失败 → 放行（避免锁死全屋）。"""
        from app.core.database import Database
        monkeypatch.setattr(Database, "get",
                            MagicMock(side_effect=RuntimeError("db gone")))
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on", "attributes": {}}]
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, _ = _call_tool(mgr, {}, MagicMock(), states)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed"}, MagicMock())
        assert ret["success"] is True

    @pytest.mark.asyncio
    async def test_semantic_mismatch_rejected_with_candidates(self, _fast_readback,
                                                              _stub_db, _stub_semantic):
        """query 命中加湿器但目标实体不是它 → 拒绝并给候选。"""
        mgr = _make_mgr()
        states = [{"entity_id": "switch.other", "state": "off", "attributes": {}},
                  {"entity_id": "humidifier.x", "state": "off", "attributes": {}}]
        devices = [{"entity_id": "humidifier.x", "domain": "humidifier",
                    "name": "加湿器", "attributes": {}}]
        session = MagicMock()
        session.current_query = "打开加湿器"
        with patch("app.tools.call_with_probe", new=AsyncMock()) as probe:
            tool, _ = _call_tool(mgr, {}, MagicMock(), states, devices=devices)
            ret = await tool.handler({"domain": "switch", "service": "turn_on",
                                      "entity_id": "switch.other"}, session)
        assert ret["success"] is False and "不符" in ret["error"]
        assert ret["candidates"] == ["加湿器"]
        probe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_readback_failure_marks_state_unknown(self, _fast_readback, _stub_db,
                                                        _stub_semantic, monkeypatch):
        """指令已发但回读失败 → state_check=failed + 如实告知的 note。"""
        import app.services.device_event_service as des
        monkeypatch.setattr(des, "record_device_op", AsyncMock())
        mgr = _make_mgr()
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(
            side_effect=[{"entity_id": "light.bed", "state": "on", "attributes": {}},
                         RuntimeError("readback boom")])
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, _ = _call_tool(mgr, {}, MagicMock(), [], ha_client=ha_client)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed"}, MagicMock())
        assert ret["success"] is True
        assert ret["state_check"] == "failed" and "未经核实" in ret["note"]

    @pytest.mark.asyncio
    async def test_readback_mismatch_notes_not_verified(self, _fast_readback, _stub_db,
                                                        _stub_semantic, monkeypatch):
        import app.services.device_event_service as des
        monkeypatch.setattr(des, "record_device_op", AsyncMock())
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "off", "attributes": {}}]
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, _ = _call_tool(mgr, {}, MagicMock(), states)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed"}, MagicMock())
        assert ret["success"] is True and ret["verified"] is False
        assert "不符" in ret["note"]

    @pytest.mark.asyncio
    async def test_device_op_recording_failure_silent(self, _fast_readback, _stub_db,
                                                      _stub_semantic, monkeypatch):
        import app.services.device_event_service as des
        monkeypatch.setattr(des, "record_device_op",
                            AsyncMock(side_effect=RuntimeError("log boom")))
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on", "attributes": {}}]
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, _ = _call_tool(mgr, {}, MagicMock(), states)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed"}, MagicMock())
        assert ret["success"] is True

    @pytest.mark.asyncio
    async def test_state_flip_failure_keeps_original_state(self, _fast_readback,
                                                           _stub_db, _stub_semantic,
                                                           monkeypatch):
        import app.services.device_event_service as des
        monkeypatch.setattr(des, "record_device_op", AsyncMock())
        monkeypatch.setattr("app.services.semantic_map.apply_state_flip",
                            MagicMock(side_effect=RuntimeError("flip boom")))
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on", "attributes": {}}]
        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
            tool, _ = _call_tool(mgr, {}, MagicMock(), states)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed"}, MagicMock())
        assert ret["success"] is True and ret["new_state"]["state"] == "on"

    @pytest.mark.asyncio
    async def test_call_with_probe_exception_returns_error(self, _stub_db, _stub_semantic):
        mgr = _make_mgr()
        states = [{"entity_id": "light.bed", "state": "on", "attributes": {}}]
        with patch("app.tools.call_with_probe",
                   new=AsyncMock(side_effect=RuntimeError("ha 500"))):
            tool, _ = _call_tool(mgr, {}, MagicMock(), states)
            ret = await tool.handler({"domain": "light", "service": "turn_on",
                                      "entity_id": "light.bed"}, MagicMock())
        assert ret["success"] is False and "ha 500" in ret["error"]


class TestScheduledTaskTools:
    @staticmethod
    def _svc():
        svc = SimpleNamespace()
        svc.add_task = AsyncMock(
            return_value={"id": "t1", "schedule": {"kind": "cron", "expr": "0 8 * * *"}})
        svc.list_tasks = AsyncMock(return_value=[{"id": "t1"}, {"id": "t2"}])
        svc.delete_task = AsyncMock(return_value=True)
        return svc

    @staticmethod
    def _register(mgr, svc):
        from app.tools import ToolDeps, _register_scheduled_task_tools
        deps = ToolDeps(mcp_client_manager=mgr, vision_client=MagicMock(),
                        ha_service=MagicMock(), ha_client_ref=[MagicMock()],
                        scheduler_service_ref=[svc])
        _register_scheduled_task_tools(deps)
        return mgr

    @pytest.mark.asyncio
    async def test_create_full_flow(self):
        mgr = self._register(_make_mgr(), self._svc())
        ret = await mgr.get_tool("local___scheduled_task_create").handler({
            "name": "起床开灯",
            "schedule": {"kind": "cron", "expr": "0 8 * * *"},
            "payload": {"kind": "tool", "tool_name": "x", "tool_input": {}},
        }, SimpleNamespace(user_id="u1"))
        assert ret["success"] is True and ret["task_id"] == "t1"
        assert ret["name"] == "起床开灯" and ret["summary"]

    @pytest.mark.asyncio
    async def test_create_validation_errors(self):
        mgr = self._register(_make_mgr(), self._svc())
        tool = mgr.get_tool("local___scheduled_task_create")
        ret = await tool.handler({"name": " ", "schedule": {}, "payload": {}}, None)
        assert "name 不能为空" in ret["error"]
        ret = await tool.handler({"name": "x", "schedule": {}, "payload": {}}, None)
        assert "必填" in ret["error"]

    @pytest.mark.asyncio
    async def test_create_scheduler_not_ready(self):
        mgr = self._register(_make_mgr(), None)
        ret = await mgr.get_tool("local___scheduled_task_create").handler(
            {"name": "x", "schedule": {"kind": "at"}, "payload": {}}, None)
        assert "调度器未就绪" in ret["error"]

    @pytest.mark.asyncio
    async def test_list_and_delete(self):
        svc = self._svc()
        mgr = self._register(_make_mgr(), svc)
        ret = await mgr.get_tool("local___scheduled_task_list").handler({}, None)
        assert ret == {"tasks": [{"id": "t1"}, {"id": "t2"}], "count": 2}
        ret = await mgr.get_tool("local___scheduled_task_delete").handler(
            {"task_id": "t1"}, None)
        assert ret == {"success": True, "task_id": "t1"}
        svc.delete_task.assert_awaited_with("t1")
        ret = await mgr.get_tool("local___scheduled_task_delete").handler(
            {"task_id": ""}, None)
        assert "task_id 不能为空" in ret["error"]
        mgr2 = self._register(_make_mgr(), None)
        for name in ("local___scheduled_task_list", "local___scheduled_task_delete"):
            ret = await mgr2.get_tool(name).handler({"task_id": "t"} if "delete" in name else {},
                                                    None)
            assert "调度器未就绪" in ret["error"]


class TestConnectExternalMcpServers:
    @pytest.mark.asyncio
    async def test_no_config_skips(self, monkeypatch):
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "get_config", lambda key, default=None: default)
        from app.tools import connect_external_mcp_servers
        mgr = _make_mgr()
        mgr.connect_external_server = AsyncMock()
        await connect_external_mcp_servers(mgr)
        mgr.connect_external_server.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connects_and_isolates_failures(self, monkeypatch):
        import app.core.config as cfg
        entries = [{"name": "good", "cmd": "good-cmd", "args": ["a"]},
                   {"name": "bad", "cmd": "bad-cmd", "args": []},
                   {"name": "", "cmd": "no-name", "args": []}]
        monkeypatch.setattr(cfg, "get_config", lambda key, default=None:
                            entries if key == "external_mcp" else default)
        from app.tools import connect_external_mcp_servers
        mgr = _make_mgr()

        async def connect(name, cmd, args):
            if name == "bad":
                raise RuntimeError("spawn failed")
            return ["tool_a", "tool_b"]

        mgr.connect_external_server = AsyncMock(side_effect=connect)
        await connect_external_mcp_servers(mgr)
        assert mgr.connect_external_server.await_count == 2  # 无 name 的条目被跳过


def _scene_container(svc):
    import app.container as container_mod
    return SimpleNamespace(get_container=lambda: SimpleNamespace(scene_service=svc))


class TestSceneTools:
    @staticmethod
    def _svc():
        svc = SimpleNamespace()
        svc.list_scenes = AsyncMock(return_value=[
            {"id": "s1", "name": "观影", "actions": [{"a": 1}, {"a": 2}]},
            {"id": "s2", "name": "睡眠", "actions": []},
        ])
        svc.apply_scene = AsyncMock(return_value={
            "ok": 2, "total": 2, "scene": "观影",
            "results": [{"entity_id": "l1", "ok": True}, {"entity_id": "l2", "ok": True}]})
        svc.create_scene = AsyncMock(return_value={"id": "s9", "actions": [{"a": 1}]})
        svc.capture_scene = AsyncMock(return_value={"id": "s8", "actions": [{"a": 1}, {"a": 2}]})
        return svc

    @staticmethod
    def _patch(mono, svc):
        import app.container as container_mod
        mono.setattr(container_mod, "get_container",
                     lambda: SimpleNamespace(scene_service=svc))

    @pytest.mark.asyncio
    async def test_list_scenes(self, monkeypatch):
        svc = self._svc()
        self._patch(monkeypatch, svc)
        mgr = _make_mgr()
        from app.tools import _register_scene_tools
        _register_scene_tools(_deps(mgr))
        ret = await mgr.get_tool("local___scene_list").handler({}, None)
        assert ret["scenes"] == [{"id": "s1", "name": "观影", "actions_count": 2},
                                 {"id": "s2", "name": "睡眠", "actions_count": 0}]

    @pytest.mark.asyncio
    async def test_apply_by_name_success_and_partial(self, monkeypatch):
        svc = self._svc()
        self._patch(monkeypatch, svc)
        mgr = _make_mgr()
        from app.tools import _register_scene_tools
        _register_scene_tools(_deps(mgr))
        tool = mgr.get_tool("local___scene_apply")
        ret = await tool.handler({"name": "观影"}, None)
        svc.apply_scene.assert_awaited_with("s1")
        assert ret["success"] is True and "2/2" in ret["summary"]
        svc.apply_scene.return_value = {
            "ok": 1, "total": 2, "scene": "观影",
            "results": [{"entity_id": "l1", "ok": True}, {"entity_id": "l2", "ok": False}]}
        ret = await tool.handler({"name": "观影"}, None)
        assert ret["success"] is True and "l2" in ret["summary"]
        svc.apply_scene.return_value = {
            "ok": 0, "total": 2, "scene": "观影",
            "results": [{"entity_id": "l1", "ok": False}]}
        ret = await tool.handler({"name": "观影"}, None)
        assert ret["success"] is False

    @pytest.mark.asyncio
    async def test_apply_unknown_name_lists_candidates(self, monkeypatch):
        svc = self._svc()
        self._patch(monkeypatch, svc)
        mgr = _make_mgr()
        from app.tools import _register_scene_tools
        _register_scene_tools(_deps(mgr))
        ret = await mgr.get_tool("local___scene_apply").handler({"name": "不存在"}, None)
        assert "没有叫" in ret["error"]
        assert ret["candidates"] == ["观影", "睡眠"]

    @pytest.mark.asyncio
    async def test_apply_requires_identifier_and_valueerror(self, monkeypatch):
        svc = self._svc()
        self._patch(monkeypatch, svc)
        mgr = _make_mgr()
        from app.tools import _register_scene_tools
        _register_scene_tools(_deps(mgr))
        tool = mgr.get_tool("local___scene_apply")
        ret = await tool.handler({}, None)
        assert "必填一个" in ret["error"]
        svc.apply_scene = AsyncMock(side_effect=ValueError("场景不存在"))
        ret = await tool.handler({"scene_id": "s1"}, None)
        assert "场景不存在" in ret["error"]

    @pytest.mark.asyncio
    async def test_create_with_actions_and_capture(self, monkeypatch):
        svc = self._svc()
        self._patch(monkeypatch, svc)
        mgr = _make_mgr()
        from app.tools import _register_scene_tools
        _register_scene_tools(_deps(mgr))
        tool = mgr.get_tool("local___scene_create")
        ret = await tool.handler({"name": "观影", "actions": [{"domain": "light"}]},
                                 SimpleNamespace(user_id="u1"))
        svc.create_scene.assert_awaited_with("观影", [{"domain": "light"}], user_id="u1")
        assert ret == {"success": True, "scene_id": "s9", "name": "观影", "actions_count": 1}
        ret = await tool.handler({"name": "抓拍", "capture": True},
                                 SimpleNamespace(user_id="u1"))
        svc.capture_scene.assert_awaited_with("抓拍", user_id="u1")
        assert ret["scene_id"] == "s8" and ret["actions_count"] == 2

    @pytest.mark.asyncio
    async def test_create_validation_errors(self, monkeypatch):
        svc = self._svc()
        self._patch(monkeypatch, svc)
        mgr = _make_mgr()
        from app.tools import _register_scene_tools
        _register_scene_tools(_deps(mgr))
        tool = mgr.get_tool("local___scene_create")
        ret = await tool.handler({"name": ""}, None)
        assert "name 不能为空" in ret["error"]
        svc.create_scene = AsyncMock(side_effect=ValueError("actions 不能为空"))
        ret = await tool.handler({"name": "x", "actions": []}, None)
        assert "actions 不能为空" in ret["error"]

    @pytest.mark.asyncio
    async def test_scene_service_not_ready(self, monkeypatch):
        import app.container as container_mod
        monkeypatch.setattr(container_mod, "get_container", lambda: object())
        mgr = _make_mgr()
        from app.tools import _register_scene_tools
        _register_scene_tools(_deps(mgr))
        for name, params in (("local___scene_list", {}), ("local___scene_apply", {}),
                             ("local___scene_create", {"name": "x"})):
            ret = await mgr.get_tool(name).handler(params, None)
            assert "场景服务未就绪" in ret["error"]


# ============================================================================
# app/ops/backup.py
# ============================================================================

@pytest.fixture
def bak_env(tmp_path, monkeypatch):
    from app.ops import audit
    from app.ops import backup as bk
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(bk, "DATA_DIR", data_dir)
    monkeypatch.setattr(bk, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(bk, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit" / "ops_audit.jsonl")
    (tmp_path / "config.json").write_text('{"ha": {"url": "http://x"}}', encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=sk-test\n", encoding="utf-8")
    return tmp_path


def _seed_db(data_dir: Path):
    conn = sqlite3.connect(data_dir / "aether.db")
    conn.execute("CREATE TABLE t(v TEXT)")
    conn.execute("INSERT INTO t VALUES('kept')")
    conn.commit()
    conn.close()


class TestBackupGaps:
    def test_create_backup_skips_wal_shm_entries(self, bak_env):
        """data/aether.db-wal / -shm 不入包（已并入一致性快照）。"""
        from app.ops import backup as bk
        _seed_db(bk.DATA_DIR)
        (bk.DATA_DIR / "aether.db-wal").write_bytes(b"wal-bytes")
        (bk.DATA_DIR / "aether.db-shm").write_bytes(b"shm-bytes")
        (bk.DATA_DIR / "jwt_secret").write_text("s3cret", encoding="utf-8")
        created = bk.create_backup("tester")
        assert created["name"].startswith("aether-backup-")
        assert created["name"].endswith(".tar.gz")
        assert created["size_bytes"] > 0
        with tarfile.open(bk.BACKUP_DIR / created["name"], "r:gz") as tf:
            names = tf.getnames()
        assert "data/aether.db-wal" not in names
        assert "data/aether.db-shm" not in names
        assert "data/aether.db" in names
        assert "config.json" in names and ".env" in names

    def test_list_backups_missing_dir_returns_empty(self, tmp_path, monkeypatch):
        from app.ops import backup as bk
        monkeypatch.setattr(bk, "BACKUP_DIR", tmp_path / "no-such-dir")
        assert bk.list_backups() == []

    def test_validate_backup_missing_file_raises(self, bak_env):
        from app.ops import backup as bk
        bk.BACKUP_DIR.mkdir()
        with pytest.raises(FileNotFoundError):
            bk.validate_backup("aether-backup-20990101-000000.tar.gz")

    def test_restore_exit_soon_calls_os_exit(self, bak_env, monkeypatch):
        """_exit_soon 定时器回调以 os._exit(0) 结束进程（容器 restart 拉起）。"""
        from app.ops import backup as bk
        _seed_db(bk.DATA_DIR)
        created = bk.create_backup("tester")
        fired = {}

        class FakeTimer:
            daemon = True

            def __init__(self, interval, fn):
                fired["interval"] = interval
                fired["fn"] = fn

            def start(self):
                fired["started"] = True

        monkeypatch.setattr(threading, "Timer", FakeTimer)
        monkeypatch.setattr(os, "_exit", lambda code: fired.update(exit_code=code))
        info = bk.restore_backup(created["name"], "operator")
        assert info["restored"] is True and fired["started"] is True
        fired["fn"]()
        assert fired["exit_code"] == 0


# ============================================================================
# app/ops/audit.py
# ============================================================================

@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    from app.ops import audit
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit" / "ops_audit.jsonl")
    return audit


class TestAuditGaps:
    def test_record_success_returns_entry_and_persists(self, audit_env):
        entry = audit_env.record("tester", "backup_create", {"name": "x.tar.gz"})
        assert entry["operator"] == "tester" and entry["action"] == "backup_create"
        assert entry["detail"] == {"name": "x.tar.gz"} and entry["ts"]
        lines = audit_env.AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0])["action"] == "backup_create"

    def test_record_write_failure_swallows_oserror(self, audit_env, tmp_path):
        """审计写失败不抛（audit dir 位置被文件占用）。"""
        blocking = tmp_path / "audit_blocked"
        blocking.write_text("i am a file", encoding="utf-8")
        audit_env.AUDIT_DIR  # noqa: B018
        audit_env.AUDIT_DIR = blocking  # plain attr already patched by fixture
        entry = audit_env.record("tester", "op", None)
        assert entry["action"] == "op" and entry["detail"] == {}

    def test_clear_missing_file_returns_zero(self, audit_env):
        assert audit_env.clear() == 0

    def test_clear_read_failure_returns_zero(self, audit_env, tmp_path):
        audit_env.AUDIT_DIR.mkdir()
        audit_env.AUDIT_FILE = audit_env.AUDIT_DIR  # 目录 → read_text 抛 OSError
        assert audit_env.clear() == 0

    def test_tail_missing_file_returns_empty(self, audit_env):
        assert audit_env.tail() == []

    def test_tail_read_failure_returns_empty(self, audit_env):
        audit_env.AUDIT_DIR.mkdir()
        audit_env.AUDIT_FILE = audit_env.AUDIT_DIR  # 目录 → read_text 抛 OSError
        assert audit_env.tail() == []

    def test_tail_skips_broken_lines(self, audit_env):
        audit_env.record("a", "op1", {})
        audit_env.AUDIT_FILE.open("a", encoding="utf-8").write("not-json\n")
        audit_env.record("b", "op2", {})
        result = audit_env.tail()
        assert [r["operator"] for r in result] == ["a", "b"]

    def test_tail_limit_and_order(self, audit_env):
        for i in range(5):
            audit_env.record(f"op{i}", "act", {})
        result = audit_env.tail(limit=2)
        assert [r["operator"] for r in result] == ["op3", "op4"]


# ============================================================================
# app/ops/auto_update.py
# ============================================================================

def _make_update_pack(path: Path, version: str | None):
    import io
    with tarfile.open(path, "w:gz") as tf:
        if version is not None:
            data = json.dumps({"version": version}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _age_file(p: Path, age: int = 120):
    old = time.time() - age
    os.utime(p, (old, old))


class TestAutoUpdateGaps:
    def test_find_candidate_missing_pack_dir(self, tmp_path, monkeypatch):
        from app.ops import auto_update as au
        from app.ops import pack_export as pe
        monkeypatch.setattr(pe, "PACK_DIR", tmp_path / "nope")
        assert au.find_candidate() is None

    def test_find_candidate_skips_pack_without_version(self, tmp_path, monkeypatch):
        """拷了一半/坏包（无 manifest 版本）静默跳过。"""
        from app.ops import auto_update as au
        from app.ops import pack_export as pe
        monkeypatch.setattr(pe, "PACK_DIR", tmp_path)
        monkeypatch.setattr(au, "SETTLE_SECONDS", 0)
        monkeypatch.setattr(au, "get_version", lambda: "1.0.0")
        p = tmp_path / "aether-update-9.9.9.tar.gz"
        _make_update_pack(p, None)
        _age_file(p)
        assert au.find_candidate() is None

    @staticmethod
    def _stop_loop(monkeypatch):
        """让 watcher_loop 跑完一轮后经 sleep 抛 CancelledError 退出。"""
        async def _cancel(_seconds):
            raise asyncio.CancelledError
        monkeypatch.setattr(asyncio, "sleep", _cancel)

    @pytest.mark.asyncio
    async def test_watcher_disabled_never_scans(self, monkeypatch):
        from app.ops import auto_update as au
        self._stop_loop(monkeypatch)
        monkeypatch.setattr(au, "auto_upgrade_enabled", lambda: False)
        scan = MagicMock(return_value=None)
        monkeypatch.setattr(au, "find_candidate", scan)
        with pytest.raises(asyncio.CancelledError):
            await au.watcher_loop()
        scan.assert_not_called()

    @pytest.mark.asyncio
    async def test_watcher_applies_newer_pack(self, tmp_path, monkeypatch):
        from app.ops import auto_update as au
        self._stop_loop(monkeypatch)
        monkeypatch.setattr(au, "auto_upgrade_enabled", lambda: True)
        pack = tmp_path / "aether-update-9.9.9.tar.gz"
        pack.write_bytes(b"pack")
        monkeypatch.setattr(au, "find_candidate",
                            lambda: (pack, "9.9.9"))
        apply_mock = AsyncMock(return_value={"from_version": "1.0.0",
                                             "to_version": "9.9.9"})
        monkeypatch.setattr(au.pack_export, "apply_local_pack", apply_mock)
        monkeypatch.setattr(au, "_lock", asyncio.Lock())
        with pytest.raises(asyncio.CancelledError):
            await au.watcher_loop()
        apply_mock.assert_awaited_once_with(pack.name, "auto")

    @pytest.mark.asyncio
    async def test_watcher_quarantines_failed_pack(self, tmp_path, monkeypatch):
        """apply 失败 → 包改名 .failed 防止每轮重试。"""
        from app.ops import auto_update as au
        self._stop_loop(monkeypatch)
        monkeypatch.setattr(au, "auto_upgrade_enabled", lambda: True)
        pack = tmp_path / "aether-update-9.9.9.tar.gz"
        pack.write_bytes(b"pack")
        monkeypatch.setattr(au, "find_candidate", lambda: (pack, "9.9.9"))
        monkeypatch.setattr(au.pack_export, "apply_local_pack",
                            AsyncMock(side_effect=RuntimeError("docker load failed")))
        monkeypatch.setattr(au, "_lock", asyncio.Lock())
        with pytest.raises(asyncio.CancelledError):
            await au.watcher_loop()
        assert not pack.exists()
        assert (tmp_path / "aether-update-9.9.9.tar.gz.failed").exists()

    @pytest.mark.asyncio
    async def test_watcher_rename_failure_isolated(self, tmp_path, monkeypatch):
        """隔离改名也失败（如目标已存在）→ 仅记日志，不崩。"""
        from app.ops import auto_update as au
        self._stop_loop(monkeypatch)
        monkeypatch.setattr(au, "auto_upgrade_enabled", lambda: True)
        pack = tmp_path / "aether-update-9.9.9.tar.gz"
        pack.write_bytes(b"pack")
        (tmp_path / "aether-update-9.9.9.tar.gz.failed").mkdir()  # 目标已存在 → OSError
        monkeypatch.setattr(au, "find_candidate", lambda: (pack, "9.9.9"))
        monkeypatch.setattr(au.pack_export, "apply_local_pack",
                            AsyncMock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(au, "_lock", asyncio.Lock())
        with pytest.raises(asyncio.CancelledError):
            await au.watcher_loop()
        assert pack.exists()  # 原包保留，未崩

    @pytest.mark.asyncio
    async def test_watcher_survives_scan_exception(self, monkeypatch):
        from app.ops import auto_update as au
        self._stop_loop(monkeypatch)
        monkeypatch.setattr(au, "auto_upgrade_enabled", lambda: True)
        monkeypatch.setattr(au, "find_candidate",
                            MagicMock(side_effect=RuntimeError("disk error")))
        with pytest.raises(asyncio.CancelledError):
            await au.watcher_loop()


# ============================================================================
# app/services/ptz_service.py
# ============================================================================

def _onvif_module(cam_factory):
    mod = MagicMock()
    mod.ONVIFCamera = cam_factory
    mod.__file__ = "/fake/onvif/__init__.py"
    return mod


class TestPtzGaps:
    def test_enabled_global_config_path(self, monkeypatch):
        import app.services.ptz_service as ps
        from app.services.ptz_service import PtzService
        monkeypatch.setattr(ps, "get_config",
                            lambda key, default=None: True if key == "ptz.enabled" else default)
        assert PtzService()._enabled() is True
        monkeypatch.setattr(ps, "get_config",
                            lambda key, default=None: False if key == "ptz.enabled" else default)
        assert PtzService()._enabled() is False

    @pytest.mark.asyncio
    async def test_ensure_connected_fast_path_when_healthy(self):
        from app.services.ptz_service import PtzService
        svc = PtzService()
        svc._cam = MagicMock()
        svc._broken = False
        assert await svc._ensure_connected() is True

    @pytest.mark.asyncio
    async def test_ensure_connected_per_camera_config_no_ip(self):
        """per-camera config 路径：读 ptz_* 字段，无 IP 直接失败。"""
        from app.services.ptz_service import PtzService
        svc = PtzService(camera_id="cam1", config={
            "ptz_enabled": 1, "ptz_ip": "", "ptz_port": 80,
            "ptz_username": "u", "ptz_password": "p"})
        assert await svc._ensure_connected() is False

    def test_speed_per_camera_clamped(self):
        from app.services.ptz_service import PtzService
        assert PtzService(config={"ptz_speed": 2.0})._speed() == 1.0
        assert PtzService(config={"ptz_speed": 0.01})._speed() == 0.1

    @pytest.mark.asyncio
    async def test_stop_error_ignored(self):
        """Stop 在已停止时报错属正常：忽略并继续返回成功。"""
        from app.services.ptz_service import PtzService
        svc = PtzService()
        svc._ptz = MagicMock()
        svc._ptz.Stop = AsyncMock(side_effect=RuntimeError("already stopped"))
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=True)):
            ret = await svc.stop()
        assert ret == {"success": True}
        svc._ptz.Stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_move_not_connected(self):
        from app.services.ptz_service import PtzService
        svc = PtzService()
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=False)):
            ret = await svc.move("up")
        assert ret["success"] is False and "not connected" in ret["error"]

    @pytest.mark.asyncio
    async def test_step_not_connected(self):
        from app.services.ptz_service import PtzService
        svc = PtzService()
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=False)):
            ret = await svc.step("left", 100)
        assert ret["success"] is False and "not connected" in ret["error"]

    @pytest.mark.asyncio
    async def test_step_move_failure_marks_broken(self):
        from app.services.ptz_service import PtzService
        svc = PtzService()
        svc._ptz = MagicMock()
        svc._ptz.Stop = AsyncMock()
        svc._ptz.create_type = MagicMock(return_value=MagicMock())
        svc._ptz.ContinuousMove = AsyncMock(side_effect=RuntimeError("move err"))
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=True)):
            ret = await svc.step("up", 100)
        assert ret["success"] is False and "move err" in ret["error"]
        assert svc._broken is True

    @pytest.mark.asyncio
    async def test_registry_creates_and_reuses_services(self):
        from app.services.ptz_service import PtzRegistry
        reg = PtzRegistry()
        cfg = {"ptz_enabled": 1, "ptz_ip": "10.0.0.5"}
        svc_a = await reg.get("cam1", cfg)
        assert isinstance(svc_a, type(await reg.get("cam1", cfg)))
        assert await reg.get("cam1", cfg) is svc_a
        svc_b = await reg.get("cam2", cfg)
        assert svc_a is not svc_b
        assert svc_a.camera_id == "cam1" and svc_a._config == cfg

    @pytest.mark.asyncio
    async def test_registry_notify_ip_changed(self):
        from app.services.ptz_service import PtzRegistry
        reg = PtzRegistry()
        reg.notify_ip_changed("ghost-cam", "10.0.0.9")  # 未知 cam：无实例 → no-op
        cfg = {"ptz_enabled": 1, "ptz_ip": "10.0.0.5"}
        svc = await reg.get("cam1", cfg)
        svc._broken = False
        reg.notify_ip_changed("cam1", "10.0.0.9")
        assert svc._broken is True and svc._cam is None and svc._ptz is None


# ============================================================================
# app/routes/vision_log_routes.py
# ============================================================================

@pytest.fixture()
def admin_client(monkeypatch):
    """真实 app + 管理员 token（与 test_vision_log_routes_api 同模式）。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.auth import create_access_token
    from app.core.database import Database

    db = AsyncMock()
    db.user_get_by_id = AsyncMock(
        return_value={"id": "u-admin", "username": "tester", "is_admin": 1})
    db.vision_logs_tail = AsyncMock(return_value=[{"id": 1, "kind": "preview"}])
    db.vision_logs_delete_camera = AsyncMock(return_value=7)
    token = create_access_token("u-admin", "tester")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(Database, "get", staticmethod(lambda: db))
    return TestClient(app), headers


class TestVisionLogRoutesGaps:
    def test_list_and_clear_endpoints(self, admin_client):
        tc, headers = admin_client
        resp = tc.get("/api/vision-logs", headers=headers,
                      params={"camera_id": "cam1", "kind": "action", "limit": 5})
        assert resp.status_code == 200 and resp.json()["data"][0]["kind"] == "preview"
        resp = tc.delete("/api/vision-logs", headers=headers, params={"camera_id": "cam1"})
        assert resp.status_code == 200 and resp.json()["data"] == {"deleted": 7}

    def test_browse_root_non_windows_branch(self, admin_client, monkeypatch):
        tc, headers = admin_client
        monkeypatch.setattr(os, "name", "posix")
        resp = tc.get("/api/files/browse", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["entries"] == [{"name": "/", "path": "/", "type": "dir"}]

    def test_browse_scan_failure_returns_400(self, admin_client, monkeypatch):
        import app.routes.vision_log_routes as vlr
        tc, headers = admin_client
        monkeypatch.setattr(vlr, "_scan_dir_sync",
                            MagicMock(side_effect=OSError("permission denied")))
        resp = tc.get("/api/files/browse", headers=headers,
                      params={"path": str(Path("/tmp").anchor)})
        assert resp.status_code == 400
        assert "read_failed" in resp.text and "读取目录失败" in resp.text

    def test_scan_dir_entry_limit_breaks(self, tmp_path, monkeypatch):
        import app.routes.vision_log_routes as vlr
        monkeypatch.setattr(vlr, "_DIR_ENTRY_LIMIT", 2)
        for i in range(4):
            (tmp_path / f"f{i}.mp4").write_bytes(b"x")
        dirs, files = vlr._scan_dir_sync(tmp_path, {"mp4"})
        assert dirs == [] and len(files) == 2  # 达到上限即 break

    def test_scan_dir_skips_entries_raising_oserror(self, tmp_path, monkeypatch):
        """无权限/失效符号链接条目：OSError → 跳过继续。"""
        import app.routes.vision_log_routes as vlr
        good = tmp_path / "ok.mp4"
        good.write_bytes(b"video")

        class BadEntry:
            name = "broken"
            path = str(tmp_path / "broken")

            def is_dir(self):
                raise OSError("stale symlink")

            def is_file(self):
                raise OSError("stale symlink")

        real_iterdir = Path.iterdir
        monkeypatch.setattr(Path, "iterdir",
                            lambda self: iter([BadEntry(), good]) if self == tmp_path
                            else real_iterdir(self))
        dirs, files = vlr._scan_dir_sync(tmp_path, {"mp4"})
        assert dirs == [] and len(files) == 1 and files[0]["name"] == "ok.mp4"

    def test_scan_dir_filters_dirs_and_files(self, tmp_path):
        import app.routes.vision_log_routes as vlr
        (tmp_path / "sub").mkdir()
        (tmp_path / "$RECYCLE.BIN").mkdir()
        (tmp_path / "movie.MKV").write_bytes(b"v")
        (tmp_path / "note.txt").write_text("x", encoding="utf-8")
        dirs, files = vlr._scan_dir_sync(tmp_path, {"mkv"})
        assert [d["name"] for d in dirs] == ["sub"]  # $ 开头目录被排除
        assert len(files) == 1 and files[0]["name"] == "movie.MKV"
        assert files[0]["size"] == 1


# ============================================================================
# app/services/camera_discovery_service.py
# ============================================================================

class TestDiscoveryGaps:
    def test_status_property_reflects_state(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        svc._status = "found"
        svc._last_found_ip = "192.168.1.5"
        status = svc.status
        assert status["status"] == "found"
        assert status["last_found_ip"] == "192.168.1.5"
        assert "device_mac" in status

    @pytest.mark.asyncio
    async def test_read_hardware_id_raises_when_onvif_missing(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        with patch.dict("sys.modules", {"onvif": None}):
            with pytest.raises(RuntimeError, match="ONVIF"):
                await svc.read_device_hardware_id("1.2.3.4", 80, "u", "p")

    @pytest.mark.asyncio
    async def test_read_hardware_id_skips_nic_without_info(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        nic = SimpleNamespace(Info=None, Enabled=True)  # Info 缺失 → 跳过该网卡
        devicemgmt = AsyncMock()
        devicemgmt.GetNetworkInterfaces = AsyncMock(return_value=[nic])
        info = SimpleNamespace(HardwareId="AA:BB:CC:DD:EE:FF", SerialNumber="12345")
        devicemgmt.GetDeviceInformation = AsyncMock(return_value=info)
        cam = MagicMock()
        cam.update_xaddrs = AsyncMock()
        cam.create_devicemgmt_service = AsyncMock(return_value=devicemgmt)
        cam.close = AsyncMock()
        with patch("onvif.ONVIFCamera", return_value=cam):
            result = await svc.read_device_hardware_id("192.168.4.16", 80, "u", "p")
        assert result == "AA:BB:CC:DD:EE:FF"
        cam.close.assert_awaited_once()

    def test_check_port_open_real_socket(self):
        """真 socket：监听端口 → True；拒绝连接的端口 → False。"""
        from app.services.camera_discovery_service import CameraDiscoveryService
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            assert CameraDiscoveryService._check_port_open("127.0.0.1", port, 1.0) is True
        finally:
            server.close()
        # 取一个当前未监听的端口（复用刚关闭的端口大概率无人监听）
        assert CameraDiscoveryService._check_port_open("127.0.0.1", port, 0.2) is False

    def test_list_subnet_ips_invalid_returns_empty(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        assert CameraDiscoveryService._list_subnet_ips("not-a-cidr") == []

    @pytest.mark.asyncio
    async def test_probe_candidate_with_creds_success_and_failure(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        svc._probe_creds = (80, "u", "p")
        with patch.object(svc, "read_device_hardware_id",
                          AsyncMock(return_value="aabbccddeeff")):
            assert await svc._probe_candidate("10.0.0.5") == "aabbccddeeff"
        with patch.object(svc, "read_device_hardware_id",
                          AsyncMock(side_effect=RuntimeError("timeout"))):
            assert await svc._probe_candidate("10.0.0.5") == ""
        svc._probe_creds = (80, "", "p")  # 凭证不全 → 直接跳过
        assert await svc._probe_candidate("10.0.0.5") == ""

    @pytest.mark.asyncio
    async def test_find_camera_infers_subnet_from_rtsp_url(self, monkeypatch):
        """cameras 行无 ptz_ip 时从 rtsp_url 提 host 推子网。"""
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        db = MagicMock()
        svc.set_db(db)

        async def fake_get(cid):
            return {"id": "cam_a", "device_mac": "aabbccddeeff", "ptz_ip": "",
                    "ptz_port": 80, "ptz_username": "u", "ptz_password": "p",
                    "rtsp_url": "rtsp://10.9.8.7:554/live", "discovery_subnet": "",
                    "discovery_enabled": 1}
        db.cameras_get = fake_get
        with patch.object(svc, "_scan_ports", AsyncMock(return_value=[])), \
             patch("asyncio.sleep", AsyncMock()):
            result = await svc.find_camera("cam_a", timeout=0.01)
        assert result is None
        assert svc._probe_creds == (80, "u", "p")
        assert svc._status == "not_found"

    @pytest.mark.asyncio
    async def test_find_camera_legacy_path_subnet_from_rtsp(self, monkeypatch):
        from app.core import config as cfg
        from app.services.camera_discovery_service import CameraDiscoveryService
        monkeypatch.setitem(cfg.CONFIG, "ptz", dict(cfg.CONFIG.get("ptz", {}), ip=""))
        monkeypatch.setitem(cfg.CONFIG, "vision",
                            dict(cfg.CONFIG.get("vision", {}),
                                 device_mac="aabbccddeeff",
                                 rtsp_url="rtsp://192.168.9.9:554/s"))
        svc = CameraDiscoveryService()
        with patch.object(svc, "_scan_ports", AsyncMock(return_value=[])), \
             patch("asyncio.sleep", AsyncMock()):
            assert await svc.find_camera(timeout=0.01) is None
        assert svc._status == "not_found"

    @pytest.mark.asyncio
    async def test_find_camera_without_subnet_errors(self, monkeypatch):
        """无 MAC 参数外的子网且无法推断（ptz.ip 与 rtsp_url 都没有）→ error。"""
        from app.core import config as cfg
        from app.services.camera_discovery_service import CameraDiscoveryService
        monkeypatch.setitem(cfg.CONFIG, "ptz", dict(cfg.CONFIG.get("ptz", {}), ip=""))
        monkeypatch.setitem(cfg.CONFIG, "vision",
                            dict(cfg.CONFIG.get("vision", {}), rtsp_url=""))
        svc = CameraDiscoveryService()
        assert await svc.find_camera(target_mac="aabbccddeeff", subnet="") is None
        assert svc._status == "error" and svc._last_error == "无法推断子网"

    @pytest.mark.asyncio
    async def test_scan_locked_subnet_without_hosts_errors(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        result = await svc._scan_locked("aabbccddeeff", "garbage-subnet", 1.0)
        assert result is None
        assert svc._status == "error" and "无可用 IP" in svc._last_error

    @pytest.mark.asyncio
    async def test_apply_found_ip_guards(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        db = MagicMock()
        db.cameras_get = AsyncMock()
        db.cameras_update = AsyncMock()
        svc.set_db(db)
        await svc.apply_found_ip("cam_a", "   ")  # 空 IP → skip
        db.cameras_get.assert_not_awaited()
        await svc.apply_found_ip("cam_a", "8.8.8.8")  # 公网 → 拒绝
        db.cameras_get.assert_not_awaited()
        await svc.apply_found_ip("cam_a", "camera.example.com")  # 非 IP → 拒绝
        db.cameras_get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_found_ip_missing_row_returns(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        db = MagicMock()
        db.cameras_get = AsyncMock(return_value=None)
        db.cameras_update = AsyncMock()
        svc.set_db(db)
        await svc.apply_found_ip("cam_a", "192.168.1.99")
        db.cameras_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_found_ip_callback_failure_isolated(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        db = MagicMock()
        db.cameras_get = AsyncMock(
            return_value={"id": "cam_a", "rtsp_url": "", "ptz_ip": "192.168.1.1"})
        db.cameras_update = AsyncMock()
        svc.set_db(db)

        def boom(cid, ip):
            raise RuntimeError("reconnect failed")

        svc.set_on_ip_changed(boom)
        await svc.apply_found_ip("cam_a", "192.168.1.99")  # 不抛
        db.cameras_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_capture_mac_per_camera_paths(self):
        from app.services.camera_discovery_service import CameraDiscoveryService
        svc = CameraDiscoveryService()
        db = MagicMock()
        svc.set_db(db)
        rows = {
            "none": None,
            "disabled": {"id": "c", "discovery_enabled": 0},
            "mac_set": {"id": "c", "discovery_enabled": 1, "device_mac": "aabb"},
            "no_ip": {"id": "c", "discovery_enabled": 1, "device_mac": "",
                      "ptz_ip": "", "rtsp_url": ""},
            "no_creds": {"id": "c", "discovery_enabled": 1, "device_mac": "",
                         "ptz_ip": "192.168.1.5", "rtsp_url": "",
                         "ptz_username": "", "ptz_password": ""},
        }
        db.cameras_get = AsyncMock(side_effect=lambda cid: rows.get(cid))
        db.cameras_update = AsyncMock()
        read = AsyncMock(return_value="aabbccddeeff")
        for cid in ("none", "disabled", "mac_set", "no_ip", "no_creds"):
            await svc.capture_mac_on_startup(cid)
        db.cameras_update.assert_not_awaited()

        # 成功：读 MAC 写回行
        db.cameras_get = AsyncMock(return_value={
            "id": "ok", "discovery_enabled": 1, "device_mac": "",
            "ptz_ip": "192.168.1.5", "ptz_port": 80,
            "ptz_username": "u", "ptz_password": "p", "rtsp_url": ""})
        with patch.object(svc, "read_device_hardware_id", read):
            await svc.capture_mac_on_startup("ok")
        db.cameras_update.assert_awaited_once_with("ok", {"device_mac": "aabbccddeeff"})

        # 设备返回空 ID：不写回
        db.cameras_update.reset_mock()
        with patch.object(svc, "read_device_hardware_id", AsyncMock(return_value="")):
            await svc.capture_mac_on_startup("ok")
        db.cameras_update.assert_not_awaited()

        # 读取异常：非致命
        with patch.object(svc, "read_device_hardware_id",
                          AsyncMock(side_effect=RuntimeError("offline"))):
            await svc.capture_mac_on_startup("ok")

    @pytest.mark.asyncio
    async def test_capture_mac_legacy_disabled_and_no_creds(self, monkeypatch):
        from app.core import config as cfg
        from app.services.camera_discovery_service import CameraDiscoveryService
        monkeypatch.setitem(cfg.CONFIG, "vision",
                            dict(cfg.CONFIG.get("vision", {}), discovery_enabled=False))
        svc = CameraDiscoveryService()
        with patch.object(svc, "read_device_hardware_id", AsyncMock()) as rd:
            await svc.capture_mac_on_startup()  # 关闭 → 直接返回
        rd.assert_not_awaited()

        monkeypatch.setitem(cfg.CONFIG, "vision",
                            dict(cfg.CONFIG.get("vision", {}),
                                 discovery_enabled=True, device_mac=""))
        monkeypatch.setitem(cfg.CONFIG, "ptz",
                            dict(cfg.CONFIG.get("ptz", {}), ip="192.168.1.50"))
        monkeypatch.delenv("PTZ_PASSWORD", raising=False)
        with patch.object(svc, "read_device_hardware_id", AsyncMock()) as rd:
            await svc.capture_mac_on_startup()  # 无凭证 → 跳过
        rd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_capture_mac_legacy_empty_hardware_id(self, monkeypatch):
        from app.core import config as cfg
        from app.services.camera_discovery_service import CameraDiscoveryService
        monkeypatch.setitem(cfg.CONFIG, "vision",
                            dict(cfg.CONFIG.get("vision", {}),
                                 discovery_enabled=True, device_mac=""))
        monkeypatch.setitem(cfg.CONFIG, "ptz",
                            dict(cfg.CONFIG.get("ptz", {}),
                                 ip="192.168.1.50", username="admin"))
        monkeypatch.setenv("PTZ_PASSWORD", "test-pwd")
        update = AsyncMock()
        monkeypatch.setattr("app.services.camera_discovery_service.update_config_section",
                            update)
        svc = CameraDiscoveryService()
        with patch.object(svc, "read_device_hardware_id", AsyncMock(return_value="")):
            await svc.capture_mac_on_startup()
        update.assert_not_awaited()


# ============================================================================
# app/virtual_camera_stream.py
# ============================================================================

def _make_vcam(trigger=None, **kw):
    from app.virtual_camera_stream import VirtualCameraStream
    return VirtualCameraStream(
        camera_id="vcam_cov",
        config={"name": "覆盖测试", "display_enabled": 0, "motion_threshold": 15,
                "motion_check_interval": 0.01, "frame_interval_ms": 0,
                "vision_use_img_count": 3},
        vision_service=None, on_automation_trigger=trigger, **kw)


def _frame(value: int = 128, size: int = 32):
    import numpy as np
    return np.full((size, size, 3), value, dtype=np.uint8)


class TestVirtualCameraStreamGaps:
    def test_start_twice_is_noop(self):
        stream = _make_vcam()
        stream.start()
        first = stream._thread
        stream.start()  # 已在运行 → 直接返回
        assert stream._thread is first
        stream.stop()

    def test_enqueue_drops_when_queue_full(self):
        """队列容量 8：满后丢弃并计数，enqueue_frame 返回 False。"""
        stream = _make_vcam()  # 不 start：worker 不消费
        for _ in range(8):
            assert stream.enqueue_frame(_frame()) is True
        assert stream.enqueue_frame(_frame()) is False
        assert stream._dropped_frames == 1

    def test_none_frame_skipped_and_pipeline_survives(self):
        """注入 None 帧：跳过不崩，后续真实帧照常入缓冲。"""
        stream = _make_vcam()
        stream.start()
        try:
            stream.enqueue_frame(None)
            assert stream.enqueue_frame(_frame(99)) is True
            deadline = time.time() + 3
            while not stream.get_recent_frames() and time.time() < deadline:
                time.sleep(0.02)
            assert len(stream.get_recent_frames()) >= 1
            assert stream._running is True  # worker 未崩
        finally:
            stream.stop()

    def test_idle_timeout_marks_camera_closed(self):
        """超过 frame_timeout 无新帧 → 标记离线（保留最后一帧）。"""
        stream = _make_vcam(frame_timeout=1.0)
        stream.start()
        try:
            stream.enqueue_frame(_frame())
            deadline = time.time() + 2
            st = {}
            while time.time() < deadline:
                st = stream.get_state()
                if st.get("camera_opened"):
                    break
                time.sleep(0.02)
            assert st.get("camera_opened") is True
            deadline = time.time() + 6
            while time.time() < deadline:
                st = stream.get_state()
                if not st.get("camera_opened"):
                    break
                time.sleep(0.05)
            assert st.get("camera_opened") is False
        finally:
            stream.stop()

    def test_worker_crash_is_logged_and_loop_continues(self, monkeypatch):
        """_process_frame 抛异常 → 记日志 + 短暂退避，worker 不死。"""
        import app.virtual_camera_stream as vcs
        stream = _make_vcam()
        calls = {"n": 0}
        real_process = stream._process_frame

        def flaky(frame):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("encoder boom")
            return real_process(frame)

        monkeypatch.setattr(stream, "_process_frame", flaky)
        stream.start()
        try:
            stream.enqueue_frame(_frame(10))
            stream.enqueue_frame(_frame(200))
            deadline = time.time() + 3
            while len(stream.get_recent_frames()) < 1 and time.time() < deadline:
                time.sleep(0.02)
            assert calls["n"] >= 2 and stream._running is True
        finally:
            stream.stop()

    def test_worker_alive_periodic_log(self, monkeypatch):
        """持续供帧 10s+ → 周期日志（用假时钟避免真等 10 秒）。"""
        import app.virtual_camera_stream as vcs
        stream = _make_vcam()
        real_time = time.time
        t0 = real_time()
        calls = {"n": 0}

        def fake_time():
            calls["n"] += 1
            return t0 if calls["n"] < 3 else t0 + 20.0

        logged = MagicMock()
        monkeypatch.setattr(vcs, "time", SimpleNamespace(time=fake_time))
        monkeypatch.setattr(vcs.logger, "info", logged)
        stream.start()
        try:
            for i in range(6):
                stream.enqueue_frame(_frame(50 + i * 20))
                time.sleep(0.01)
            deadline = time.time() + 3
            while not logged.called and time.time() < deadline:
                time.sleep(0.02)
            assert logged.called
            args = logged.call_args[0]
            assert "worker alive" in args[0]
            assert args[1] == "vcam_cov"  # camera_id 位置参数
            assert args[3] == 0  # dropped 计数
        finally:
            stream.stop()


# ============================================================================
# app/migrations.py
# ============================================================================

class FakeMigrationsDb:
    def __init__(self, users=None, settings=None, kv=None, remap=0):
        self._users = users or []
        self._settings = settings or {}
        self._kv = kv or {}
        self._remap = remap

    async def user_list_all(self):
        return self._users

    async def user_setting_get(self, user_id, key):
        return self._settings.get((user_id, key))

    async def kv_get(self, key):
        return self._kv.get(key)

    async def kv_set(self, key, value):
        self._kv[key] = value

    async def cameras_remap_frame_interval(self, old_ms, new_ms):
        return self._remap


class TestMigrationsGaps:
    @pytest.mark.asyncio
    async def test_llm_keys_skip_users_without_keys_then_migrate(self, monkeypatch):
        """config 无 key：跳过无 key 用户，迁移第一个有 key 的用户（含 providers）。"""
        import app.migrations as mig
        db = FakeMigrationsDb(
            users=[{"id": "u0", "username": "empty-array"}, {"id": "u1", "username": "empty"},
                   {"id": "u2", "username": "rich"}],
            settings={("u0", "llm_keys"): json.dumps([]),
                      ("u2", "llm_keys"): json.dumps(["sk-1"]),
                      ("u2", "providers"): json.dumps({"p": {"base_url": "http://x"}})})
        update_mem = MagicMock()
        save_keys = MagicMock()
        update_section = MagicMock()
        monkeypatch.setattr(mig, "update_memory_config", update_mem)
        monkeypatch.setattr(mig, "save_global_llm_keys", save_keys)
        monkeypatch.setattr(mig, "update_config_section", update_section)
        await mig.migrate_global_llm_keys(db)
        update_mem.assert_any_call("llm_keys", ["sk-1"])
        update_mem.assert_any_call("providers", {"p": {"base_url": "http://x"}})
        save_keys.assert_called_once_with(["sk-1"])
        update_section.assert_called_once_with("providers", {"p": {"base_url": "http://x"}})

    @pytest.mark.asyncio
    async def test_llm_keys_persist_failure_logged_not_raised(self, monkeypatch):
        import app.migrations as mig
        db = FakeMigrationsDb(
            users=[{"id": "u1", "username": "a"}],
            settings={("u1", "llm_keys"): json.dumps(["sk-1"])})
        monkeypatch.setattr(mig, "update_memory_config", MagicMock())
        monkeypatch.setattr(mig, "save_global_llm_keys",
                            MagicMock(side_effect=OSError("config locked")))
        await mig.migrate_global_llm_keys(db)  # 不抛

    @pytest.mark.asyncio
    async def test_home_info_skips_empty_and_invalid_then_migrates(self, monkeypatch):
        import app.migrations as mig
        db = FakeMigrationsDb(
            users=[{"id": "u1", "username": "no-home"},
                   {"id": "u2", "username": "bad-json"},
                   {"id": "u3", "username": "ok"}],
            settings={("u1", "home_info"): None,
                      ("u2", "home_info"): "{not json",
                      ("u3", "home_info"): json.dumps({"city": "杭州", "district": "西湖",
                                                       "province": "浙江"})})
        update_section = MagicMock()
        monkeypatch.setattr(mig, "update_config_section", update_section)
        await mig.migrate_home_info(db)
        update_section.assert_called_once_with("home", {
            "home_name": "", "owner_name": "", "province": "浙江",
            "city": "杭州", "district": "西湖"})

    @pytest.mark.asyncio
    async def test_load_vision_focuses_invalid_json_warns(self):
        import app.migrations as mig
        db = FakeMigrationsDb(kv={"vision_focuses": "{bad json"})
        vs = MagicMock()
        await mig.load_vision_focuses(db, vs)
        vs.load_focuses.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_vision_focuses_legacy_single_focus_migrates(self):
        import app.migrations as mig
        db = FakeMigrationsDb(kv={"vision_focus": "门口有人时提醒我"})
        vs = MagicMock()
        vs.get_vision_focuses.return_value = ["门口有人时提醒我"]
        await mig.load_vision_focuses(db, vs)
        vs.add_focus.assert_called_once_with("门口有人时提醒我")
        assert db._kv["vision_focuses"] == json.dumps(["门口有人时提醒我"])


# ============================================================================
# app/services/entity_controls.py
# ============================================================================

class TestEntityControlsGaps:
    @staticmethod
    def resolve(entity_id, state, attrs, services):
        from app.services.entity_controls import resolve_controls
        return resolve_controls({"entity_id": entity_id, "state": state,
                                 "attributes": attrs}, services)

    def test_plural_options_without_service_field_skipped(self):
        """单数 attr 有复数选项但服务无对应 field → 不生成控件（1b 分支）。"""
        services = {"light": {"turn_on": {"fields": ["entity_id", "brightness_pct"]}}}
        controls = self.resolve("light.x", "on",
                                {"sound_mode": "rock", "sound_modes": ["rock", "pop"]},
                                services)
        assert "sound_mode" not in controls

    def test_plural_attr_without_service_field_skipped(self):
        """单数 attr 有复数选项但服务无对应 field → 不生成控件。"""
        controls = self.resolve("light.x", "on", {"sound_mode": "rock"}, {})
        assert controls == {}

    def test_current_attr_with_base_in_attrs_skipped(self):
        """current_X 且 X 同时在 attrs → current_X 是读数，跳过。"""
        services = {"climate": {"set_temperature": {"fields": ["entity_id", "temperature"]}}}
        controls = self.resolve("climate.x", "cool",
                                {"current_temperature": 21, "temperature": 22}, services)
        assert "current_temperature" not in controls
        assert "temperature" in controls

    def test_current_attr_without_matching_field_skipped(self):
        services = {"climate": {"set_temperature": {"fields": ["entity_id", "temperature"]}}}
        controls = self.resolve("climate.x", "cool", {"current_humidity": 40}, services)
        assert controls == {}

    def test_raw_brightness_field_skipped(self):
        """brightness 原始值（0-255）跳过，只暴露 _pct。"""
        services = {"light": {"turn_on": {"fields": ["entity_id", "brightness"]}}}
        controls = self.resolve("light.x", "on", {"brightness": 128}, services)
        assert "brightness" not in controls
        assert controls == {}

    def test_action_service_already_in_controls_not_overwritten(self):
        """枚举控件已占用键 → 同名无参服务不覆盖为 action。"""
        services = {"vacuum": {
            "set_fan_speed": {"fields": ["entity_id", "fan_speed"]},
            "fan_speed": {"fields": []}}}
        controls = self.resolve("vacuum.x", "cleaning",
                                {"fan_speed": "high", "fan_speeds": ["low", "high"]},
                                services)
        assert controls["fan_speed"]["type"] == "enum"
        assert controls["fan_speed"]["options"] == ["low", "high"]

    def test_concept_match_filters_unrelated_specializations(self):
        """无 tilt 属性时 open_cover_tilt 被过滤（去掉 tilt 后仍是合法服务），其余保留。"""
        services = {"cover": {"open_cover": {"fields": []},
                              "close_cover": {"fields": []},
                              "open_cover_tilt": {"fields": []}}}
        controls = self.resolve("cover.win", "open", {}, services)
        assert "open_cover" in controls
        assert "open_cover_tilt" not in controls
        assert "close_cover" in controls  # 去掉 close 只剩 cover（非服务名）→ 保留

    def test_concept_match_partial_word_in_attr_keeps_action(self):
        """词是属性名的子串（tilt ⊂ current_tilt_position）→ 视为相关。"""
        services = {"cover": {"open_cover_tilt": {"fields": []},
                              "open_cover": {"fields": []}}}
        controls = self.resolve("cover.win", "open",
                                {"current_tilt_position": 50}, services)
        assert "open_cover_tilt" in controls

    def test_missing_pct_slider_inferred_when_light_off(self):
        """灯关时无 brightness 属性 → 从 turn_on 的 brightness_pct 反推滑块。"""
        services = {"light": {"turn_on": {"fields": ["entity_id", "brightness_pct"]}}}
        controls = self.resolve("light.x", "off", {"brightness": None}, services)
        slider = controls["brightness"]
        assert slider["type"] == "slider"
        assert slider["service"] == "turn_on" and slider["param"] == "brightness_pct"
        assert (slider["min"], slider["max"], slider["current"]) == (0, 100, 0)

    def test_section5_skips_numeric_attr_already_handled(self):
        """set_ 服务的 field 对应数值 attr 在第 2 节被 brightness 规则跳过 → 5 节跳过。"""
        services = {"light": {"set_brightness": {"fields": ["entity_id", "brightness"]}}}
        controls = self.resolve("light.x", "on", {"brightness": 128}, services)
        assert controls == {}  # 原始 brightness(0-255) 不做滑块，第 5 节也不再补

    def test_pct_attr_renames_slider_key_to_base_name(self):
        """brightness_pct 数值滑块的 key 统一为基础名 brightness。"""
        services = {"light": {"set_brightness": {
            "fields": ["entity_id", "brightness", "brightness_pct"]}}}
        controls = self.resolve("light.x", "on",
                                {"brightness": 128, "brightness_pct": 50}, services)
        assert set(controls) == {"brightness"}
        assert controls["brightness"]["param"] == "brightness_pct"
        assert controls["brightness"]["current"] == 50

    def test_section5_pct_field_skipped_and_action_word_substring(self):
        """set_ 服务的 _pct field 直接跳过；动作服务名是 attr 名的子串 → 保留动作。"""
        services = {"climate": {"set_temperature": {
            "fields": ["entity_id", "temperature", "temperature_pct"]},
            "action": {"fields": []}}}
        controls = self.resolve("climate.x", "idle",
                                {"actions_history": "on,off"}, services)
        # w="action" 不是完整属性词但是 "actions" 的子串 → 视为相关，动作保留
        assert controls["action"]["type"] == "action"

    def test_controls_to_text_formats(self):
        from app.services.entity_controls import _action, _enum, _slider, controls_to_text
        entity = {"entity_id": "light.bed", "attributes": {"friendly_name": "床头灯"}}
        controls = {
            "brightness": _slider("turn_on", "brightness_pct", 0, 100, 1, 40, "%"),
            "effect": _enum("turn_on", "effect", ["rain", "sun"], "rain"),
            "toggle": _action("toggle"),
        }
        text = controls_to_text(entity, controls, note="继电器反转\n注意安全")
        assert text.startswith("床头灯 (light.bed)")
        assert "备注（用户自定义，优先级最高）：继电器反转" in text
        assert "备注（用户自定义，优先级最高）：注意安全" in text
        assert "Brightness — 0%~100%, now 40%" in text
        assert "options: rain|sun, now rain" in text
        assert "Toggle — action" in text
        # 子实体缩进 + 子名 + 空控件
        sub = controls_to_text({"entity_id": "light.bed_child", "attributes": {}},
                               {}, indent=1, sub_name="左键")
        assert "子功能 light.bed_child（左键）" in sub
        assert "(no controls)" in sub

    def test_find_field_pct_fallback(self):
        from app.services.entity_controls import _find_field
        svcs = {"turn_on": {"fields": ["entity_id", "brightness_pct"]}}
        assert _find_field(svcs, "brightness") == {
            "service": "turn_on", "field": "brightness_pct"}

    def test_find_field_attr_names_word_fallback(self):
        from app.services.entity_controls import _find_field
        svcs = {"set_swing_mode": {"fields": ["entity_id", "preset_mode"]}}
        match = _find_field(svcs, "preset", {"preset", "mode"})
        assert match == {"service": "set_swing_mode", "field": "preset_mode"}
        # 属性词与任何 field 部件都无关 → None
        assert _find_field(svcs, "nothing", {"zzz"}) is None

    def test_attr_key_scan_variants(self):
        from app.services.entity_controls import _attr_key
        # 前缀扫描命中短 base：min_temp → temperature
        assert _attr_key({"min_temp": 16}, "min", "temperature") == "min_temp"
        assert _attr_key({"mintemperature": 5}, "min", "temperature") == "mintemperature"
        assert _attr_key({"min_temperature": 16}, "min", "temperature") == "min_temperature"
        # base 与目标无派生关系 → None
        assert _attr_key({"min_X_step": 1}, "min", "brightness", "_step") is None
        assert _attr_key({"unrelated": 1}, "min", "temperature") is None


# ============================================================================
# app/services/automation_service.py
# ============================================================================

def _rule(**kw):
    base = {"id": "r1", "name": "规则R", "type": "time", "condition": "条件",
            "enabled": True, "user_id": "", "actions": [],
            "cooldown_seconds": 0, "last_triggered_at": 0.0}
    base.update(kw)
    return base


def _make_svc(rules, registry=None):
    from app.services.automation_service import AutomationService
    reg = registry or MagicMock()
    reg.list_rules.return_value = rules
    return AutomationService(reg)


@pytest.fixture()
def _quiet_context(monkeypatch):
    """_build_condition_context 的外部依赖：时间处理器 + 天气（返回 error 不缓存）。"""
    import app.mcp.local_mcp_servers as lms
    import app.mcp.weather_tools as wt
    monkeypatch.setattr(lms, "current_time_handler",
                        AsyncMock(return_value={"date": "2026-09-05", "weekday": "周六",
                                                "time": "10:00:00"}))
    monkeypatch.setattr(wt, "get_weather_handler",
                        AsyncMock(return_value={"error": "not configured"}))


@pytest.fixture()
def _metrics_ok(monkeypatch):
    import app.container as container_mod
    metrics = MagicMock()
    monkeypatch.setattr(container_mod, "get_container",
                        lambda: SimpleNamespace(metrics_service=metrics))
    return metrics


@pytest.fixture()
def _db_log(monkeypatch):
    from app.core.database import Database
    db = MagicMock()
    db.vision_log_insert = AsyncMock()
    monkeypatch.setattr(Database, "get", staticmethod(lambda: db))
    return db


class TestEvaluateGaps:
    @pytest.mark.asyncio
    async def test_ha_snapshot_failure_disables_gate(self, _quiet_context, monkeypatch):
        """HA 快照拉取失败 → 门控禁用，评估照常进行。"""
        ha = MagicMock()
        ha.get_states_snapshot = AsyncMock(side_effect=RuntimeError("ha down"))
        client = MagicMock()
        client.chat = AsyncMock(return_value="0")
        svc = _make_svc([_rule(condition="夜里了")])
        svc._ha_service = ha
        monkeypatch.setattr(svc, "_resolve_chat_client", AsyncMock(return_value=client))
        applied = await svc.evaluate(rule_types=("time",))
        assert applied == []
        ha.get_states_snapshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cooldown_and_empty_condition_skipped(self, _quiet_context):
        now = time.time()
        svc = _make_svc([
            _rule(id="cool", last_triggered_at=now, cooldown_seconds=60),
            _rule(id="empty", condition="   "),
        ])
        applied = await svc.evaluate(rule_types=("time",))
        assert applied == []

    @pytest.mark.asyncio
    async def test_chat_rule_timeout_skips_group(self, _quiet_context, monkeypatch):
        from app.services.automation_service import _EVAL_TIMEOUT_SECONDS
        monkeypatch.setattr("app.services.automation_service._EVAL_TIMEOUT_SECONDS", 0.05)
        client = MagicMock()

        async def slow_chat(*a, **k):
            await asyncio.sleep(0.5)
            return "1"

        client.chat = slow_chat
        svc = _make_svc([_rule(condition="夜里了")])
        monkeypatch.setattr(svc, "_resolve_chat_client", AsyncMock(return_value=client))
        applied = await svc.evaluate(rule_types=("time",))
        assert applied == []

    @pytest.mark.asyncio
    async def test_vision_rule_timeout_skips_group(self, _quiet_context, monkeypatch):
        monkeypatch.setattr("app.services.automation_service._EVAL_TIMEOUT_SECONDS", 0.05)

        async def slow_eval(*a, **k):
            await asyncio.sleep(0.5)
            return 1

        vs = SimpleNamespace(encode_frames_b64=AsyncMock(return_value="b64"),
                             evaluate_condition=slow_eval)
        svc = _make_svc([_rule(type="vision", condition="有人在")])
        svc._vision_service = vs
        applied = await svc.evaluate(frames=[object()], rule_types=("vision",))
        assert applied == []

    @pytest.mark.asyncio
    async def test_chat_rule_hit_executes_action_and_logs(self, _quiet_context,
                                                          _metrics_ok, _db_log,
                                                          monkeypatch):
        import app.services.alert_service as alert_mod
        alert = SimpleNamespace(record=AsyncMock())
        monkeypatch.setattr(alert_mod, "alert_service", alert)
        client = MagicMock()
        client.chat = AsyncMock(return_value="判定：1")
        executor = MagicMock()
        executor.resolve_tool_name.return_value = "ha_devices___call_service"
        executor.execute_tool_by_name = AsyncMock(return_value={"success": True})
        registry = MagicMock()
        rules = [_rule(
            condition="现在是深夜",
            actions=[{"mcp_tool_name": "call_service",
                      "mcp_tool_input": {"domain": "light", "service": "turn_off",
                                         "entity_id": "light.bed"}}])]
        svc = _make_svc(rules, registry=registry)
        svc._tool_executor = executor
        monkeypatch.setattr(svc, "_resolve_chat_client", AsyncMock(return_value=client))
        applied = await svc.evaluate(rule_types=("time",))
        assert len(applied) == 1
        assert applied[0]["result"]["tool"] == "ha_devices___call_service"
        executor.execute_tool_by_name.assert_awaited_once()
        registry.update_trigger_time.assert_called_once()
        _metrics_ok.record_automation_eval.assert_called_once()
        _metrics_ok.record_tool_call.assert_called_once_with("ha_devices___call_service")
        alert.record.assert_awaited_once()
        _db_log.vision_log_insert.assert_awaited()  # rule_eval + action 两条留痕
        kinds = [c.args[1] for c in _db_log.vision_log_insert.await_args_list]
        assert "rule_eval" in kinds and "action" in kinds

    @pytest.mark.asyncio
    async def test_metrics_failure_silent(self, _quiet_context, monkeypatch):
        import app.container as container_mod
        monkeypatch.setattr(container_mod, "get_container",
                            MagicMock(side_effect=RuntimeError("no container")))
        client = MagicMock()
        client.chat = AsyncMock(return_value="0")
        svc = _make_svc([_rule(condition="夜里了")])
        monkeypatch.setattr(svc, "_resolve_chat_client", AsyncMock(return_value=client))
        assert await svc.evaluate(rule_types=("time",)) == []

    @pytest.mark.asyncio
    async def test_alert_failure_silent(self, _quiet_context, _metrics_ok, _db_log,
                                        monkeypatch):
        """触发事件落库失败（含无动作规则）静默，不影响返回。"""
        import app.services.alert_service as alert_mod
        alert = SimpleNamespace(record=AsyncMock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(alert_mod, "alert_service", alert)
        client = MagicMock()
        client.chat = AsyncMock(return_value="1")
        svc = _make_svc([_rule(condition="夜里了")])  # 无 actions 也会记触发事件
        monkeypatch.setattr(svc, "_resolve_chat_client", AsyncMock(return_value=client))
        assert await svc.evaluate(rule_types=("time",)) == []
        alert.record.assert_awaited_once()  # 失败被吞，不抛

    @pytest.mark.asyncio
    async def test_rule_exception_in_results_skipped(self, _quiet_context, _db_log):
        """gather(return_exceptions=True) 返回的异常被记日志跳过。"""
        vs = SimpleNamespace(
            encode_frames_b64=AsyncMock(return_value="b64"),
            evaluate_condition=AsyncMock(side_effect=RuntimeError("vl exploded")))
        svc = _make_svc([_rule(type="vision", condition="有人在")])
        svc._vision_service = vs
        applied = await svc.evaluate(frames=[object()], rule_types=("vision",))
        assert applied == []
        _db_log.vision_log_insert.assert_not_awaited()


class TestExecuteActionGaps:
    @pytest.mark.asyncio
    async def test_action_without_tool_name_skipped(self, _db_log):
        from app.services.automation_service import AutomationService
        svc = AutomationService(MagicMock())
        assert await svc._execute_action({}) is None

    @pytest.mark.asyncio
    async def test_dry_run_records_without_executing(self, _db_log):
        from app.services.automation_service import AutomationService
        svc = AutomationService(MagicMock())
        executor = MagicMock()
        executor.execute_tool_by_name = AsyncMock()
        svc._tool_executor = executor
        task = {"mcp_tool_name": "call_service",
                "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                   "entity_id": "light.x"}}
        ret = await svc._execute_action(task, camera_id="vcam", dry_run=True)
        assert ret["dry_run"] is True and ret["tool"] == "call_service"
        executor.execute_tool_by_name.assert_not_awaited()
        _db_log.vision_log_insert.assert_awaited_once()
        args = _db_log.vision_log_insert.await_args
        assert args.args[1] == "action" and args.args[2]["attempted"] is False

    @pytest.mark.asyncio
    async def test_no_executor_returns_none(self, _db_log):
        from app.services.automation_service import AutomationService
        svc = AutomationService(MagicMock())
        assert await svc._execute_action({"mcp_tool_name": "x"}) is None

    @pytest.mark.asyncio
    async def test_executor_exception_records_error(self, _db_log, _metrics_ok):
        from app.services.automation_service import AutomationService
        executor = MagicMock()
        executor.resolve_tool_name.return_value = "resolved_tool"
        executor.execute_tool_by_name = AsyncMock(side_effect=RuntimeError("rpc down"))
        svc = AutomationService(MagicMock())
        svc._tool_executor = executor
        ret = await svc._execute_action({"mcp_tool_name": "x",
                                         "mcp_tool_input": {"domain": "light"}})
        assert ret is None
        _metrics_ok.record_tool_call.assert_called_once_with("resolved_tool", error=True)
        args = _db_log.vision_log_insert.await_args
        assert args.args[2]["error"] == "execution failed"

    @pytest.mark.asyncio
    async def test_failed_result_records_error(self, _db_log, _metrics_ok):
        from app.services.automation_service import AutomationService
        executor = MagicMock()
        executor.resolve_tool_name.return_value = "resolved_tool"
        executor.execute_tool_by_name = AsyncMock(
            return_value={"success": False, "error": "entity 不存在"})
        svc = AutomationService(MagicMock())
        svc._tool_executor = executor
        ret = await svc._execute_action({"mcp_tool_name": "x", "mcp_tool_input": {}})
        assert ret is None
        args = _db_log.vision_log_insert.await_args
        assert "不存在" in args.args[2]["error"]

    @pytest.mark.asyncio
    async def test_metrics_recording_failure_silent_both_paths(self, _db_log, monkeypatch):
        """工具调用指标记录失败（成功/异常两条路径）都不影响动作结果。"""
        import app.container as container_mod
        monkeypatch.setattr(container_mod, "get_container",
                            MagicMock(side_effect=RuntimeError("no container")))
        executor = MagicMock()
        executor.resolve_tool_name.return_value = "resolved_tool"
        executor.execute_tool_by_name = AsyncMock(return_value={"success": True})
        from app.services.automation_service import AutomationService
        svc = AutomationService(MagicMock())
        svc._tool_executor = executor
        ret = await svc._execute_action({"mcp_tool_name": "x", "mcp_tool_input": {}})
        assert ret["tool"] == "resolved_tool"
        # 异常路径
        executor.execute_tool_by_name = AsyncMock(side_effect=RuntimeError("rpc down"))
        assert await svc._execute_action({"mcp_tool_name": "x", "mcp_tool_input": {}}) is None
        _db_log.vision_log_insert.assert_awaited()  # 动作留痕仍写入

    @pytest.mark.asyncio
    async def test_success_truncates_result_summary(self, _db_log, _metrics_ok):
        from app.services.automation_service import AutomationService
        executor = MagicMock()
        executor.resolve_tool_name.return_value = "resolved_tool"
        result = {"success": True, "new_state": {"state": "on"}, "verified": True,
                  "extra_a": 1, "extra_b": 2, "extra_c": 3}
        executor.execute_tool_by_name = AsyncMock(return_value=result)
        svc = AutomationService(MagicMock())
        svc._tool_executor = executor
        ret = await svc._execute_action({"mcp_tool_name": "x", "mcp_tool_input": {}})
        assert ret["tool"] == "resolved_tool" and ret["result"] == result
        args = _db_log.vision_log_insert.await_args
        assert set(args.args[2]["result"]) == set(list(result)[:5])


class TestTargetStateGaps:
    def setup_method(self):
        from app.services.automation_service import AutomationService
        self.svc = AutomationService(MagicMock())

    def test_derive_turn_on_and_open_cover(self):
        assert self.svc._derive_target_state("light", "turn_on", {}) == {"state": "on"}
        assert self.svc._derive_target_state("cover", "open_cover", {}) == {"state": "open"}

    def test_derive_setters(self):
        assert self.svc._derive_target_state("climate", "set_humidity",
                                             {"humidity": 55}) == {
            "attributes": {"humidity": 55}}
        assert self.svc._derive_target_state("cover", "set_cover_position",
                                             {"position": 70}) == {
            "attributes": {"current_position": 70}}
        assert self.svc._derive_target_state("light", "set_brightness",
                                             {"brightness": 200}) == {
            "attributes": {"brightness": 200}}

    def test_matches_current_prefix_fallback(self):
        current = {"state": "on", "attributes": {"current_temperature": 26}}
        target = {"attributes": {"temperature": 26}}
        assert self.svc._matches_target_state(current, target) is True

    def test_matches_missing_attribute_is_false(self):
        current = {"state": "on", "attributes": {}}
        assert self.svc._matches_target_state(
            current, {"attributes": {"temperature": 26}}) is False

    def test_matches_string_attribute_mismatch(self):
        current = {"state": "on", "attributes": {"mode": "heat"}}
        assert self.svc._matches_target_state(
            current, {"attributes": {"mode": "cool"}}) is False
        assert self.svc._matches_target_state(
            {"state": "on", "attributes": {"mode": "cool"}},
            {"attributes": {"mode": "cool"}}) is True

    def test_matches_unknown_target_shape_is_false(self):
        assert self.svc._matches_target_state({"state": "on"}, {}) is False

    def test_virtual_dry_run_matrix(self):
        class CM:
            def __init__(self, virtual, flag):
                self.virtual = virtual
                self.flag = flag

            def is_virtual_camera(self, cid):
                return self.virtual

            def get_virtual_flag(self, cid, key, default=None):
                if key == "real_exec" and self.flag:
                    raise RuntimeError("flag boom")
                return self.flag

        assert self.svc._virtual_dry_run("") is False  # 无 camera_id
        assert self.svc._virtual_dry_run("cam") is False  # 未注入 manager
        self.svc._camera_manager = CM(virtual=False, flag=None)
        assert self.svc._virtual_dry_run("cam") is False  # 非虚拟摄像头
        self.svc._camera_manager = CM(virtual=True, flag=False)
        assert self.svc._virtual_dry_run("cam") is True  # 虚拟 + 未开真实执行
        self.svc._camera_manager = CM(virtual=True, flag=True)
        assert self.svc._virtual_dry_run("cam") is False  # 已开真实执行
        self.svc._camera_manager = CM(virtual=True, flag="boom")
        assert self.svc._virtual_dry_run("cam") is False  # 查询异常 → 保守 False


class TestResolveChatClientGaps:
    @pytest.mark.asyncio
    async def test_resolver_exception_falls_back_to_global(self, monkeypatch):
        from app.services.automation_service import AutomationService
        monkeypatch.setattr("app.core.key_resolver.resolve_key_for_role_user",
                            AsyncMock(side_effect=RuntimeError("db down")))
        fake_client_cls = MagicMock(return_value=object())
        monkeypatch.setattr("app.clients.llm_chat_client.LlmChatClient", fake_client_cls)
        svc = AutomationService(MagicMock())
        first = await svc._resolve_chat_client("u1")
        second = await svc._resolve_chat_client("u1")
        assert first is second and fake_client_cls.call_count == 1  # lazy init 复用

    @pytest.mark.asyncio
    async def test_cached_per_user_client_reused_when_signature_same(self, monkeypatch):
        from app.services.automation_service import AutomationService
        key_info = {"api_key": "sk", "base_url": "http://x", "model": "m"}
        monkeypatch.setattr("app.core.key_resolver.resolve_key_for_role_user",
                            AsyncMock(return_value=key_info))
        svc = AutomationService(MagicMock())
        sentinel = object()
        svc._per_user_clients["u1"] = (("sk", "http://x", "m"), sentinel)
        assert await svc._resolve_chat_client("u1") is sentinel

    @pytest.mark.asyncio
    async def test_context_only_failure_returns_zero(self, monkeypatch):
        from app.services.automation_service import AutomationService
        client = MagicMock()
        client.chat = AsyncMock(side_effect=RuntimeError("llm down"))
        svc = AutomationService(MagicMock())
        monkeypatch.setattr(svc, "_resolve_chat_client", AsyncMock(return_value=client))
        assert await svc._evaluate_context_only("夜里了", "ctx", "") == 0
