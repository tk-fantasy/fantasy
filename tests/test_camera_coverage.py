"""摄像机五模块深覆盖补充测试。

目标模块：app/camera_stream.py、app/routes/camera_routes.py、
app/services/camera_manager.py、app/services/camera_discovery_service.py、
app/virtual_camera_stream.py。

边界 mock 原则：只 mock cv2.VideoCapture/imencode、socket、时间（替换模块内
time 引用，sleep 只记录不真睡）、线程/线程池；不碰真实摄像头/网络/固定端口。
每个测试都断言真实行为（返回值 / 状态迁移 / 调用参数 / 异常 / 日志）。
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import os
import queue
import socket
import sys
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import cv2
import numpy as np
import pytest

import app.camera_stream as cam
import app.virtual_camera_stream as vcam
from app.camera_stream import (
    CameraStream,
    _OFFLINE_JPEG,
    _TINY_JPEG,
    _build_offline_jpeg,
)
from app.services.camera_discovery_service import CameraDiscoveryService
from app.services.camera_manager import CameraManager
from app.vision import ActionResult
from app.virtual_camera_stream import VirtualCameraStream


# ---------------------------------------------------------------------------
# 共享工具
# ---------------------------------------------------------------------------

def _config(overrides: dict | None = None) -> dict:
    """单路配置（cameras 表行形状），默认 USB 源 + 关闭预览推理。"""
    base = {
        "id": "cam_cov", "source_type": "usb", "rtsp_url": "", "usb_index": 0,
        "motion_hash_size": 16, "motion_threshold": 15, "motion_check_interval": 0.05,
        "vision_min_infer_interval": 1.0, "vision_max_idle_interval": 5.0,
        "vision_use_img_count": 3, "frame_interval_ms": 0, "display_enabled": 0,
    }
    base.update(overrides or {})
    return base


def _stream(overrides: dict | None = None, vision_service=None) -> CameraStream:
    return CameraStream(
        "cam_cov", _config(overrides),
        vision_service=vision_service if vision_service is not None else MagicMock(),
    )


def _frame(value: int = 100, h: int = 48, w: int = 64) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


class FakeTime:
    """替换目标模块内的 time 引用：time() 受控推进、sleep 只记录不真睡。"""

    def __init__(self, step: float = 0.0) -> None:
        self._now = 1_000_000.0
        self.step = step
        self.sleeps: list[float] = []

    def time(self) -> float:
        self._now += self.step
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


# ===========================================================================
# 1. app/camera_stream.py
# ===========================================================================

class TestStartStopLifecycle:
    def test_start_when_already_running_is_noop(self):
        """start() 在 _running=True 时直接返回，不再起新线程。"""
        s = _stream()
        s._running = True
        s.start()
        assert s._thread is None and s._buffer_thread is None

    def test_display_toggles(self):
        """set_display_enabled / start_display / stop_display 开关语义。"""
        s = _stream({"display_enabled": 1})
        assert s._camera_vl_display_enabled is True
        s.set_display_enabled(False)
        assert s._camera_vl_display_enabled is False
        s.start_display()
        assert s._camera_vl_display_enabled is True
        s.stop_display()
        assert s._camera_vl_display_enabled is False

    def test_stop_drains_queues_cancels_futures_releases_cap(self):
        """stop():清空两个队列、join 存活线程、取消未完成推理 future、释放 cap。"""
        s = _stream()
        s._running = True
        # 三个"存活"线程替身:stop 置 _running=False 后各自在当前 sleep 周期退出。
        # 递增的 sleep 周期保证 join 第 1 个线程时后两个仍存活,三条 join 都执行。
        def make_spin(delay):
            def spin():
                while s._running:
                    time.sleep(delay)
            return spin
        delays = (0.001, 0.03, 0.05)
        threads = [threading.Thread(target=make_spin(d), daemon=True) for d in delays]
        for t in threads:
            t.start()
        s._thread, s._buffer_thread, s._infer_scheduler_thread = threads
        # 队列残留(无消费者,只能靠 stop 排空) + 未完成 future + 已打开的 cap
        s._buffer_queue.put(_frame())
        s._infer_queue.put((_frame(), "motion"))
        pending = concurrent.futures.Future()
        s._infer_futures.append(pending)
        fake_cap = MagicMock()
        s._cap = fake_cap

        s.stop()

        assert s._buffer_queue.empty()
        assert s._infer_queue.empty()
        assert pending.cancelled()
        assert s._infer_futures == []
        assert s._cap is None
        fake_cap.release.assert_called_once()
        assert s._thread is None and s._buffer_thread is None
        assert s._infer_scheduler_thread is None
        for t in threads:
            t.join(timeout=2)
            assert not t.is_alive()
        assert s._state.camera_opened is False
        assert s.get_jpeg() is None  # _clear_cached_frame 清掉缓存帧

    def test_start_spawns_worker_threads_and_stop_joins(self, monkeypatch):
        """start():置位 _running 并起 buffer/infer-scheduler/worker 三线程。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        closed = MagicMock()
        closed.isOpened.return_value = False
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: closed)
        s = _stream()
        s.start()
        try:
            assert s._running is True
            assert s._thread is not None and s._thread.is_alive()
            assert s._buffer_thread is not None and s._buffer_thread.is_alive()
            assert s._infer_scheduler_thread is not None
        finally:
            s.stop()
        assert s._running is False
        assert s._thread is None

    def test_stop_clears_keepalive_placeholder_state(self):
        """stop() 后 get_state 报 last_error=摄像头已停止。"""
        s = _stream()
        s._mark_camera_closed("x", keep_cache=True)
        s.stop()
        assert s.get_state()["last_error"] == "摄像头已停止"


class TestFrameAccessors:
    def test_get_jpeg_returns_cached_bytes(self):
        s = _stream()
        with s._lock:
            s._latest_jpeg = b"jpeg-bytes"
        assert s.get_jpeg() == b"jpeg-bytes"

    def test_get_latest_frame_returns_copy(self):
        s = _stream()
        assert s.get_latest_frame() is None  # 无帧 → None
        f = _frame(7)
        with s._lock:
            s._latest_frame = f
        got = s.get_latest_frame()
        assert got is not f
        got[:] = 0  # 改副本不影响内部帧
        assert f[0, 0, 0] == 7

    def test_get_recent_frames_returns_copies_with_count(self):
        s = _stream()
        assert s.get_recent_frames() == []  # 空缓冲
        f1, f2, f3 = _frame(1), _frame(2), _frame(3)
        with s._lock:
            s._frame_buffer.extend([f1, f2, f3])
        all_frames = s.get_recent_frames()
        assert len(all_frames) == 3 and all_frames[0] is not f1
        last2 = s.get_recent_frames(2)
        assert len(last2) == 2
        assert last2[0][0, 0, 0] == 2 and last2[1][0, 0, 0] == 3
        last2[1][:] = 9  # 拷贝语义
        assert f3[0, 0, 0] == 3


class TestBufferWorker:
    def test_buffer_worker_moves_queue_frames_into_ring_buffer(self):
        """_buffer_worker:取队列帧 → 拷贝进 _frame_buffer 并打时间戳。"""
        s = _stream()
        s._running = True
        t = threading.Thread(target=s._buffer_worker, daemon=True)
        t.start()
        try:
            s._buffer_queue.put(_frame(5))
            deadline = time.time() + 2
            while not s._frame_buffer and time.time() < deadline:
                time.sleep(0.01)
            assert len(s._frame_buffer) == 1
            assert s._frame_buffer[0][0, 0, 0] == 5
            assert len(s._frame_timestamps) == 1
        finally:
            s._running = False
            t.join(timeout=2)
        assert not t.is_alive()


class TestInferSchedulerWorker:
    def test_no_loop_falls_back_to_thread_executor(self):
        """未注入 loop → 回退线程池跑 _run_inference。"""
        s = _stream()
        s._running = True
        seen: list = []
        monkey_target = lambda frame: seen.append(frame)  # noqa: E731
        s._run_inference = monkey_target  # 实例属性遮蔽，executor 会调到它
        t = threading.Thread(target=s._infer_scheduler_worker, daemon=True)
        t.start()
        try:
            s._infer_queue.put((_frame(3), "motion"))
            deadline = time.time() + 2
            while not seen and time.time() < deadline:
                time.sleep(0.01)
            assert len(seen) == 1
        finally:
            s._running = False
            t.join(timeout=2)

    @pytest.mark.asyncio
    async def test_with_loop_schedules_inference_on_loop(self):
        """注入 loop → run_coroutine_threadsafe 投递，future 完成后自动摘除。"""
        s = _stream()
        s._running = True
        s.set_event_loop(asyncio.get_running_loop())
        seen: list = []

        async def fake_async(frame):
            seen.append(frame)

        s._run_inference_async = fake_async
        t = threading.Thread(target=s._infer_scheduler_worker, daemon=True)
        t.start()
        try:
            s._infer_queue.put((_frame(4), "heartbeat"))
            deadline = time.time() + 2
            while (not seen or s._infer_futures) and time.time() < deadline:
                await asyncio.sleep(0.01)
            assert len(seen) == 1
            assert s._infer_futures == []  # done_callback 已移除
        finally:
            s._running = False
            t.join(timeout=2)
            await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_scheduler_exception_resets_infer_busy(self, caplog):
        """调度异常 → 记日志并把 _infer_busy 复位，避免推理永久卡死。"""
        s = _stream()
        s._running = True
        s._infer_busy = True
        s.set_event_loop(asyncio.get_running_loop())

        def boom(coro, loop):
            coro.close()  # 防止 "coroutine never awaited" 告警
            raise RuntimeError("schedule failed")

        monkey = getattr(cam.asyncio, "run_coroutine_threadsafe")
        cam.asyncio.run_coroutine_threadsafe = boom
        try:
            t = threading.Thread(target=s._infer_scheduler_worker, daemon=True)
            t.start()
            s._infer_queue.put((_frame(5), "motion"))
            deadline = time.time() + 2
            while s._infer_busy and time.time() < deadline:
                await asyncio.sleep(0.01)
            assert s._infer_busy is False
            assert "Inference scheduler error" in caplog.text
        finally:
            cam.asyncio.run_coroutine_threadsafe = monkey
            s._running = False
            t.join(timeout=2)


class TestSettersAndHoldSeconds:
    def test_set_motion_threshold_updates_detector(self):
        s = _stream()
        s.set_motion_threshold(40)
        assert s._motion.threshold == 40

    def test_set_discovery_service_injects(self):
        s = _stream()
        svc = object()
        s.set_discovery_service(svc)
        assert s._discovery_service is svc

    def test_offline_hold_seconds_default(self):
        """未配置 → 默认 10 秒。"""
        assert CameraStream._offline_hold_seconds() == 10.0

    def test_offline_hold_seconds_negative_falls_back(self, monkeypatch):
        """配置为负数 → 回退默认值。"""
        monkeypatch.setattr("app.core.config.get_config", lambda *a, **k: -3.0)
        assert CameraStream._offline_hold_seconds() == 10.0

    def test_offline_hold_seconds_config_error_falls_back(self, monkeypatch):
        """配置读取抛异常 → 回退默认值。"""
        def boom(*a, **k):
            raise RuntimeError("cfg")
        monkeypatch.setattr("app.core.config.get_config", boom)
        assert CameraStream._offline_hold_seconds() == 10.0


