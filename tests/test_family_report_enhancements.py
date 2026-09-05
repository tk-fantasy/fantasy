"""家庭报告增强回归测试：告警状态重启重建 / 设备状态事件流 / 周报统计扩展。

覆盖三块改动：
- A3 修复：alert_service 重启后从 family_events 回放重建未恢复告警
  （修复"重启后恢复通知永久丢失，用户印象停留在离线"）
- B 新功能：DeviceEventService 的 state_changed 过滤/分类节流 + device_op 插桩
- C 增强：周报 stats 并入设备动态与对话统计，LLM 输入排除传感器逐条事件
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AlertService：重启后告警状态重建
# ---------------------------------------------------------------------------

class TestAlertStateRestore:
    def _svc(self):
        from app.services.alert_service import AlertService
        return AlertService()

    @pytest.mark.asyncio
    async def test_restore_rebuilds_unresolved_only(self):
        svc = self._svc()
        events = [
            {"kind": "alert", "source": "camera:c1", "message": "离线", "created_at": 1000},
            {"kind": "alert", "source": "camera:c2", "message": "离线", "created_at": 2000},
            {"kind": "alert_resolved", "source": "camera:c2", "message": "恢复", "created_at": 3000},
            # 任务失败没有 resolve 语义，不应重建进 _active
            {"kind": "alert", "source": "scheduler:t", "message": "x", "created_at": 4000},
        ]
        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=events)
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = db
            await svc._restore_active_from_events()
        assert set(svc._active) == {"camera:c1"}
        assert svc._active["camera:c1"]["active"] is True

    @pytest.mark.asyncio
    async def test_restore_then_resolve_notifies(self):
        """核心场景：重启前有未恢复离线告警，重启后恢复在线能补发恢复通知。"""
        svc = self._svc()
        events = [
            {"kind": "alert", "source": "camera:c1", "message": "离线", "created_at": 1000},
        ]
        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=events)
        received = []

        async def notifier(message, level):
            received.append(message)

        svc.register_notifier("t", notifier)
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = db
            await svc._restore_active_from_events()
        with patch.object(svc, "_record", new=AsyncMock()):
            await svc.resolve("camera:c1", "摄像头「c1」已恢复在线")
        assert len(received) == 1
        assert "恢复" in received[0]

    @pytest.mark.asyncio
    async def test_restore_swallows_missing_db(self):
        svc = self._svc()
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = None
            await svc._restore_active_from_events()  # 不应抛
        assert svc._active == {}


# ---------------------------------------------------------------------------
# DeviceEventService：过滤 / 分类节流
# ---------------------------------------------------------------------------

class TestDeviceEventService:
    def _svc(self):
        from app.services.device_event_service import DeviceEventService
        return DeviceEventService(ha_service=None)

    @pytest.mark.asyncio
    async def test_instant_domain_recorded_with_zh_state(self):
        svc = self._svc()
        with patch("app.services.alert_service.alert_service") as alert:
            alert.record = AsyncMock()
            await svc._on_state_changed(
                "light.bed",
                {"state": "off", "attributes": {"friendly_name": "床头灯"}},
                {"state": "on", "attributes": {"friendly_name": "床头灯"}},
            )
        alert.record.assert_awaited_once()
        kind, source, message = alert.record.await_args.args
        assert kind == "device_state"
        assert source == "device:light.bed"
        assert "床头灯" in message and "开" in message

    @pytest.mark.asyncio
    async def test_attribute_only_change_ignored(self):
        svc = self._svc()
        with patch("app.services.alert_service.alert_service") as alert:
            alert.record = AsyncMock()
            await svc._on_state_changed(
                "light.bed",
                {"state": "on", "attributes": {"brightness": 10}},
                {"state": "on", "attributes": {"brightness": 99}},
            )
        alert.record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_entity_and_delete_ignored(self):
        """HA 重启全量重发（old_state=None）与实体删除（new_state=None）不记。"""
        svc = self._svc()
        with patch("app.services.alert_service.alert_service") as alert:
            alert.record = AsyncMock()
            await svc._on_state_changed(
                "light.new", None,
                {"state": "on", "attributes": {"friendly_name": "新灯"}})
            await svc._on_state_changed(
                "light.old",
                {"state": "on", "attributes": {}},
                None)
        alert.record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_recorded_instantly(self):
        svc = self._svc()
        with patch("app.services.alert_service.alert_service") as alert:
            alert.record = AsyncMock()
            await svc._on_state_changed(
                "switch.plug",
                {"state": "on", "attributes": {"friendly_name": "插座"}},
                {"state": "unavailable", "attributes": {"friendly_name": "插座"}})
        alert.record.assert_awaited_once()
        message = alert.record.await_args.args[2]
        assert "不可用" in message

    @pytest.mark.asyncio
    async def test_sensor_buffered_then_flushed_aggregated(self):
        """传感器变化先缓冲（不逐条落库），flush 时聚合成一条。"""
        svc = self._svc()
        with patch("app.services.alert_service.alert_service") as alert:
            alert.record = AsyncMock()
            await svc._on_state_changed(
                "sensor.temp",
                {"state": "25.0"},
                {"state": "26.0", "attributes": {
                    "friendly_name": "客厅温度", "unit_of_measurement": "°C"}})
            await svc._on_state_changed(
                "sensor.temp", {"state": "26.0"}, {"state": "27.5", "attributes": {}})
            alert.record.assert_not_awaited()  # 仍在缓冲窗口内
            await svc._flush_sensors(force=True)
        alert.record.assert_awaited_once()
        message = alert.record.await_args.args[2]
        assert "客厅温度" in message
        assert "变化 2 次" in message
        assert "26~27.5°C" in message

    @pytest.mark.asyncio
    async def test_non_monitored_domain_ignored(self):
        svc = self._svc()
        with patch("app.services.alert_service.alert_service") as alert:
            alert.record = AsyncMock()
            await svc._on_state_changed(
                "update.ha_core",
                {"state": "off", "attributes": {}},
                {"state": "on", "attributes": {"friendly_name": "更新"}})
        alert.record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_record_device_op_shapes_event(self):
        from app.services.device_event_service import record_device_op
        with patch("app.services.alert_service.alert_service") as alert:
            alert.record = AsyncMock()
            await record_device_op(["light.bed"], "turn_on", "AI",
                                   {"light.bed": "床头灯"})
        alert.record.assert_awaited_once_with(
            "device_op", "device:light.bed", "AI将「床头灯」执行 打开", "AI")

    @pytest.mark.asyncio
    async def test_record_device_op_swallows_errors(self):
        from app.services.device_event_service import record_device_op
        with patch("app.services.alert_service.alert_service") as alert:
            alert.record = AsyncMock(side_effect=RuntimeError("db down"))
            await record_device_op(["light.bed"], "turn_on")  # 不应抛


# ---------------------------------------------------------------------------
# WeeklyReport：stats 扩展 + LLM 输入排除传感器事件
# ---------------------------------------------------------------------------

class TestWeeklyReportStatsExtended:
    @pytest.mark.asyncio
    async def test_stats_includes_device_and_chat(self):
        from app.services.weekly_report_service import WeeklyReportService
        svc = WeeklyReportService(llm_chat_client=None)
        events = [
            {"kind": "device_op", "source": "device:light.a",
             "message": "AI将「灯A」执行 打开"},
            {"kind": "device_op", "source": "device:light.b",
             "message": "手动将「灯B」执行 关闭"},
            {"kind": "device_state", "source": "device:sensor.c",
             "message": "温度 1 小时内变化 5 次"},
            {"kind": "device_state", "source": "device:light.a",
             "message": "灯A 开"},
        ]
        db = MagicMock()
        db.sessions_all = AsyncMock(return_value=[
            # 窗口内：2 条 user 消息 → 2 轮
            {"updated_at": time.time() * 1000,
             "model_messages": [{"role": "user"}, {"role": "assistant"},
                                {"role": "user"}]},
            # 窗口外：不计
            {"updated_at": 0, "model_messages": [{"role": "user"}]},
        ])
        with patch("app.services.weekly_report_service.Database") as db_cls:
            db_cls.get.return_value = db
            text = await svc._summarize_stats(events)
        assert "3 台设备有动态" in text
        assert "AI 操作 1 次" in text
        assert "手动操作 1 次" in text
        assert "对话 2 轮" in text

    @pytest.mark.asyncio
    async def test_stats_plain_kinds_still_work(self):
        """无设备/对话事件时输出与旧格式兼容（不出现空设备段）。"""
        from app.services.weekly_report_service import WeeklyReportService
        svc = WeeklyReportService(llm_chat_client=None)
        with patch("app.services.weekly_report_service.Database") as db_cls:
            db = MagicMock()
            db.sessions_all = AsyncMock(return_value=[])
            db_cls.get.return_value = db
            text = await svc._summarize_stats([
                {"kind": "automation", "source": "r", "message": ""},
                {"kind": "task_success", "source": "s", "message": ""},
            ])
        assert "自动化触发 1 次" in text
        assert "全部成功" in text
        assert "设备" not in text

    @pytest.mark.asyncio
    async def test_generate_excludes_sensor_events_from_llm_input(self):
        """device_state 逐条不进 LLM 输入（防传感器挤占 500 条窗口稀释告警）。"""
        from app.services.weekly_report_service import WeeklyReportService
        llm = MagicMock()
        llm.enabled = True
        captured: dict = {}

        async def fake_chat(messages, timeout):
            captured["prompt"] = messages[0]["content"]
            return "周报正文"

        llm.chat = fake_chat
        svc = WeeklyReportService(llm_chat_client=llm)
        events = [
            {"kind": "alert", "source": "camera:c1", "message": "摄像头离线",
             "created_at": 1000},
            {"kind": "device_state", "source": "device:sensor.t",
             "message": "温度 1 小时内变化 99 次", "created_at": 2000},
        ]
        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=[dict(e) for e in events])
        db.kv_get = AsyncMock(return_value=None)
        db.kv_set = AsyncMock()
        db.family_event_add = AsyncMock()
        with patch("app.services.weekly_report_service.Database") as db_cls, \
             patch("app.services.alert_service.alert_service") as alert:
            db_cls.get.return_value = db
            alert.broadcast_report = AsyncMock()
            result = await svc.generate()
        assert result["generated"] is True
        prompt = captured["prompt"]
        assert "摄像头离线" in prompt          # 告警保留
        assert "变化 99 次" not in prompt      # 传感器逐条被排除
        assert "1 台设备有动态" in prompt       # 但统计摘要里有设备信息
        assert "统计摘要" in prompt
