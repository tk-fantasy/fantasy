"""CameraManager 单测(Task 4)。

多路摄像头生命周期 + 单通道并发调度 + 单路 AI 预览(D1/D3/D4)。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.camera_manager import CameraManager


def _make_stream(camera_id, online=True, name="x", area=""):
    """Mock CameraStream。补 _config(name/area)供 list_cameras 读。"""
    s = MagicMock()
    s.camera_id = camera_id
    s._config = {"name": name, "area": area}
    s.get_recent_frames = MagicMock(return_value=[b"frame"])
    s.get_latest_frame = MagicMock(return_value=b"frame")
    s.start = MagicMock()
    s.stop = MagicMock()
    s.set_event_loop = MagicMock()
    s.set_discovery_service = MagicMock()
    s.set_on_automation_trigger = MagicMock()
    s.start_display = MagicMock()
    s.stop_display = MagicMock()
    s.get_state = MagicMock(return_value={"camera_id": camera_id, "online": online})
    s.mjpeg_generator = MagicMock(return_value=iter([b"x"]))
    return s


class TestConcurrencyLimits:
    """自动化通道并发上限(峰值=auto_concurrency)。"""

    @pytest.mark.asyncio
    async def test_automation_channel_caps_at_5(self):
        mgr = CameraManager.__new__(CameraManager)
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._streams = {}
        mgr._db = MagicMock()
        mgr._loop = asyncio.get_event_loop()

        in_flight = 0
        peak = 0

        async def fake_eval(cid, frames):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1

        mgr._eval_one = fake_eval

        tasks = [asyncio.create_task(mgr.request_automation_eval(f"cam_{i}", [b"f"])) for i in range(8)]
        await asyncio.gather(*tasks)
        assert peak == 5   # 峰值严格=auto_concurrency


class TestDisplaySingleton:
    """D4:AI 预览单例 —— enable_display 切到新路时旧路 stop_display,active 唯一。"""

    @pytest.mark.asyncio
    async def test_enable_display_switches_single_active(self):
        mgr = CameraManager.__new__(CameraManager)
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._streams = {"cam_a": _make_stream("cam_a"), "cam_b": _make_stream("cam_b")}
        mgr._active_display_id = None

        await mgr.enable_display("cam_a")
        assert mgr._active_display_id == "cam_a"
        await mgr.enable_display("cam_b")
        assert mgr._active_display_id == "cam_b"
        # 旧路停预览,新路起预览
        mgr._streams["cam_a"].stop_display.assert_called_once()
        mgr._streams["cam_b"].start_display.assert_called_once()

    @pytest.mark.asyncio
    async def test_enable_same_camera_noop(self):
        """重复 enable 同一路不重复 start_display。"""
        mgr = CameraManager.__new__(CameraManager)
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._streams = {"cam_a": _make_stream("cam_a")}
        mgr._active_display_id = None

        await mgr.enable_display("cam_a")
        await mgr.enable_display("cam_a")   # 同一路
        mgr._streams["cam_a"].start_display.assert_called_once()

    @pytest.mark.asyncio
    async def test_disable_display_clears_active(self):
        mgr = CameraManager.__new__(CameraManager)
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._streams = {"cam_a": _make_stream("cam_a")}
        mgr._active_display_id = "cam_a"

        await mgr.disable_display("cam_a")
        assert mgr._active_display_id is None
        mgr._streams["cam_a"].stop_display.assert_called_once()


class TestListCameras:
    @pytest.mark.asyncio
    async def test_list_cameras_returns_camera_info(self):
        mgr = CameraManager.__new__(CameraManager)
        mgr._streams = {
            "cam_a": _make_stream("cam_a", online=True, name="客厅", area="客厅"),
            "cam_b": _make_stream("cam_b", online=False, name="门口", area="玄关"),
        }
        mgr._db = MagicMock()
        cams = mgr.list_cameras()
        ids = {c["id"] for c in cams}
        assert ids == {"cam_a", "cam_b"}
        a = next(c for c in cams if c["id"] == "cam_a")
        assert a["name"] == "客厅" and a["area"] == "客厅" and a["online"] is True


class TestInitializeActivatesOnlyFirstDisplay:
    """D4:initialize 启动所有 enabled 路 worker,但 AI 预览只激活第一路 display_enabled=1。"""

    @pytest.mark.asyncio
    async def test_initialize_starts_all_workers_but_display_only_first(self, monkeypatch):
        mgr = CameraManager.__new__(CameraManager)
        mgr._vision_service = None
        mgr._discovery_service = None
        mgr._loop = asyncio.get_event_loop()
        mgr._streams = {}
        mgr._active_display_id = None

        rows = [
            {"id": "cam_a", "enabled": 1, "display_enabled": 1, "name": "客厅"},
            {"id": "cam_b", "enabled": 1, "display_enabled": 1, "name": "门口"},
            {"id": "cam_c", "enabled": 1, "display_enabled": 0, "name": "车库"},
            {"id": "cam_d", "enabled": 0, "display_enabled": 0, "name": "禁用"},
        ]
        mgr._db = MagicMock()
        mgr._db.cameras_all = AsyncMock(return_value=rows)

        spawned = []

        async def fake_spawn(row):
            cid = row["id"]
            s = _make_stream(cid, name=row["name"])
            mgr._streams[cid] = s
            spawned.append(cid)
            s.start()   # 模拟真实 _spawn 会 start worker
            return s
        monkeypatch.setattr(mgr, "_spawn", fake_spawn)

        await mgr.initialize()
        # 三路 enabled 的 worker 都启动(cam_d 禁用)
        assert set(spawned) == {"cam_a", "cam_b", "cam_c"}
        for s in mgr._streams.values():
            s.start.assert_called_once()
        # 只有第一个 display_enabled=1 的 cam_a 起预览
        mgr._streams["cam_a"].start_display.assert_called_once()
        mgr._streams["cam_b"].start_display.assert_not_called()
        assert mgr._active_display_id == "cam_a"


class TestOnAutomationTriggerBridge:
    """worker 线程回调 → 投递自动化评估到主循环。"""

    @pytest.mark.asyncio
    async def test_on_automation_trigger_schedules_eval(self):
        mgr = CameraManager.__new__(CameraManager)
        mgr._loop = asyncio.get_event_loop()
        mgr._streams = {"cam_a": _make_stream("cam_a")}
        mgr._auto_sem = asyncio.Semaphore(5)
        called = []

        async def fake_req(cid, frames):
            called.append((cid, frames))
        mgr.request_automation_eval = fake_req

        # worker 线程同步调
        mgr._on_automation_trigger("cam_a")
        # 给 run_coroutine_threadsafe 投递的协程一点时间跑
        await asyncio.sleep(0.05)
        assert len(called) == 1
        assert called[0][0] == "cam_a"