class TestMjpegGenerator:
    def _online_stream(self) -> CameraStream:
        s = _stream()
        s._running = True
        s._state.camera_opened = True
        return s

    def test_sends_new_frame_each_time_it_changes(self):
        s = self._online_stream()
        with s._lock:
            s._latest_jpeg = b"AAA"
        gen = s.mjpeg_generator()
        first = next(gen)
        assert b"AAA" in first and first.startswith(b"--frame")

        def swap():
            time.sleep(0.12)
            with s._lock:
                s._latest_jpeg = b"BBB"

        th = threading.Thread(target=swap)
        th.start()
        second = next(gen)  # 中间若干轮帧未变化不重复发送，直到变化
        th.join()
        gen.close()
        assert b"BBB" in second and b"AAA" not in second

    def test_no_frame_sends_keepalive_after_100_idle_loops(self, monkeypatch):
        """无帧 100 轮 → 推 1x1 JPEG keepalive,计数器复位。"""
        monkeypatch.setattr(cam.time, "sleep", lambda *_: None)
        s = self._online_stream()
        with s._lock:
            s._latest_jpeg = None
        gen = s.mjpeg_generator()
        out1 = next(gen)   # 单次 next 内部空转 100 轮后产出 keepalive
        out2 = next(gen)   # 恢复执行:计数器复位后再空转 100 轮
        gen.close()
        keepalive = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + _TINY_JPEG + b"\r\n"
        assert out1 == keepalive
        assert out2 == keepalive

    def test_offline_grace_expiry_continues_without_repeating_placeholder(self):
        """离线推过占位图后不再重复推（下轮直接 continue），状态恢复后发新帧。"""
        s = _stream({"vision.offline_hold_seconds": 0})
        s._running = True
        s._state.camera_opened = False
        with s._lock:
            s._latest_jpeg = b"STALE"
        s._offline_hold_seconds = lambda: 0.0
        gen = s.mjpeg_generator()
        first = next(gen)
        assert _OFFLINE_JPEG in first

        def recover():
            time.sleep(0.3)
            with s._lock:
                s._state.camera_opened = True
                s._latest_jpeg = b"LIVE"

        th = threading.Thread(target=recover)
        th.start()
        second = next(gen)  # 占位图不重复推；恢复在线后发 LIVE
        th.join()
        gen.close()
        assert b"LIVE" in second and _OFFLINE_JPEG not in second


class TestBlacklistAndCandidates:
    def test_blacklist_ignores_empty_and_unavailable(self):
        s = _stream()
        s._blacklist_backend("")
        s._blacklist_backend("unavailable")
        assert s._backend_blacklist == {}

    def test_blacklist_adds_entry_with_ttl(self):
        s = _stream()
        before = time.time()
        s._blacklist_backend("dshow:0")
        exp = s._backend_blacklist["dshow:0"]
        assert exp - before == pytest.approx(s._backend_blacklist_ttl, abs=5)

    def test_build_candidates_full_order(self):
        s = _stream()
        cands = s._build_candidates()
        assert [c[0] for c in cands] == ["dshow:0", "dshow:1", "msmf:0", "msmf:1"]

    def test_build_candidates_skips_blacklisted(self):
        s = _stream()
        s._backend_blacklist["dshow:0"] = time.time() + 30
        cands = s._build_candidates()
        assert "dshow:0" not in [c[0] for c in cands]

    def test_build_candidates_purges_expired_entries(self):
        """过期黑名单项被清理,backend 重新可用。"""
        s = _stream()
        s._backend_blacklist = {"msmf:1": time.time() - 1}
        cands = s._build_candidates()
        assert [c[0] for c in cands] == ["dshow:0", "dshow:1", "msmf:0", "msmf:1"]
        assert s._backend_blacklist == {}  # 过期项已清理

    def test_build_candidates_all_blacklisted_falls_back_to_full(self):
        """全部 backend 都在黑名单内(TTL 未过期) → 忽略黑名单,保证有候选。"""
        s = _stream()
        s._backend_blacklist = {name: time.time() + 60
                                for name in ("dshow:0", "dshow:1", "msmf:0", "msmf:1")}
        cands = s._build_candidates()
        assert [c[0] for c in cands] == ["dshow:0", "dshow:1", "msmf:0", "msmf:1"]

    def test_build_candidates_last_success_first(self):
        s = _stream()
        s._last_success_backend = ("msmf:0", 0, cv2.CAP_MSMF)
        cands = s._build_candidates()
        assert cands[0] == ("msmf:0", 0, cv2.CAP_MSMF)
        assert [c[0] for c in cands[1:]] == ["dshow:0", "dshow:1", "msmf:1"]


class TestOpenCameraUsb:
    def _patch_cvcap(self, monkeypatch, behavior):
        """替换 cam.cv2.VideoCapture。behavior(index, backend, cap) 定制每个 cap。"""
        made: list[tuple[int, int, MagicMock]] = []

        def factory(index=0, backend=None):
            cap = MagicMock()
            behavior(index, backend, cap)
            made.append((index, backend, cap))
            return cap

        monkeypatch.setattr(cam.cv2, "VideoCapture", factory)
        return made

    def test_open_success_first_candidate(self, monkeypatch):
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        f = _frame()
        self._patch_cvcap(
            monkeypatch,
            lambda i, b, cap: (setattr(cap, "isOpened", MagicMock(return_value=True)),
                               setattr(cap, "read", MagicMock(return_value=(True, f)))),
        )
        s = _stream()
        cap = s._open_camera()
        assert cap.isOpened() is True
        assert s._last_success_backend == ("dshow:0", 0, cv2.CAP_DSHOW)
        assert s._camera_index == 0
        assert s._state.backend_name == "dshow:0"
        prop_calls = [c[0][0] for c in cap.set.call_args_list]
        assert prop_calls == [
            cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT,
            cv2.CAP_PROP_FPS, cv2.CAP_PROP_BUFFERSIZE,
        ]
        assert ft.sleeps == [0.3]  # 预热前固定 sleep

    def test_warmup_slow_read_blacklists_and_moves_on(self, monkeypatch):
        """预热读到帧但超慢 → 拉黑该 backend，切下一个候选成功。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        s = _stream()
        s._slow_read_ms = -1.0  # 任何探测都算慢
        f = _frame()
        caps: list[MagicMock] = []

        def behavior(i, b, cap):
            caps.append(cap)
            cap.isOpened.return_value = True
            cap.read.return_value = (True, f)
            if i != 0:  # 第二个候选:在 set 阶段恢复阈值,让自己的探测判为快
                cap.set.side_effect = lambda *a, **k: setattr(s, "_slow_read_ms", 10 ** 9)

        self._patch_cvcap(monkeypatch, behavior)
        cap = s._open_camera()
        assert cap is caps[1]
        assert "dshow:0" in s._backend_blacklist
        assert s._last_success_backend == ("dshow:1", 1, cv2.CAP_DSHOW)
        caps[0].release.assert_called_once()

    def test_rtsp_config_routes_to_network_stream(self, monkeypatch):
        """配置了 rtsp_url → _open_camera 走网络流路径。"""
        s = _stream({"source_type": "rtsp", "rtsp_url": "rtsp://1.2.3.4/live"})
        sentinel = MagicMock(name="network_cap")
        monkeypatch.setattr(s, "_open_network_stream", MagicMock(return_value=sentinel))
        assert s._open_camera() is sentinel

    def test_all_backends_fail_to_open(self, monkeypatch):
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        made = self._patch_cvcap(
            monkeypatch, lambda i, b, cap: setattr(cap, "isOpened", MagicMock(return_value=False)),
        )
        s = _stream()
        cap = s._open_camera()
        assert len(made) == 5  # 四个候选全试过 + 末尾的空 cap 兜底
        assert all(c.isOpened() is False for _, _, c in made[:4])
        assert cap is made[4][2]
        assert s._state.backend_name == "unavailable"
        assert cap.isOpened() is False  # 返回空 cap 兜底

    def test_backend_never_produces_frame(self, monkeypatch):
        """打开成功但 12 次预热都读不到帧 → 换下一个，最终 unavailable。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        made = self._patch_cvcap(monkeypatch, lambda i, b, cap: (
            setattr(cap, "isOpened", MagicMock(return_value=True)),
            setattr(cap, "read", MagicMock(return_value=(False, None))),
        ))
        s = _stream()
        cap = s._open_camera()
        assert len(made) == 5  # 四个候选全试过 + 末尾的空 cap 兜底
        for _, _, c in made[:4]:
            assert c.read.call_count == 12
            c.release.assert_called_once()
        assert s._state.backend_name == "unavailable"
        assert cap is made[4][2]


class TestRtspUrlHelpers:
    def test_sanitize_url_masks_password(self):
        url = "rtsp://admin:secret@1.2.3.4:554/live"
        assert CameraStream._sanitize_url(url) == "rtsp://admin:***@1.2.3.4:554/live"

    def test_sanitize_url_without_credentials_unchanged(self):
        assert CameraStream._sanitize_url("rtsp://1.2.3.4/live") == "rtsp://1.2.3.4/live"

    def test_resolve_empty_rtsp_returns_empty(self):
        s = _stream({"rtsp_url": ""})
        assert s._resolve_rtsp_url() == ""

    def test_resolve_url_without_scheme_returned_as_is(self):
        s = _stream({"rtsp_url": "192.168.1.5:554/live",
                     "rtsp_username": "u", "rtsp_password": "p"})
        assert s._resolve_rtsp_url() == "192.168.1.5:554/live"

    def test_resolve_url_without_credentials_bare_connect(self):
        """只配了用户名没配密码 → 裸连 URL 原样返回。"""
        s = _stream({"rtsp_url": "rtsp://1.2.3.4/live",
                     "rtsp_username": "u", "rtsp_password": ""})
        assert s._resolve_rtsp_url() == "rtsp://1.2.3.4/live"

    def test_resolve_url_percent_encodes_special_chars(self):
        s = _stream({"rtsp_url": "rtsp://1.2.3.4/live",
                     "rtsp_username": "a@b", "rtsp_password": "p/x"})
        assert s._resolve_rtsp_url() == "rtsp://a%40b:p%2Fx@1.2.3.4/live"


