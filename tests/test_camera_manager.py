"""CameraManager 单测(Task 4)。

多路摄像头生命周期 + 单通道并发调度 + 单路 AI 预览(D1/D3/D4)。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.camera_manager import CameraManager


def _make_stream(camera_id, online=True, name="x", area=""):
    """Mock CameraStream。补 _config(name/area)供 list_cameras 读。

    get_state 返回真实 CameraState 结构（camera_opened 而非 online），
    这样 list_cameras 的 online 推断逻辑才被真正验证。
    """
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
    # CameraState 实际字段是 camera_opened（camera_stream.py:33），无 online
    s.get_state = MagicMock(return_value={"camera_id": camera_id, "camera_opened": online})
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
        mgr._db = None
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
        mgr._db = None
        mgr._streams = {"cam_a": _make_stream("cam_a")}
        mgr._active_display_id = None

        await mgr.enable_display("cam_a")
        await mgr.enable_display("cam_a")   # 同一路
        mgr._streams["cam_a"].start_display.assert_called_once()

    @pytest.mark.asyncio
    async def test_disable_display_clears_active(self):
        mgr = CameraManager.__new__(CameraManager)
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._db = None
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
        mgr._last_trigger_at = {}
        mgr._min_trigger_interval = 3.0
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

    @pytest.mark.asyncio
    async def test_on_automation_trigger_throttles_within_window(self):
        """per-camera 节流:窗口内重复触发直接丢弃,与单摄 trigger_evaluate 一致。"""
        mgr = CameraManager.__new__(CameraManager)
        mgr._loop = asyncio.get_event_loop()
        mgr._streams = {"cam_a": _make_stream("cam_a")}
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._last_trigger_at = {}
        mgr._min_trigger_interval = 3.0
        called = []

        async def fake_req(cid, frames):
            called.append(cid)
        mgr.request_automation_eval = fake_req

        # 连续触发(同一秒内),只有首次放行
        mgr._on_automation_trigger("cam_a")
        mgr._on_automation_trigger("cam_a")
        mgr._on_automation_trigger("cam_a")
        await asyncio.sleep(0.05)
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_on_automation_trigger_independent_per_camera(self):
        """per-camera 独立计时:一路触发不饿死另一路。"""
        mgr = CameraManager.__new__(CameraManager)
        mgr._loop = asyncio.get_event_loop()
        mgr._streams = {"cam_a": _make_stream("cam_a"), "cam_b": _make_stream("cam_b")}
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._last_trigger_at = {}
        mgr._min_trigger_interval = 3.0
        called = []

        async def fake_req(cid, frames):
            called.append(cid)
        mgr.request_automation_eval = fake_req

        # 两个不同路各自首次都应放行(不是全局一个时间戳)
        mgr._on_automation_trigger("cam_a")
        mgr._on_automation_trigger("cam_b")
        await asyncio.sleep(0.05)
        assert called == ["cam_a", "cam_b"]


class TestOnCameraIpChanged:
    """discovery 回新 IP → 重建该路 stream(让 worker 用最新 rtsp_url)。

    Bug 3b:之前只打日志不重建,worker 缓存了构造时 rtsp_url、IP 变更后不回读 DB,
    导致 discovery 即使找回新 IP 写入 DB,worker 仍死磕旧 IP 连不上。
    """

    @pytest.mark.asyncio
    async def test_rebuild_stream_pops_old_and_respawns_with_latest_row(self, monkeypatch):
        """_rebuild_stream:停旧 stream、按最新 DB 行(含新 rtsp_url)重 spawn。"""
        mgr = CameraManager.__new__(CameraManager)
        old_stream = _make_stream("cam_a")
        mgr._streams = {"cam_a": old_stream}
        mgr._db = MagicMock()
        mgr._db.cameras_get = AsyncMock(return_value={
            "id": "cam_a", "enabled": 1, "rtsp_url": "rtsp://192.168.1.99/stream",
        })

        spawned: list[dict] = []

        async def fake_spawn(row):
            s = _make_stream(row["id"])
            mgr._streams[row["id"]] = s
            spawned.append(row)
            s.start()
            return s
        monkeypatch.setattr(mgr, "_spawn", fake_spawn)

        row = await mgr._rebuild_stream("cam_a")
        # 旧 stream 被停掉
        old_stream.stop.assert_called_once()
        # spawn 收到的是最新 DB 行(含新 IP 的 URL)
        assert spawned[0]["rtsp_url"] == "rtsp://192.168.1.99/stream"
        # 返回最新行
        assert row["rtsp_url"] == "rtsp://192.168.1.99/stream"

    @pytest.mark.asyncio
    async def test_rebuild_stream_skips_disabled_camera(self, monkeypatch):
        """enabled=0 的路只清旧 stream、不重 spawn。"""
        mgr = CameraManager.__new__(CameraManager)
        mgr._streams = {"cam_a": _make_stream("cam_a")}
        mgr._db = MagicMock()
        mgr._db.cameras_get = AsyncMock(return_value={"id": "cam_a", "enabled": 0})
        monkeypatch.setattr(mgr, "_spawn", AsyncMock())

        await mgr._rebuild_stream("cam_a")
        mgr._spawn.assert_not_called()
        assert "cam_a" not in mgr._streams   # 旧 stream 被 pop,无新 spawn 补回

    @pytest.mark.asyncio
    async def test_on_camera_ip_changed_schedules_rebuild(self, monkeypatch):
        """sync 回调用 run_coroutine_threadsafe 投递重建协程到 loop。"""
        mgr = CameraManager.__new__(CameraManager)
        mgr._loop = asyncio.get_event_loop()
        mgr._streams = {"cam_a": _make_stream("cam_a")}

        rebuilt: list[str] = []

        async def fake_rebuild(cid):
            rebuilt.append(cid)
        monkeypatch.setattr(mgr, "_rebuild_stream", fake_rebuild)

        # worker/discovery 线程同步调
        mgr._on_camera_ip_changed("cam_a", "192.168.1.99")
        # 给投递的协程一点时间跑
        await asyncio.sleep(0.05)
        assert rebuilt == ["cam_a"]

    def test_on_camera_ip_changed_handles_missing_loop(self, caplog):
        """loop 不可用时不抛、只 warn。"""
        mgr = CameraManager.__new__(CameraManager)
        mgr._loop = None
        with caplog.at_level("WARNING"):
            mgr._on_camera_ip_changed("cam_a", "1.2.3.4")
        assert "event loop unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_update_camera_delegates_to_rebuild(self, monkeypatch):
        """补空白:update_camera 改完 DB 后复用 _rebuild_stream 重建。"""
        mgr = CameraManager.__new__(CameraManager)
        old_stream = _make_stream("cam_a")
        mgr._streams = {"cam_a": old_stream}
        mgr._db = MagicMock()
        mgr._db.cameras_update = AsyncMock(return_value=True)
        mgr._db.cameras_get = AsyncMock(return_value={
            "id": "cam_a", "enabled": 1, "rtsp_url": "rtsp://new/stream",
        })

        async def fake_spawn(row):
            s = _make_stream(row["id"])
            mgr._streams[row["id"]] = s
            s.start()
            return s
        monkeypatch.setattr(mgr, "_spawn", fake_spawn)

        result = await mgr.update_camera("cam_a", {"rtsp_url": "rtsp://new/stream"})
        mgr._db.cameras_update.assert_called_once_with("cam_a", {"rtsp_url": "rtsp://new/stream"})
        old_stream.stop.assert_called_once()
        mgr._db.cameras_get.assert_called_once_with("cam_a")
        assert result["rtsp_url"] == "rtsp://new/stream"