class TestOpenNetworkStream:
    def _rtsp_stream(self) -> CameraStream:
        return _stream({"source_type": "rtsp", "rtsp_url": "rtsp://1.2.3.4/live",
                        "rtsp_username": "u", "rtsp_password": "p"})

    def _patch_cvcap(self, monkeypatch, behavior):
        made: list[MagicMock] = []

        def factory(*a, **k):
            cap = MagicMock()
            behavior(cap)
            made.append(cap)
            return cap

        monkeypatch.setattr(cam.cv2, "VideoCapture", factory)
        return made

    def test_open_success_sets_rtsp_backend_and_options(self, monkeypatch):
        monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "preset")
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        f = _frame()
        made = self._patch_cvcap(monkeypatch, lambda cap: (
            setattr(cap, "isOpened", MagicMock(return_value=True)),
            setattr(cap, "read", MagicMock(return_value=(True, f))),
        ))
        s = self._rtsp_stream()
        cap = s._open_network_stream("rtsp://u:p@1.2.3.4/live")
        assert cap is made[0]
        made[0].set.assert_called_once_with(cv2.CAP_PROP_BUFFERSIZE, 1)
        assert s._state.backend_name == "rtsp"
        opts = os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]
        assert "rtsp_transport;tcp" in opts and "timeout;5000000" in opts

    def test_open_failure_returns_dummy(self, monkeypatch):
        monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "preset")
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        made = self._patch_cvcap(monkeypatch, lambda cap: setattr(
            cap, "isOpened", MagicMock(return_value=False)))
        s = self._rtsp_stream()
        cap = s._open_network_stream("rtsp://u:p@1.2.3.4/live")
        made[0].release.assert_called_once()
        assert s._state.backend_name == "unavailable"
        assert cap.isOpened() is False

    def test_open_but_no_frame_releases(self, monkeypatch):
        """15 次预热都读不到帧 → release + unavailable。"""
        monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "preset")
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        made = self._patch_cvcap(monkeypatch, lambda cap: (
            setattr(cap, "isOpened", MagicMock(return_value=True)),
            setattr(cap, "read", MagicMock(return_value=(False, None))),
        ))
        s = self._rtsp_stream()
        cap = s._open_network_stream("rtsp://1.2.3.4/live")
        assert made[0].read.call_count == 15
        made[0].release.assert_called_once()
        assert s._state.backend_name == "unavailable"
        assert len(made) == 2  # 真实 cap + 末尾的空 cap 兜底
        assert cap is made[1]


class TestWorkerLoop:
    def test_happy_path_processes_frames_and_resets_failure_state(self, monkeypatch):
        """开流成功 → 连续失败计数清零、ever_opened 置位、帧送 _process_frame。"""
        s = _stream()
        f = _frame()
        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = True
        fake_cap.read.return_value = (True, f)
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: fake_cap)
        processed: list = []

        def pf(frame):
            processed.append(frame)
            if len(processed) >= 2:
                s._running = False

        s._process_frame = pf
        s._consecutive_open_failures = 2  # 预置失败,成功后应清零
        s._running = True
        s._worker()
        assert len(processed) == 2 and processed[0] is f
        assert s._consecutive_open_failures == 0
        assert s._ever_opened is True

    def test_open_failure_backoff_and_state(self, monkeypatch):
        """开流失败 → 指数退避 sleep、报错且保留缓存帧。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        closed = MagicMock()
        closed.isOpened.return_value = False
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: closed)
        s = _stream()
        with s._lock:
            s._latest_jpeg = b"CACHED"

        def stop_after_sleep(sec):
            ft.sleeps.append(sec)
            s._running = False

        ft.sleep = stop_after_sleep
        s._running = True
        s._worker()
        assert s._consecutive_open_failures == 1
        assert ft.sleeps == [s._release_cooldown]
        assert s._state.last_error == "无法打开电脑摄像头"
        assert s._state.camera_opened is False
        assert s.get_jpeg() == b"CACHED"  # keep_cache=True

    def test_cold_open_backoff_tiers(self, monkeypatch):
        """从未成功开流:失败 5/15/30 次分档拉长退避 60s/300s/900s。"""
        closed = MagicMock()
        closed.isOpened.return_value = False
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: closed)
        original_time = cam.time
        try:
            for preset in (4, 14, 29):
                ft = FakeTime()
                cam.time = ft
                s = _stream()
                s._consecutive_open_failures = preset

                def stop_after_sleep(sec, _s=s, _ft=ft):
                    _ft.sleeps.append(sec)
                    _s._running = False

                ft.sleep = stop_after_sleep
                s._running = True
                s._worker()
                if preset == 4:
                    assert ft.sleeps[-1] == s._cold_open_backoff
                elif preset == 14:
                    assert ft.sleeps[-1] == min(max(s._cold_open_backoff, 300.0),
                                                s._cold_open_max_backoff)
                else:
                    assert ft.sleeps[-1] == s._cold_open_max_backoff
        finally:
            cam.time = original_time

    def test_worker_fps_logging(self, monkeypatch):
        """每 10 秒记一次 worker FPS → _state.worker_fps 更新。"""
        ft = FakeTime(step=5.0)  # 每次 time() 推 5s,一轮循环即超 10s
        monkeypatch.setattr(cam, "time", ft)
        f = _frame()
        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = True
        fake_cap.read.return_value = (True, f)
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: fake_cap)
        s = _stream()
        s._slow_read_ms = 10 ** 9  # 关闭慢读检测,避免干扰
        processed: list = []

        def pf(frame):
            processed.append(frame)
            if len(processed) >= 2:
                s._running = False

        s._process_frame = pf
        s._running = True
        s._worker()
        assert len(processed) == 2
        assert s._state.worker_fps > 0

    def test_slow_read_reopens_and_blacklists_backend(self, monkeypatch):
        """连续慢读达阈值 → 拉黑 backend、释放设备、重连。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        s = _stream()
        s._slow_read_ms = -1.0  # 每次 read 都算慢
        s._slow_read_threshold = 2
        s._read_retry_count = 0
        s._state.backend_name = "dshow:0"
        f = _frame()
        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = True

        def read(*a):  # 前 2 次慢帧(凑满 streak),重连后读不到帧走失败分支收尾
            read.n += 1
            return (True, f) if read.n <= 2 else (False, None)
        read.n = 0
        fake_cap.read.side_effect = read
        opens = {"n": 0}

        def fake_open(self):
            opens["n"] += 1
            if opens["n"] >= 2:
                s._running = False
            return fake_cap

        monkeypatch.setattr(CameraStream, "_open_camera", fake_open)
        s._running = True
        s._worker()
        assert "dshow:0" in s._backend_blacklist
        # 慢读重连分支 release 一次 + 重连后读帧失败分支 release 一次
        assert fake_cap.release.call_count == 2
        assert s._release_cooldown in ft.sleeps  # 重连前冷却
        assert opens["n"] == 2

    def test_read_failure_retries_then_releases(self, monkeypatch):
        """读帧失败 → 就地重试 N 次仍失败才释放设备(保留缓存帧)。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        s = _stream()
        s._read_retry_count = 2
        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = True
        fake_cap.read.return_value = (False, None)
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: fake_cap)

        def stop_after_sleep(sec):
            ft.sleeps.append(sec)
            if sec == s._release_cooldown:
                s._running = False

        ft.sleep = stop_after_sleep
        with s._lock:
            s._latest_jpeg = b"KEEP"
        s._running = True
        s._worker()
        assert fake_cap.read.call_count == 3  # 首读 + 2 次重试
        fake_cap.release.assert_called_once()
        assert ft.sleeps.count(s._read_retry_interval) == 2
        assert s._state.last_error == "读取摄像头画面失败"
        assert s.get_jpeg() == b"KEEP"  # keep_cache=True

    def test_read_recovered_after_retry(self, monkeypatch):
        """瞬时掉帧:重试一次成功 → 恢复后的帧继续送 _process_frame。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        s = _stream()
        s._read_retry_count = 2
        f1, f2 = _frame(1), _frame(2)
        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = True
        fake_cap.read.side_effect = [(False, None), (True, f1), (True, f2)]
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: fake_cap)
        processed: list = []

        def pf(frame):
            processed.append(frame)
            if len(processed) >= 2:
                s._running = False

        s._process_frame = pf
        s._running = True
        s._worker()
        assert processed == [f1, f2]  # 掉帧后恢复,用的是重试拿到的帧

    def test_read_retry_breaks_when_stopping(self, monkeypatch):
        """stop 进行中:就地重试循环检测到 _running=False 立即中止。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        s = _stream()
        s._read_retry_count = 3
        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = True

        def read(*a):
            s._running = False  # 首读失败同时触发停止
            return (False, None)

        fake_cap.read.side_effect = read
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: fake_cap)
        s._running = True
        s._worker()
        assert fake_cap.read.call_count == 1  # 未进入任何一次重试
        assert ft.sleeps == [s._release_cooldown]  # 只有释放冷却,没有重试间隔
        fake_cap.release.assert_called_once()  # 落入"重试失败"收尾分支

    def test_worker_exception_clears_cache_and_continues(self, monkeypatch):
        """处理帧抛异常 → 清缓存报错,线程不崩,下一轮恢复。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        s = _stream()
        f = _frame()
        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = True
        fake_cap.read.return_value = (True, f)
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: fake_cap)
        calls = {"n": 0}

        def pf(frame):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            s._running = False

        s._process_frame = pf
        with s._lock:
            s._latest_jpeg = b"OLD"
        s._running = True
        s._worker()
        assert calls["n"] == 2
        assert s._state.last_error == "摄像头线程异常"
        assert s.get_jpeg() is None  # 非 keep_cache,缓存被清

    def test_worker_discovery_failure_is_contained(self, monkeypatch, caplog):
        """discovery 投递失败只记日志,worker 继续退避循环。"""
        ft = FakeTime()
        monkeypatch.setattr(cam, "time", ft)
        closed = MagicMock()
        closed.isOpened.return_value = False
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self: closed)
        discovery = MagicMock()
        discovery.find_and_apply = AsyncMock(return_value=None)
        s = _stream({"discovery_enabled": 1}, )
        s._discovery_service = discovery
        s.set_event_loop(asyncio.new_event_loop())
        s._discovery_trigger_threshold = 1

        def boom(coro, loop):
            raise RuntimeError("loop gone")

        monkeypatch.setattr(cam.asyncio, "run_coroutine_threadsafe", boom)

        def stop_after_sleep(sec):
            ft.sleeps.append(sec)
            s._running = False

        ft.sleep = stop_after_sleep
        s._running = True
        s._worker()
        assert s._open_fail_count == 0  # 触发后已清零
        assert "ONVIF discovery triggered from worker failed" in caplog.text
        assert s._state.last_error == "正在重新发现摄像头…"


class TestReadSourceFps:
    def test_fps_from_cap(self):
        s = _stream()
        s._cap = MagicMock()
        s._cap.get.return_value = 25.0
        assert s._read_source_fps() == 25.0

    def test_fps_zero_when_cap_reports_zero(self):
        s = _stream()
        s._cap = MagicMock()
        s._cap.get.return_value = 0.0
        assert s._read_source_fps() == 0.0

    def test_fps_zero_when_cap_get_raises(self):
        s = _stream()
        s._cap = MagicMock()
        s._cap.get.side_effect = RuntimeError("x")
        assert s._read_source_fps() == 0.0

    def test_fps_zero_without_cap(self):
        s = _stream()
        s._cap = None
        assert s._read_source_fps() == 0.0


class TestProcessFrame:
    def test_process_frame_updates_state_jpeg_and_buffer(self):
        s = _stream()  # frame_interval_ms=0 → 每帧都进缓冲
        f = _frame(120)
        s._viewers = 1  # 有观众才编码（按需编码契约）
        s._process_frame(f)
        st = s.get_state()
        assert st["camera_opened"] is True
        assert st["frame_width"] == 64 and st["frame_height"] == 48
        assert st["last_error"] is None
        assert st["fps"] == 0.0  # 虚拟/无 cap → 0
        jpeg = s.get_jpeg()
        assert jpeg is not None and jpeg[:2] == b"\xff\xd8"
        assert np.array_equal(s.get_latest_frame(), f)  # get_latest_frame 返回拷贝
        assert s.get_latest_frame() is not f
        assert s._buffer_queue.qsize() == 1  # 入队给环形缓冲线程

    def test_process_frame_encode_failure_keeps_old_cache(self, monkeypatch):
        """JPEG 编码失败 → 记日志直接返回,不覆盖旧缓存。"""
        monkeypatch.setattr(cam.cv2, "imencode", lambda *a, **k: (False, None))
        s = _stream()
        s._viewers = 1  # 无观众时根本不进编码分支，本测试需要观众
        with s._lock:
            s._latest_jpeg = b"PREVIOUS"
        s._process_frame(_frame())
        assert s.get_jpeg() == b"PREVIOUS"
        assert s.get_state()["camera_opened"] is False

    def test_process_frame_no_viewer_skips_encoding(self, monkeypatch):
        """无观众 → 跳过亮度准备+JPEG 编码；状态/原始帧/缓冲照常更新。"""
        encode_calls = []
        real_imencode = cam.cv2.imencode

        def spy(*a, **k):
            encode_calls.append(True)
            return real_imencode(*a, **k)

        monkeypatch.setattr(cam.cv2, "imencode", spy)
        s = _stream()
        f = _frame(120)
        s._process_frame(f)
        assert encode_calls == []  # 编码被跳过
        assert s.get_jpeg() is None  # _latest_jpeg 不产出
        st = s.get_state()
        assert st["camera_opened"] is True  # 状态照常
        assert np.array_equal(s.get_latest_frame(), f)  # 原始帧照常供视觉用
        assert s._buffer_queue.qsize() == 1  # 环形缓冲照常

    def test_mjpeg_generator_tracks_viewers(self):
        """观众计数：开始迭代 +1，关闭生成器归位——驱动按需编码。"""
        s = _stream()
        s._running = True
        with s._lock:
            s._latest_jpeg = b"JPEG1"
            s._state.camera_opened = True
        gen = s.mjpeg_generator()
        assert s._viewers == 0  # 未迭代不算观众
        next(gen)
        assert s._viewers == 1
        gen.close()
        assert s._viewers == 0

    def test_process_frame_buffer_queue_full_drops_frame(self):
        """环形缓冲队满 → 丢帧不阻塞。"""
        s = _stream()
        for i in range(10):
            s._buffer_queue.put(i)
        s._process_frame(_frame())  # 不应抛 queue.Full
        assert s._buffer_queue.qsize() == 10

    def test_process_frame_respects_buffer_interval(self):
        """frame_interval_ms 间隔内的帧不入缓冲。"""
        s = _stream({"frame_interval_ms": 3600_000})
        s._frame_timestamps.append(time.time())  # 预置时间戳(缓冲线程未跑)
        s._process_frame(_frame(1))
        assert s._buffer_queue.qsize() == 1
        s._process_frame(_frame(2))  # 间隔未到
        assert s._buffer_queue.qsize() == 1


class TestMaybeScheduleInference:
    def _sched_stream(self, moved=(True, 50)):
        s = _stream({"vision_min_infer_interval": 0.5, "vision_max_idle_interval": 100.0})
        s._camera_vl_display_enabled = True
        motion = MagicMock()
        motion.assess.return_value = moved
        motion.threshold = 15
        motion.commit_reference = MagicMock()
        s._motion = motion
        s._last_motion_check = 0.0
        return s, motion

    def test_motion_check_interval_gate(self):
        """间隔内不重复做 dhash 评估。"""
        s, motion = self._sched_stream()
        s._maybe_schedule_inference(_frame())
        s._maybe_schedule_inference(_frame())  # 紧跟第二帧,间隔未到
        assert motion.assess.call_count == 1

    def test_motion_distance_and_callback(self):
        """运动距离刷进状态;moved 即触发自动化回调(带 camera_id)。"""
        s, _ = self._sched_stream((True, 33))
        got: list[str] = []
        s.set_on_automation_trigger(lambda cid: got.append(cid))
        s._maybe_schedule_inference(_frame())
        assert s.get_state()["motion_distance"] == 33
        assert got == ["cam_cov"]

    def test_callback_exception_does_not_propagate(self):
        s, _ = self._sched_stream()
        s._camera_vl_display_enabled = False  # 关展示,隔离调度副作用

        def boom(cid):
            raise RuntimeError("cb")

        s.set_on_automation_trigger(boom)
        s._maybe_schedule_inference(_frame())  # 不应抛
        assert s._infer_queue.empty()

    def test_display_disabled_stops_preview_inference(self):
        """展示关 → dhash/回调照常,但不调度预览推理。"""
        s, _ = self._sched_stream()
        s._camera_vl_display_enabled = False
        got: list[str] = []
        s.set_on_automation_trigger(lambda cid: got.append(cid))
        s._maybe_schedule_inference(_frame())
        assert got == ["cam_cov"]
        assert s._infer_queue.empty()
        assert s._infer_busy is False

    def test_recognizer_disabled_skips_inference(self):
        s, _ = self._sched_stream()
        s._recognizer = MagicMock(enabled=False)
        s._maybe_schedule_inference(_frame())
        assert s._infer_queue.empty()

    def test_busy_guard_skips_and_timeout_resets(self):
        """推理中跳过;超时后强制复位并重新调度。"""
        s, motion = self._sched_stream()
        s._infer_timeout = 45.0
        s._infer_busy = True
        s._infer_started_at = time.time() - 1  # 未超时
        s._maybe_schedule_inference(_frame())
        assert s._infer_queue.empty()
        assert s._infer_busy is True

        s._infer_started_at = time.time() - 100  # 超时
        s._last_motion_check = 0.0  # 重置运动检查闸,允许本次评估
        s._maybe_schedule_inference(_frame())
        assert s._infer_busy is True  # 重新调度后再次置位
        assert s._infer_queue.qsize() == 1
        motion.commit_reference.assert_called_once()
        frame, trigger = s._infer_queue.get_nowait()
        assert trigger == "motion"

    def test_interactive_priority_blocks_scheduling(self, monkeypatch):
        """用户交互期间让位,不调度推理。"""
        s, _ = self._sched_stream()
        monkeypatch.setattr(cam.interactive_priority, "active", lambda: True)
        s._maybe_schedule_inference(_frame())
        assert s._infer_queue.empty()
        assert s._infer_busy is False

    def test_heartbeat_trigger_when_idle_too_long(self):
        """无运动但太久没推理 → heartbeat 兜底。"""
        s, _ = self._sched_stream((False, 0))
        s._max_idle_interval = 1.0
        s._last_model_run_at = time.time() - 500
        s._maybe_schedule_inference(_frame())
        _, trigger = s._infer_queue.get_nowait()
        assert trigger == "heartbeat"

    def test_no_trigger_when_quiet_and_recent(self):
        """无运动且未到心跳间隔 → 不调度。"""
        s, _ = self._sched_stream((False, 0))
        s._last_model_run_at = time.time()
        s._maybe_schedule_inference(_frame())
        assert s._infer_queue.empty()
        assert s._infer_busy is False

    def test_queue_full_resets_busy(self):
        """推理队满 → 丢帧并复位 _infer_busy(否则永久卡死)。"""
        s, _ = self._sched_stream()
        for i in range(5):
            s._infer_queue.put((object(), "x"))
        s._maybe_schedule_inference(_frame())
        assert s._infer_busy is False
        assert s._infer_queue.qsize() == 5


class TestRunInference:
    def test_success_updates_latest_result(self):
        s = _stream()
        s._recognizer = MagicMock()
        s._recognizer.classify_frame.return_value = ActionResult("person", "有人", {"o": 1})
        s._infer_busy = True
        f = _frame()
        s._run_inference(f)
        s._recognizer.classify_frame.assert_called_once_with(f, camera_id="cam_cov")
        assert s._latest_result.action == "person"
        assert s._infer_busy is False
        assert s._infer_started_at == 0.0

    def test_failure_sets_error_result(self):
        s = _stream()
        s._recognizer = MagicMock()
        s._recognizer.classify_frame.side_effect = RuntimeError("boom")
        s._infer_busy = True
        s._run_inference(_frame())
        assert s._latest_result.action == "idle"
        assert "模型识别失败" in s._latest_result.feedback
        assert "boom" in s._latest_result.feedback
        assert s._infer_busy is False

    @pytest.mark.asyncio
    async def test_async_success_records_vision_log(self):
        s = _stream()
        s._recognizer = MagicMock()
        s._recognizer.classify_frame_async = AsyncMock(
            return_value=ActionResult("wave", "挥手", {"observation": "hi"}))
        s._record_vision_log = AsyncMock()
        s._infer_busy = True
        await s._run_inference_async(_frame())
        s._recognizer.classify_frame_async.assert_awaited_once()
        assert s._latest_result.action == "wave"
        s._record_vision_log.assert_awaited_once()
        kind, content = s._record_vision_log.await_args.args
        assert kind == "preview" and content["event"] == "wave"
        assert s._infer_busy is False

    @pytest.mark.asyncio
    async def test_async_failure_sets_error_result(self):
        s = _stream()
        s._recognizer = MagicMock()
        s._recognizer.classify_frame_async = AsyncMock(side_effect=RuntimeError("net"))
        s._record_vision_log = AsyncMock()
        await s._run_inference_async(_frame())
        assert s._latest_result.action == "idle"
        assert "net" in s._latest_result.feedback
        s._record_vision_log.assert_not_awaited()
        assert s._infer_busy is False

    @pytest.mark.asyncio
    async def test_record_vision_log_inserts(self, monkeypatch):
        s = _stream()
        db = MagicMock()
        db.vision_log_insert = AsyncMock()
        monkeypatch.setattr("app.core.database.Database.get", classmethod(lambda cls: db))
        await s._record_vision_log("preview", {"event": "x"})
        db.vision_log_insert.assert_awaited_once_with("cam_cov", "preview", {"event": "x"})

    @pytest.mark.asyncio
    async def test_record_vision_log_swallows_errors(self, monkeypatch):
        """DB 未初始化 → 只记 debug,不影响推理。"""
        s = _stream()
        monkeypatch.setattr(
            "app.core.database.Database.get",
            classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no db"))),
        )
        await s._record_vision_log("preview", {})  # 不应抛


class TestResolveDisplayResult:
    def test_recognizer_disabled_shows_plain_message(self):
        s = _stream(vision_service=MagicMock(enabled=False))
        out = s._resolve_display_result(ActionResult("person", "有人", {"k": 1}))
        assert out.action == "idle"
        assert "视觉模型未启用" in out.feedback
        assert out.details["enabled"] is False

    def test_event_first_frames_show_waiting_confirm(self):
        s = _stream()
        s._state.motion_distance = 20
        out = s._resolve_display_result(ActionResult("person", "有人", {}))
        assert out.action == "waiting_confirm"
        assert out.details["presence_frames"] == 1

    def test_presence_confirmed_after_threshold(self):
        s = _stream()
        s._state.motion_distance = 20
        for _ in range(2):
            s._resolve_display_result(ActionResult("person", "有人", {}))
        out = s._resolve_display_result(ActionResult("person", "有人", {}))
        assert out.action == "person"
        assert out.details["presence_frames"] == 3

    def test_absence_quiet_feedback_without_motion(self):
        s = _stream()
        s._state.motion_distance = 0  # 未触发推理 → 固定文案
        out = s._resolve_display_result(ActionResult("idle", "LLM说没事", {}))
        assert out.action == "idle"
        assert out.feedback == "画面平静，等待新事件。"

    def test_absence_with_motion_shows_model_feedback(self):
        s = _stream()
        s._state.motion_distance = 20  # ≥ 阈值 → 展示 LLM observation
        out = s._resolve_display_result(ActionResult("idle", "LLM说没事", {}))
        assert out.feedback == "LLM说没事"

    def test_sustained_absence_feedback(self):
        s = _stream()
        s._state.motion_distance = 0
        for _ in range(3):
            out = s._resolve_display_result(ActionResult("idle", "x", {}))
        assert out.action == "idle"
        assert out.feedback == "画面平静，未检测到明显事件。"
        assert out.details["absence_frames"] == 3

    def test_prepare_display_frame_scales_brightness(self):
        f = _frame(100)
        out = CameraStream._prepare_display_frame(f)
        assert out.shape == f.shape
        assert out.dtype == np.uint8
        assert out[0, 0, 0] > 100  # alpha=1.15, beta=10 提亮


# ===========================================================================
# 2. app/routes/camera_routes.py
# ===========================================================================

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import camera_routes


def _routes_container():
    """mock 容器(与 test_camera_routes.py 同款,补 PTZ/流生成器)。"""
    c = MagicMock()
    c.camera_manager = MagicMock()
    c.camera_manager.create_camera = AsyncMock(return_value={
        "id": "cam_new", "name": "x", "rtsp_password": "pw", "ptz_password": ""})
    c.camera_manager.update_camera = AsyncMock(return_value={"id": "cam_new", "name": "renamed"})
    c.camera_manager.delete_camera = AsyncMock(return_value=True)
    c.camera_manager.get_state = MagicMock(return_value={
        "camera_id": "cam_a", "camera_opened": True})
    c.camera_manager.mjpeg_generator = MagicMock(return_value=iter([b"--framepart"]))
    c.camera_manager.enable_display = AsyncMock(return_value=None)
    c.camera_manager.disable_display = AsyncMock(return_value=None)
    c.ptz_registry = MagicMock()
    ptz_svc = MagicMock()
    ptz_svc.move = AsyncMock(return_value={"moved": True})
    ptz_svc.stop = AsyncMock(return_value={"stopped": True})
    ptz_svc.step = AsyncMock(return_value={"stepped": True})
    c.ptz_registry.get = AsyncMock(return_value=ptz_svc)
    c.vision_service = MagicMock()
    c.vision_service.get_vision_focuses = MagicMock(return_value=[{"id": "f1"}])
    c.vision_service.add_focus = MagicMock(return_value={"id": "f2", "text": "人"})
    c.vision_service.update_focus = MagicMock(return_value={"id": "f1", "text": "猫"})
    c.vision_service.delete_focus = MagicMock(return_value=True)
    c.vision_service.get_all_focuses_flat = MagicMock(return_value=[{"id": "f1"}])
    c.ha_service = MagicMock()
    c.discovery_service = MagicMock()
    c.discovery_service.find_and_apply = AsyncMock(return_value="1.2.3.4")
    c.discovery_service.apply_found_ip = AsyncMock(return_value=None)
    return c


@pytest.fixture
def routes_client(monkeypatch):
    cont = _routes_container()
    monkeypatch.setattr(camera_routes, "get_container", lambda: cont)
    mock_db = MagicMock()
    mock_db.cameras_get = AsyncMock(return_value={
        "id": "cam_a", "name": "客厅", "rtsp_url": "rtsp://192.168.1.50:554/s"})
    mock_db.kv_set = AsyncMock(return_value=None)
    monkeypatch.setattr(camera_routes.Database, "get", classmethod(lambda cls: mock_db))
    app = FastAPI()
    app.include_router(camera_routes.router, prefix="/api")
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # 试连预热 sleep 不真睡
    return TestClient(app), cont, mock_db


class TestCameraRoutesCrudAndMasking:
    def test_list_masks_password_fields(self, routes_client):
        """密码列剥除,换 has_* 标志(26 行 _mask_camera 循环体)。"""
        client, cont, _ = routes_client
        cont.camera_manager.cameras_all = AsyncMock(return_value=[{
            "id": "cam_a", "rtsp_password": "secret", "ptz_password": ""}])
        r = client.get("/api/cameras")
        assert r.status_code == 200
        row = r.json()["data"][0]
        assert "rtsp_password" not in row and "ptz_password" not in row
        assert row["has_rtsp_password"] is True
        assert row["has_ptz_password"] is False

    def test_create_masks_returned_row(self, routes_client):
        client, cont, _ = routes_client
        r = client.post("/api/cameras", json={"name": "新", "rtsp_password": "pw"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["has_rtsp_password"] is True and "rtsp_password" not in data

    def test_get_camera_404_when_row_missing(self, routes_client):
        """cameras_get 返回 None → 404。"""
        client, _, mock_db = routes_client
        mock_db.cameras_get = AsyncMock(return_value=None)
        r = client.get("/api/cameras/nope")
        assert r.status_code == 404
        assert "摄像头不存在" in r.json()["detail"]

    def test_get_camera_merges_state(self, routes_client):
        client, cont, _ = routes_client
        r = client.get("/api/cameras/cam_a")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["id"] == "cam_a" and data["state"]["camera_opened"] is True

    def test_update_camera_forwards_body(self, routes_client):
        client, cont, _ = routes_client
        r = client.put("/api/cameras/cam_a", json={"name": "改名"})
        assert r.status_code == 200
        cont.camera_manager.update_camera.assert_awaited_once_with("cam_a", {"name": "改名"})
        assert r.json()["data"]["name"] == "renamed"

    def test_delete_camera(self, routes_client):
        client, cont, _ = routes_client
        r = client.delete("/api/cameras/cam_a")
        assert r.status_code == 200
        assert r.json()["data"] == {"deleted": True}

    def test_video_feed_streams_mjpeg(self, routes_client):
        """MJPEG 单路端点返回 multipart 流。"""
        client, cont, _ = routes_client
        r = client.get("/api/cameras/cam_a/video_feed")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
        assert b"--framepart" in r.content
        cont.camera_manager.mjpeg_generator.assert_called_once_with("cam_a")

    def test_state_endpoint(self, routes_client):
        client, cont, _ = routes_client
        r = client.get("/api/cameras/cam_a/state")
        assert r.status_code == 200
        assert r.json()["data"]["camera_id"] == "cam_a"


class TestTestStreamEndpoint:
    def test_empty_url_rejected(self, routes_client):
        client, _, _ = routes_client
        r = client.post("/api/cameras/cam_a/test-stream", json={"rtsp_url": "  "})
        assert r.status_code == 200
        assert r.json()["data"] == {"ok": False, "error": "rtsp_url 为空"}

    def test_bad_scheme_rejected(self, routes_client):
        """file:// 等非白名单 scheme 被拦(net_guard)。"""
        client, _, _ = routes_client
        r = client.post("/api/cameras/cam_a/test-stream", json={"rtsp_url": "file:///etc/passwd"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["ok"] is False and "只允许" in data["error"]

    def test_worker_online_short_circuit(self, routes_client):
        """该路在线且 url 与库中一致 → 直接成功,不再开第二条连接。"""
        client, cont, mock_db = routes_client
        cont.camera_manager.get_state = MagicMock(return_value={"camera_opened": True})
        r = client.post("/api/cameras/cam_a/test-stream",
                        json={"rtsp_url": "rtsp://192.168.1.50:554/s"})
        assert r.json()["data"] == {"ok": True, "error": ""}

    def test_db_error_falls_through_to_probe(self, routes_client, monkeypatch):
        """读 DB 抛异常 → 视为离线,继续走试连探测。"""
        client, cont, mock_db = routes_client
        cont.camera_manager.get_state = MagicMock(return_value={"camera_opened": True})
        mock_db.cameras_get = AsyncMock(side_effect=RuntimeError("db down"))
        made = []

        def factory(*a, **k):
            cap = MagicMock()
            cap.isOpened.return_value = True
            cap.read.return_value = (True, _frame())
            made.append(cap)
            return cap
        monkeypatch.setattr(camera_routes_cv2(), "VideoCapture", factory)
        r = client.post("/api/cameras/cam_a/test-stream",
                        json={"rtsp_url": "rtsp://192.168.1.50:554/s"})
        assert r.json()["data"]["ok"] is True
        assert len(made) == 1  # 探测真的发生了

    def test_probe_success_sets_ffmpeg_options(self, routes_client, monkeypatch):
        client, _, _ = routes_client
        monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "preset")

        def factory(*a, **k):
            cap = MagicMock()
            cap.isOpened.return_value = True
            cap.read.return_value = (True, _frame())
            return cap
        monkeypatch.setattr(camera_routes_cv2(), "VideoCapture", factory)
        r = client.post("/api/cameras/cam_a/test-stream",
                        json={"rtsp_url": "rtsp://1.2.3.4/live"})
        assert r.json()["data"] == {"ok": True, "error": ""}
        assert "rtsp_transport;tcp" in os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]

    def test_probe_open_failure(self, routes_client, monkeypatch):
        client, _, _ = routes_client

        def factory(*a, **k):
            cap = MagicMock()
            cap.isOpened.return_value = False
            return cap
        monkeypatch.setattr(camera_routes_cv2(), "VideoCapture", factory)
        r = client.post("/api/cameras/cam_a/test-stream", json={"rtsp_url": "rtsp://1.2.3.4/live"})
        data = r.json()["data"]
        assert data["ok"] is False and "打不开" in data["error"]

    def test_probe_opens_but_no_frames(self, routes_client, monkeypatch):
        """打开但 10 次预热读不到帧 → 报"打开但读不到帧"。"""
        client, _, _ = routes_client

        def factory(*a, **k):
            cap = MagicMock()
            cap.isOpened.return_value = True
            cap.read.return_value = (False, None)
            return cap
        monkeypatch.setattr(camera_routes_cv2(), "VideoCapture", factory)
        r = client.post("/api/cameras/cam_a/test-stream", json={"rtsp_url": "rtsp://1.2.3.4/live"})
        data = r.json()["data"]
        assert data["ok"] is False and data["error"] == "打开但读不到帧"

    def test_probe_injects_percent_encoded_credentials(self, routes_client, monkeypatch):
        """凭证注入 + percent-encode(与 worker 的 _resolve_rtsp_url 一致)。"""
        client, _, _ = routes_client
        seen = {}

        def factory(url, *a, **k):
            seen["url"] = url
            cap = MagicMock()
            cap.isOpened.return_value = True
            cap.read.return_value = (True, _frame())
            return cap
        monkeypatch.setattr(camera_routes_cv2(), "VideoCapture", factory)
        client.post("/api/cameras/cam_a/test-stream", json={
            "rtsp_url": "rtsp://1.2.3.4/live", "rtsp_username": "a@b", "rtsp_password": "p/x"})
        assert seen["url"] == "rtsp://a%40b:p%2Fx@1.2.3.4/live"

    def test_probe_timeout_returns_error(self, routes_client, monkeypatch):
        """探测 12s 超时 → 返回超时错误(handler 一定返回)。"""
        client, _, _ = routes_client
        real_wait_for = asyncio.wait_for

        async def fake_wait_for(aw, timeout=None):
            if timeout == 12.0:
                aw.close()  # 防止 "coroutine never awaited" 告警
                raise asyncio.TimeoutError()
            return await real_wait_for(aw, timeout=timeout)
        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        r = client.post("/api/cameras/cam_a/test-stream", json={"rtsp_url": "rtsp://1.2.3.4/live"})
        assert r.json()["data"] == {"ok": False, "error": "试连超时（12 秒无响应）"}


def camera_routes_cv2():
    """test_stream 在函数内 import cv2 → 拿到的是同一个全局模块。"""
    import cv2 as _cv2
    return _cv2


class TestPtzAndDiscoveryRoutes:
    def test_ptz_move(self, routes_client):
        client, cont, _ = routes_client
        r = client.post("/api/cameras/cam_a/ptz/move", json={"direction": "up"})
        assert r.status_code == 200
        assert r.json()["data"] == {"moved": True}
        svc = cont.ptz_registry.get.return_value
        svc.move.assert_awaited_once_with("up")

    def test_ptz_stop(self, routes_client):
        client, cont, _ = routes_client
        r = client.post("/api/cameras/cam_a/ptz/stop")
        assert r.status_code == 200
        svc = cont.ptz_registry.get.return_value
        svc.stop.assert_awaited_once_with()

    def test_ptz_step_default_duration(self, routes_client):
        client, cont, _ = routes_client
        r = client.post("/api/cameras/cam_a/ptz/step", json={"direction": "left"})
        assert r.status_code == 200
        svc = cont.ptz_registry.get.return_value
        svc.step.assert_awaited_once_with("left", 300)

    def test_ptz_step_custom_duration(self, routes_client):
        client, cont, _ = routes_client
        client.post("/api/cameras/cam_a/ptz/step", json={"direction": "left", "duration_ms": 800})
        svc = cont.ptz_registry.get.return_value
        svc.step.assert_awaited_once_with("left", 800)

    def test_discovery_manual_ip(self, routes_client):
        client, cont, _ = routes_client
        r = client.post("/api/cameras/cam_a/discovery/manual-ip", json={"ip": "192.168.1.99"})
        assert r.status_code == 200
        cont.discovery_service.apply_found_ip.assert_awaited_once_with(
            camera_id="cam_a", new_ip="192.168.1.99")


class TestFocusRoutes:
    def test_add_focus_persists_flat_list(self, routes_client):
        """增关注项 → 拍平写回 KV(vision_focuses)。"""
        client, cont, mock_db = routes_client
        r = client.post("/api/cameras/cam_a/focuses", json={"text": "人"})
        assert r.status_code == 200
        cont.vision_service.add_focus.assert_called_once_with("人", camera_id="cam_a")
        mock_db.kv_set.assert_awaited_once()
        args = mock_db.kv_set.await_args.args
        assert args[0] == "vision_focuses"

    def test_update_focus(self, routes_client):
        client, cont, _ = routes_client
        r = client.put("/api/cameras/cam_a/focuses/f1", json={"text": "猫", "enabled": False})
        assert r.status_code == 200
        cont.vision_service.update_focus.assert_called_once_with(
            "f1", text="猫", enabled=False, camera_id="cam_a")
        assert r.json()["data"]["text"] == "猫"

    def test_delete_focus(self, routes_client):
        client, cont, _ = routes_client
        r = client.delete("/api/cameras/cam_a/focuses/f1")
        assert r.status_code == 200
        assert r.json()["data"] == {"deleted": True}
        cont.vision_service.delete_focus.assert_called_once_with("f1", camera_id="cam_a")

    def test_persist_focuses_skips_when_db_unavailable(self, routes_client, monkeypatch):
        """DB 未初始化(RuntimeError) → 跳过持久化,请求仍成功。"""
        client, _, mock_db = routes_client

        def boom():
            raise RuntimeError("no db")
        monkeypatch.setattr(camera_routes.Database, "get", classmethod(lambda cls: boom()))
        r = client.post("/api/cameras/cam_a/focuses", json={"text": "人"})
        assert r.status_code == 200
        mock_db.kv_set.assert_not_awaited()


class TestRemainingCameraRoutes:
    """补齐其余薄端点(与 test_camera_routes.py 互补,保证本文件自足)。"""

    def test_enable_disable_display(self, routes_client):
        client, cont, _ = routes_client
        assert client.post("/api/cameras/cam_a/display/enable").status_code == 200
        cont.camera_manager.enable_display.assert_awaited_once_with("cam_a")
        assert client.post("/api/cameras/cam_a/display/disable").status_code == 200
        cont.camera_manager.disable_display.assert_awaited_once_with("cam_a")

    def test_discovery_find(self, routes_client):
        client, cont, _ = routes_client
        r = client.post("/api/cameras/cam_a/discovery/find")
        assert r.status_code == 200
        assert r.json()["data"] == {"new_ip": "1.2.3.4"}

    def test_list_focuses(self, routes_client):
        client, cont, _ = routes_client
        r = client.get("/api/cameras/cam_a/focuses")
        assert r.status_code == 200
        assert r.json()["data"] == [{"id": "f1"}]

    def test_list_areas(self, routes_client):
        client, cont, _ = routes_client
        cont.ha_service.get_areas = AsyncMock(return_value=[{"area_id": "a", "name": "客厅"}])
        r = client.get("/api/ha/areas")
        assert r.status_code == 200
        assert r.json()["data"][0]["name"] == "客厅"


# ===========================================================================
# 3. app/services/camera_manager.py
# ===========================================================================

import app.services.camera_manager as cm_mod


def _mock_stream(camera_id, online=True):
    s = MagicMock()
    s.camera_id = camera_id
    s._config = {"name": "n", "area": "a", "display_enabled": 1}
    s.get_state = MagicMock(return_value={"camera_id": camera_id, "camera_opened": online})
    s.get_latest_frame = MagicMock(return_value=b"frame")
    s.get_recent_frames = MagicMock(return_value=[b"frame"])
    s.mjpeg_generator = MagicMock(return_value=iter([b"mjpeg"]))
    return s


def _bare_mgr(**attrs):
    """绕过 __init__ 构造 manager(与既有测试同款),按需补属性。"""
    m = CameraManager.__new__(CameraManager)
    m._auto_sem = asyncio.Semaphore(5)
    m._streams = {}
    m._db = None
    m._loop = None
    m._active_display_id = None
    m._last_trigger_at = {}
    m._min_trigger_interval = 3.0
    m._virtual_cams = {}
    m._vision_service = None
    m._discovery_service = None
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class TestManagerConstruction:
    def test_init_wires_discovery_callback_and_clamps_concurrency(self):
        """构造:discovery 注入回调;auto_concurrency 钳到 [1,9]。"""
        discovery = MagicMock()
        m = CameraManager(vision_service=None, db=None, discovery_service=discovery,
                          auto_concurrency=99)
        discovery.set_on_ip_changed.assert_called_once_with(m._on_camera_ip_changed)
        assert m._auto_sem._value == 9
        m2 = CameraManager(auto_concurrency=0)
        assert m2._auto_sem._value == 1
        m3 = CameraManager()  # 默认走 config(测试环境缺省 5)
        assert m3._auto_sem._value == 5

    def test_setters_inject_dependencies(self):
        m = CameraManager()
        db, ha, auto = object(), object(), object()
        m.set_db(db)
        m.set_ha_service(ha)
        m.set_automation_service(auto)
        assert m._db is db and m._ha_service is ha and m._automation_service is auto


class TestManagerInitializeAndSpawn:
    @pytest.mark.asyncio
    async def test_initialize_inserts_default_camera_when_table_empty(self):
        """全新安装:cameras 表空 → 插默认 USB 路(幂等靠插入后非空)。"""
        m = _bare_mgr()
        row = {"id": "cam_default", "enabled": 1, "display_enabled": 1}
        m._db = MagicMock()
        m._db.cameras_all = AsyncMock(side_effect=[[], [row]])
        m._db.cameras_insert = AsyncMock()
        m._spawn = AsyncMock(return_value=_mock_stream("cam_default"))

        await m.initialize()

        inserted = m._db.cameras_insert.await_args.args[0]
        assert inserted["id"].startswith("cam_")
        assert inserted["enabled"] == 1 and inserted["display_enabled"] == 1
        m._spawn.assert_awaited_once_with(row)

    @pytest.mark.asyncio
    async def test_spawn_builds_and_starts_stream(self, monkeypatch):
        """_spawn:按行构造 CameraStream(带回调/discovery),注入 loop 并 start。"""
        m = _bare_mgr()
        loop = asyncio.get_running_loop()
        m.set_event_loop(loop)
        made = {}

        def fake_cls(**kwargs):
            made.update(kwargs)
            s = _mock_stream(kwargs["camera_id"])
            return s
        monkeypatch.setattr(cm_mod, "CameraStream", fake_cls)

        stream = await m._spawn({"id": "cam_x", "enabled": 1})
        assert made["camera_id"] == "cam_x"
        assert made["on_automation_trigger"] == m._on_automation_trigger
        assert m._streams["cam_x"] is stream
        stream.start.assert_called_once()
        stream.set_event_loop.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_spawn_without_loop_skips_set_event_loop(self, monkeypatch):
        m = _bare_mgr()
        m._loop = None
        monkeypatch.setattr(cm_mod, "CameraStream",
                            lambda **kwargs: _mock_stream(kwargs["camera_id"]))
        stream = await m._spawn({"id": "cam_y", "enabled": 1})
        stream.set_event_loop.assert_not_called()


class TestManagerVirtualLifecycle:
    @pytest.mark.asyncio
    async def test_register_virtual_sets_loop_when_available(self, monkeypatch):
        """注册虚拟路:loop 已注入时同步给 stream。"""
        m = _bare_mgr()
        m.set_event_loop(asyncio.get_running_loop())

        def fake_vcam(**kwargs):
            s = _mock_stream(kwargs["camera_id"])
            return s
        monkeypatch.setattr(cm_mod, "VirtualCameraStream", fake_vcam)

        info = await m.register_virtual_camera("plug1", {"name": "虚拟路"})
        assert info == {"camera_id": "vcam_plug1", "name": "虚拟路"}
        m._streams["vcam_plug1"].set_event_loop.assert_called_once_with(m._loop)
        m._streams["vcam_plug1"].start.assert_called_once()

    @pytest.mark.asyncio
    async def test_unregister_unknown_plugin_returns_false(self):
        m = _bare_mgr()
        assert await m.unregister_plugin_cameras("nope") is False

    @pytest.mark.asyncio
    async def test_unregister_resets_active_display_and_survives_stop_error(self, monkeypatch):
        """注销:停 stream 失败只记日志;是当前预览路则清 active。"""
        m = _bare_mgr()
        m._active_display_id = "vcam_p"

        def fake_vcam(**kwargs):
            s = _mock_stream(kwargs["camera_id"])
            s.stop.side_effect = RuntimeError("stop failed")
            return s
        monkeypatch.setattr(cm_mod, "VirtualCameraStream", fake_vcam)
        await m.register_virtual_camera("p", {})

        assert await m.unregister_plugin_cameras("p") is True
        assert "vcam_p" not in m._streams
        assert m._active_display_id is None

    @pytest.mark.asyncio
    async def test_unregister_without_stream_entry(self):
        """_virtual_cams 有记录但 stream 已不在 → 仍返回 True。"""
        m = _bare_mgr()
        m._virtual_cams = {"p": {"camera_id": "vcam_p", "spec": {}, "flags": {}}}
        assert await m.unregister_plugin_cameras("p") is True

    @pytest.mark.asyncio
    async def test_push_frame_decode_error(self):
        """坏 base64 → 报 frame decode failed。"""
        m = _bare_mgr()
        s = VirtualCameraStream(camera_id="vcam_x", config={}, vision_service=None)
        m._streams["vcam_x"] = s
        m._virtual_cams["p"] = {"camera_id": "vcam_x", "spec": {}, "flags": {}}
        result = m.push_frame("vcam_x", "!!!not-base64!!!")
        assert result["ok"] is False and "frame decode failed" in result["error"]

    @pytest.mark.asyncio
    async def test_push_frame_decodes_to_none(self):
        """合法 base64 但不是图片 → imdecode 返回 None。"""
        m = _bare_mgr()
        s = VirtualCameraStream(camera_id="vcam_x", config={}, vision_service=None)
        m._streams["vcam_x"] = s
        m._virtual_cams["p"] = {"camera_id": "vcam_x", "spec": {}, "flags": {}}
        result = m.push_frame("vcam_x", base64.b64encode(b"\xff\xff\xff").decode())
        assert result == {"ok": False, "error": "frame decode returned None"}

    def test_virtual_flag_default_and_missing(self):
        """set/get 虚拟路标志:未知路返回 False/default。"""
        m = _bare_mgr()
        m._virtual_cams = {"p": {"camera_id": "vcam_p", "spec": {}, "flags": {}}}
        assert m.set_virtual_flag("vcam_none", "k", 1) is False
        assert m.get_virtual_flag("vcam_none", "k", "D") == "D"
        assert m.set_virtual_flag("vcam_p", "k", 1) is True
        assert m.get_virtual_flag("vcam_p", "k") == 1

    def test_virtual_rows_shape_and_online(self):
        """_virtual_rows:与 cameras 表行形状对齐,online 取自 stream 状态。"""
        m = _bare_mgr()
        s = _mock_stream("vcam_p", online=True)
        m._streams["vcam_p"] = s
        m._virtual_cams = {"p": {"camera_id": "vcam_p",
                                 "spec": {"name": "测试", "display_enabled": 0}, "flags": {}}}
        rows = m._virtual_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "vcam_p" and row["source_type"] == "test"
        assert row["plugin_id"] == "p" and row["virtual"] is True
        assert row["online"] is True and row["display_enabled"] == 0
        assert row["sort_order"] == 9000

    @pytest.mark.asyncio
    async def test_cameras_all_db_none_returns_virtual_only(self):
        m = _bare_mgr()
        rows = await m.cameras_all()
        assert rows == []


class TestManagerCrud:
    @pytest.mark.asyncio
    async def test_create_camera_generates_id_and_spawns(self, monkeypatch):
        """创建:未带 id 自动生成;enabled 路 spawn。"""
        m = _bare_mgr()
        m._db = MagicMock()
        m._db.cameras_insert = AsyncMock()
        spawned = []

        async def fake_spawn(row):
            spawned.append(row["id"])
            return _mock_stream(row["id"])
        m._spawn = fake_spawn

        data = await m.create_camera({"name": "新", "enabled": 1})
        assert data["id"].startswith("cam_")
        m._db.cameras_insert.assert_awaited_once_with(data)
        assert spawned == [data["id"]]

    @pytest.mark.asyncio
    async def test_create_camera_disabled_does_not_spawn(self):
        m = _bare_mgr()
        m._db = MagicMock()
        m._db.cameras_insert = AsyncMock()
        m._spawn = AsyncMock()
        await m.create_camera({"name": "x", "enabled": 0})
        m._spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_camera_unknown_raises_keyerror(self):
        m = _bare_mgr()
        m._db = MagicMock()
        m._db.cameras_get = AsyncMock(return_value=None)
        with pytest.raises(KeyError):
            await m.update_camera("ghost", {"name": "x"})

    @pytest.mark.asyncio
    async def test_rebuild_stream_survives_stop_error(self):
        """重建:旧 stream stop 抛异常只记日志,流程继续。"""
        m = _bare_mgr()
        old = _mock_stream("cam_a")
        old.stop.side_effect = RuntimeError("x")
        m._streams = {"cam_a": old}
        m._db = MagicMock()
        m._db.cameras_get = AsyncMock(return_value={"id": "cam_a", "enabled": 0})
        m._spawn = AsyncMock()
        row = await m._rebuild_stream("cam_a")
        old.stop.assert_called_once()
        assert row == {"id": "cam_a", "enabled": 0}
        m._spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_camera_stops_stream_and_resets_display(self):
        m = _bare_mgr()
        old = _mock_stream("cam_a")
        m._streams = {"cam_a": old}
        m._active_display_id = "cam_a"
        m._db = MagicMock()
        m._db.cameras_delete = AsyncMock(return_value=True)

        assert await m.delete_camera("cam_a") is True
        old.stop.assert_called_once()
        assert m._active_display_id is None
        m._db.cameras_delete.assert_awaited_once_with("cam_a")

    @pytest.mark.asyncio
    async def test_delete_camera_missing_stream(self):
        m = _bare_mgr()
        m._db = MagicMock()
        m._db.cameras_delete = AsyncMock(return_value=False)
        assert await m.delete_camera("ghost") is False

    def test_stop_survives_stream_errors(self):
        """manager.stop:单路 stop 失败不影响其他路。"""
        m = _bare_mgr()
        bad, good = _mock_stream("bad"), _mock_stream("good")
        bad.stop.side_effect = RuntimeError("x")
        m._streams = {"bad": bad, "good": good}
        m.stop()
        bad.stop.assert_called_once()
        good.stop.assert_called_once()


class TestManagerDisplay:
    @pytest.mark.asyncio
    async def test_enable_display_persists_both_rows(self):
        """切换预览:新路落库 1,旧路落库 0。"""
        m = _bare_mgr()
        old, new = _mock_stream("cam_a"), _mock_stream("cam_b")
        m._streams = {"cam_a": old, "cam_b": new}
        m._active_display_id = "cam_a"
        db = MagicMock()
        db.cameras_update = AsyncMock()
        m._db = db

        await m.enable_display("cam_b")

        old.stop_display.assert_called_once()
        new.start_display.assert_called_once()
        assert m._active_display_id == "cam_b"
        assert db.cameras_update.await_args_list[0].args == ("cam_b", {"display_enabled": 1})
        assert db.cameras_update.await_args_list[1].args == ("cam_a", {"display_enabled": 0})

    @pytest.mark.asyncio
    async def test_persist_display_failure_is_contained(self):
        """落库失败只记日志,预览切换不受影响。"""
        m = _bare_mgr()
        new = _mock_stream("cam_b")
        m._streams = {"cam_b": new}
        db = MagicMock()
        db.cameras_update = AsyncMock(side_effect=RuntimeError("db"))
        m._db = db
        await m.enable_display("cam_b")  # 不抛
        assert m._active_display_id == "cam_b"

    @pytest.mark.asyncio
    async def test_disable_display_persists_zero(self):
        m = _bare_mgr()
        s = _mock_stream("cam_a")
        m._streams = {"cam_a": s}
        m._active_display_id = "cam_a"
        db = MagicMock()
        db.cameras_update = AsyncMock()
        m._db = db
        await m.disable_display("cam_a")
        s.stop_display.assert_called_once()
        assert m._active_display_id is None
        db.cameras_update.assert_awaited_once_with("cam_a", {"display_enabled": 0})

    def test_set_motion_threshold_broadcasts_and_contains_errors(self):
        """全局阈值广播;单路失败不影响其余。"""
        m = _bare_mgr()
        bad, good = _mock_stream("bad"), _mock_stream("good")
        bad.set_motion_threshold.side_effect = RuntimeError("x")
        m._streams = {"bad": bad, "good": good}
        m.set_motion_threshold(42)
        good.set_motion_threshold.assert_called_once_with(42)

    def test_global_display_switch_restores_active(self):
        """总开关开:active 路在 → 直接恢复它。"""
        m = _bare_mgr()
        s = _mock_stream("cam_a")
        m._streams = {"cam_a": s}
        m._active_display_id = "cam_a"
        m.set_camera_vl_display_enabled(True)
        s.start_display.assert_called_once()

    def test_global_display_on_falls_back_to_first_enabled(self):
        """总开关开:active 路已删 → 回退激活第一个 display_enabled 路。"""
        m = _bare_mgr()
        off, on = _mock_stream("off"), _mock_stream("on")
        off._config = {"display_enabled": 0}
        on._config = {"display_enabled": 1}
        m._streams = {"off": off, "on": on}
        m._active_display_id = "deleted"
        m.set_camera_vl_display_enabled(True)
        off.start_display.assert_not_called()
        on.start_display.assert_called_once()
        assert m._active_display_id == "on"

    def test_global_display_off_keeps_active_id(self):
        """总开关关:停当前预览路,保留 active id(on 回来恢复同一路)。"""
        m = _bare_mgr()
        s = _mock_stream("cam_a")
        m._streams = {"cam_a": s}
        m._active_display_id = "cam_a"
        m.set_camera_vl_display_enabled(False)
        s.stop_display.assert_called_once()
        assert m._active_display_id == "cam_a"


class TestManagerAccessors:
    def test_get_frame_and_state_and_mjpeg(self):
        m = _bare_mgr()
        s = _mock_stream("cam_a")
        m._streams = {"cam_a": s}
        assert m.get_frame("cam_a") == b"frame"
        assert m.get_frame("ghost") is None
        assert m.get_recent_frames("cam_a", 3) == [b"frame"]
        assert m.get_recent_frames("ghost") == []
        assert m.get_state("cam_a")["camera_id"] == "cam_a"
        assert m.mjpeg_generator("cam_a") is s.mjpeg_generator.return_value
        assert list(m.mjpeg_generator("ghost")) == []

    @pytest.mark.asyncio
    async def test_primary_camera_id(self):
        m = _bare_mgr()
        assert m.primary_camera_id() is None  # 无路
        s = _mock_stream("cam_a")
        m._streams = {"cam_a": s}
        assert m.primary_camera_id() == "cam_a"  # 无 active → 第一路
        m._active_display_id = "cam_b"
        assert m.primary_camera_id() == "cam_b"  # active 优先


class TestManagerAutomationBridge:
    @pytest.mark.asyncio
    async def test_eval_one_noop_without_service(self):
        m = _bare_mgr()
        m._automation_service = None
        await m._eval_one("cam_a", [b"f"])  # 不抛

    @pytest.mark.asyncio
    async def test_eval_one_contains_evaluate_errors(self):
        m = _bare_mgr()
        svc = MagicMock()
        svc.evaluate = AsyncMock(side_effect=RuntimeError("boom"))
        m._automation_service = svc
        await m._eval_one("cam_a", [b"f"])  # 不抛
        svc.evaluate.assert_awaited_once()


class TestManagerCoverageExtras:
    """补齐 manager 剩余分支(与既有文件互补)。"""

    @pytest.mark.asyncio
    async def test_initialize_skips_disabled_rows(self):
        """enabled=0 的路不 spawn(96 行 continue)。"""
        m = _bare_mgr()
        m._db = MagicMock()
        m._db.cameras_all = AsyncMock(return_value=[{"id": "cam_off", "enabled": 0}])
        m._spawn = AsyncMock()
        await m.initialize()
        m._spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_register_virtual_is_idempotent_per_plugin(self, monkeypatch):
        """重复注册同一插件:先注销旧路再建(141 行)。"""
        m = _bare_mgr()
        calls = {"n": 0}

        def fake_vcam(**kwargs):
            calls["n"] += 1
            return _mock_stream(kwargs["camera_id"])
        monkeypatch.setattr(cm_mod, "VirtualCameraStream", fake_vcam)
        await m.register_virtual_camera("plug", {})
        first = m._streams["vcam_plug"]
        await m.register_virtual_camera("plug", {})
        first.stop.assert_called_once()  # 旧路被注销
        assert m._streams["vcam_plug"] is not first

    def test_push_frame_unknown_camera(self):
        """推帧到不存在的路 → not found(188 行)。"""
        m = _bare_mgr()
        result = m.push_frame("vcam_ghost", "aGVsbG8=")
        assert result["ok"] is False and "not found" in result["error"]

    def test_push_frame_success_and_backpressure(self):
        """成功入队(196-197)与队满 dropped=True 背压信号。"""
        m = _bare_mgr()
        s = VirtualCameraStream(camera_id="vcam_x", config={}, vision_service=None)
        m._streams["vcam_x"] = s
        m._virtual_cams["p"] = {"camera_id": "vcam_x", "spec": {}, "flags": {}}
        ok, buf = cv2.imencode(".jpg", _frame())
        b64 = base64.b64encode(buf.tobytes()).decode()
        result = m.push_frame("vcam_x", b64)
        assert result == {"ok": True, "dropped": False}
        while not s._inject_queue.empty():  # 清掉首帧占用的格子
            s._inject_queue.get_nowait()
        for _ in range(8):  # 填满注入队列(maxsize=8)
            s._inject_queue.put_nowait(object())
        result2 = m.push_frame("vcam_x", b64)
        assert result2 == {"ok": True, "dropped": True}

    def test_set_event_loop_broadcasts_to_existing_streams(self):
        """后注入 loop:广播给已有 stream(260-261 行)。"""
        m = _bare_mgr()
        s = _mock_stream("cam_a")
        m._streams = {"cam_a": s}
        loop = MagicMock()
        m.set_event_loop(loop)
        assert m._loop is loop
        s.set_event_loop.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_update_camera_non_stream_field_rereads_row(self):
        """非流字段变更:只写库并回读最新行,不重建(292-302 路径)。"""
        m = _bare_mgr()
        old = _mock_stream("cam_a")
        m._streams = {"cam_a": old}
        db = MagicMock()
        db.cameras_get = AsyncMock(return_value={"id": "cam_a", "enabled": 1, "name": "old"})
        db.cameras_update = AsyncMock()
        m._db = db
        row = await m.update_camera("cam_a", {"name": "新名"})
        db.cameras_update.assert_awaited_once_with("cam_a", {"name": "新名"})
        old.stop.assert_not_called()
        assert row["name"] == "old"

    @pytest.mark.asyncio
    async def test_rebuild_stream_respawns_enabled_row(self):
        """重建:enabled 行重新 spawn(320 行)。"""
        m = _bare_mgr()
        m._streams = {}
        db = MagicMock()
        db.cameras_get = AsyncMock(return_value={"id": "cam_a", "enabled": 1})
        m._db = db
        spawned = []

        async def fake_spawn(row):
            spawned.append(row["id"])
            return _mock_stream(row["id"])
        m._spawn = fake_spawn
        await m._rebuild_stream("cam_a")
        assert spawned == ["cam_a"]

    @pytest.mark.asyncio
    async def test_delete_camera_survives_stop_error(self):
        """删除:停流失败只记日志,删除照常(328-329 行)。"""
        m = _bare_mgr()
        old = _mock_stream("cam_a")
        old.stop.side_effect = RuntimeError("x")
        m._streams = {"cam_a": old}
        db = MagicMock()
        db.cameras_delete = AsyncMock(return_value=True)
        m._db = db
        assert await m.delete_camera("cam_a") is True

    @pytest.mark.asyncio
    async def test_enable_display_same_camera_early_return(self):
        """重复 enable 同一路直接返回,不重复落库(355-356 行)。"""
        m = _bare_mgr()
        s = _mock_stream("cam_a")
        m._streams = {"cam_a": s}
        m._active_display_id = "cam_a"
        db = MagicMock()
        db.cameras_update = AsyncMock()
        m._db = db
        await m.enable_display("cam_a")
        s.start_display.assert_not_called()
        db.cameras_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persist_display_writes_virtual_spec_not_db(self):
        """虚拟路切预览:写运行时 spec,不写 cameras 表(342 行)。"""
        m = _bare_mgr()
        m._streams["vcam_p"] = _mock_stream("vcam_p")
        spec = {"display_enabled": 0}
        m._virtual_cams = {"p": {"camera_id": "vcam_p", "spec": spec, "flags": {}}}
        db = MagicMock()
        db.cameras_update = AsyncMock()
        m._db = db
        await m._persist_display("vcam_p", 1)
        assert spec["display_enabled"] == 1
        db.cameras_update.assert_not_awaited()

    def test_get_state_missing_stream_reports_offline(self):
        """stream 不存在(重建窗口) → 显式离线 dict(440 行)。"""
        m = _bare_mgr()
        st = m.get_state("ghost")
        assert st == {"camera_id": "ghost", "online": False, "camera_opened": False}

    @pytest.mark.asyncio
    async def test_cameras_all_merges_db_rows_with_runtime_state(self):
        """cameras_all:DB 行补 online/virtual 字段 + 合并虚拟行(470-477 行)。"""
        m = _bare_mgr()
        s = _mock_stream("cam_a", online=True)
        m._streams["cam_a"] = s
        m._virtual_cams = {"p": {"camera_id": "vcam_p", "spec": {"name": "v"}, "flags": {}}}
        vstream = _mock_stream("vcam_p", online=False)
        m._streams["vcam_p"] = vstream
        db = MagicMock()
        db.cameras_all = AsyncMock(return_value=[{"id": "cam_a", "name": "客厅"}])
        m._db = db
        rows = await m.cameras_all()
        assert rows[0]["online"] is True and rows[0]["virtual"] is False
        assert rows[1]["id"] == "vcam_p" and rows[1]["online"] is False

    def test_on_automation_trigger_schedules_via_loop(self):
        """运动触发:节流放行后投递评估到主循环(502-516 行)。"""
        m = _bare_mgr()
        loop = asyncio.new_event_loop()
        s = _mock_stream("cam_a")
        m._streams = {"cam_a": s}
        m._loop = loop
        seen = []

        async def fake_eval(cid, frames):
            seen.append((cid, frames))
        monkey_req = fake_eval
        m.request_automation_eval = monkey_req
        try:
            m._on_automation_trigger("cam_a")
            m._on_automation_trigger("cam_a")  # 节流窗口内丢弃
            deadline = time.time() + 2
            while not seen and time.time() < deadline:
                loop.run_until_complete(asyncio.sleep(0.01))
            assert seen == [("cam_a", [b"frame"])]
        finally:
            loop.close()

    def test_on_automation_trigger_no_loop_returns(self):
        """未注入 loop:触发直接返回(508 行)。"""
        m = _bare_mgr()
        m._loop = None
        m._on_automation_trigger("cam_a")  # 不抛
        assert "cam_a" in m._last_trigger_at

    def test_on_camera_ip_changed_rebuilds_via_loop(self):
        """discovery 回新 IP:经 loop 投递重建(539-544 行)。"""
        m = _bare_mgr()
        loop = asyncio.new_event_loop()
        m._loop = loop
        rebuilt = []

        async def fake_rebuild(cid):
            rebuilt.append(cid)
        m._rebuild_stream = fake_rebuild
        try:
            m._on_camera_ip_changed("cam_a", "192.168.1.99")
            deadline = time.time() + 2
            while not rebuilt and time.time() < deadline:
                loop.run_until_complete(asyncio.sleep(0.01))
            assert rebuilt == ["cam_a"]
        finally:
            loop.close()
