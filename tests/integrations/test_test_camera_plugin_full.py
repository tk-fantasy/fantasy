"""test-camera 插件补充测试（第二批，兄弟文件，同 importlib 模式）。

覆盖第一批未触达的分支：setup/_startup 生命周期、sender 发送循环、
播放 worker 的 step/限频/编码失败分支、重启播放的线程拉起路径、
上传目录清理边界、config.set 的 camera_name 与 set_flags 失败路径。

边界 mock：host 反向 RPC（AsyncMock）、cv2.VideoCapture/imencode、
time.monotonic/sleep；不 spawn 子进程、无真实摄像头。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import logging
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np

PLUGIN_DIR = Path(__file__).parent.parent.parent / "integrations" / "test-camera"

_spec = importlib.util.spec_from_file_location("test_camera_plugin_full", PLUGIN_DIR / "plugin.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TestCameraPlugin = _module.TestCameraPlugin


def _make_plugin():
    """构造插件 + AsyncMock host.camera，注册全部自定义方法（不走 setup 异步链）。"""
    plugin = TestCameraPlugin()
    cam = AsyncMock()
    cam.register.return_value = {"camera_id": "vcam_x", "name": "测试摄像头"}
    cam.push_frame.return_value = {"ok": True}
    cam.set_flags.return_value = {"ok": True}
    plugin.host = types.SimpleNamespace(camera=cam)
    plugin.camera_id = "vcam_x"
    for name in ("playback.set", "playback.restart", "playback.status",
                 "config.set", "config.get"):
        plugin.register_method(name, getattr(plugin, f"_m_{name.replace('.', '_')}"))
    plugin.manifest = {"id": "test-camera", "capabilities": []}
    return plugin, cam


async def _drain_pending_tasks():
    """取消并等待本测试在事件循环里留下的常驻任务（sender 循环等）。"""
    current = asyncio.current_task()
    for t in list(asyncio.all_tasks()):
        if t is not current:
            t.cancel()
    for t in list(asyncio.all_tasks()):
        if t is not current:
            with contextlib.suppress(asyncio.CancelledError):
                await t


def _frame(v: int = 7) -> np.ndarray:
    return np.full((48, 48, 3), v, dtype=np.uint8)


class _EofCap:
    """立即 EOF 的假视频源：seek 即把播放代次 +1，让 worker 自然退出。"""

    def __init__(self, plugin: TestCameraPlugin):
        self._plugin = plugin
        self.reads = 0
        self.released = False

    def isOpened(self):
        return True

    def get(self, prop):
        return 25.0 if prop == 5 else 0.0

    def read(self):
        self.reads += 1
        return False, None

    def set(self, prop, value):
        self._plugin._play_seq += 1
        return True

    def release(self):
        self.released = True


class _SeqCap:
    """顺序出帧、EOF 一次后退出的假视频源（EOF 让 worker 退出，防无限循环）。"""

    def __init__(self, frames, plugin: TestCameraPlugin, fps: float = 25.0):
        self._frames = list(frames)
        self._plugin = plugin
        self._fps = fps
        self.released = False

    def isOpened(self):
        return True

    def get(self, prop):
        if prop == 5:
            return self._fps
        if prop == 7:
            return float(len(self._frames))
        return 0.0

    def read(self):
        if not self._frames:
            self._plugin._play_seq += 1  # EOF：worker 下一轮 while 退出
            return False, None
        return True, self._frames.pop(0)

    def set(self, prop, value):
        return True

    def release(self):
        self.released = True


def _run_worker(monkeypatch, plugin: TestCameraPlugin, cap, monotonic_values=None):
    """同步跑一遍播放 worker；monotonic_values 控制限频时钟。"""
    plugin.current_video = {"path": "x.mp4", "name": "x"}
    monkeypatch.setattr(_module.cv2, "VideoCapture", lambda *a, **k: cap)
    monkeypatch.setattr(_module.time, "sleep", lambda s: None)
    if monotonic_values is not None:
        clock = {"i": 0}

        def _mono():
            v = monotonic_values[min(clock["i"], len(monotonic_values) - 1)]
            clock["i"] += 1
            return v

        monkeypatch.setattr(_module.time, "monotonic", _mono)
    plugin._play_worker(seq=plugin._play_seq)


# ================================================================ 生命周期


async def test_setup_registers_methods_and_startup_starts_sender():
    plugin, cam = _make_plugin()
    plugin.setup({"id": "test-camera", "capabilities": []})
    assert set(plugin._custom_methods) == {
        "playback.set", "playback.restart", "playback.status", "config.set", "config.get"}
    assert plugin._loop is asyncio.get_running_loop()

    # startup 任务执行：注册虚拟摄像头，并拉起 sender 循环投递队列里的帧
    plugin._send_queue.put_nowait("Zm9v")
    for _ in range(30):
        await asyncio.sleep(0.01)
        if cam.push_frame.await_count:
            break
    assert plugin.camera_id == "vcam_x"
    cam.register.assert_awaited_once()
    spec = cam.register.call_args.args[0]
    assert spec["name"] == "测试摄像头"
    assert spec["display_enabled"] == 1
    assert spec["flags"] == {"real_exec": False}
    assert cam.push_frame.await_args.args == ("vcam_x", "Zm9v")
    await _drain_pending_tasks()


async def test_startup_register_failure_logs_and_skips_sender(caplog):
    plugin, cam = _make_plugin()
    plugin.camera_id = ""  # 尚未注册成功
    cam.register.side_effect = RuntimeError("host has no camera permission")
    with caplog.at_level(logging.ERROR, logger="test-camera"):
        await plugin._startup()
    assert plugin.camera_id == ""
    assert any("注册失败" in r.message for r in caplog.records)
    cam.push_frame.assert_not_awaited()  # sender 循环未拉起


async def test_sender_loop_pushes_rejects_and_survives_errors():
    plugin, cam = _make_plugin()
    cam.push_frame.side_effect = [{"ok": True}, {"ok": False}, RuntimeError("ws down")]
    task = asyncio.create_task(plugin._sender_loop())
    plugin._send_queue.put_nowait("f1")
    plugin._send_queue.put_nowait("f2")
    plugin._send_queue.put_nowait("f3")
    for _ in range(60):
        await asyncio.sleep(0.01)
        if cam.push_frame.await_count >= 3:
            break
    await asyncio.sleep(0.06)  # 队列空路径（sleep+continue）也转一圈
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert cam.push_frame.await_count == 3
    assert [c.args for c in cam.push_frame.await_args_list] == [
        ("vcam_x", "f1"), ("vcam_x", "f2"), ("vcam_x", "f3")]


# ================================================================ 重启播放


async def test_restart_playback_needs_camera_and_video(monkeypatch):
    plugin, _ = _make_plugin()
    plugin._loop = asyncio.get_running_loop()
    spy = MagicMock(side_effect=plugin._loop.call_soon_threadsafe)
    monkeypatch.setattr(plugin._loop, "call_soon_threadsafe", spy)

    plugin.camera_id = ""
    plugin.current_video = {"path": "x.mp4", "name": "x"}
    plugin._restart_playback()
    assert plugin._play_seq == 1
    spy.assert_not_called()

    plugin.camera_id = "vcam_x"
    plugin.current_video = None
    plugin._restart_playback()
    assert plugin._play_seq == 2
    spy.assert_not_called()  # 无可播内容 → 不拉起线程


async def test_restart_playback_launches_worker_in_executor(monkeypatch, tmp_path):
    plugin, _ = _make_plugin()
    plugin._loop = asyncio.get_running_loop()
    plugin.current_video = {"path": str(tmp_path / "v.mp4"), "name": "v"}
    caps: list[_EofCap] = []
    monkeypatch.setattr(
        _module.cv2, "VideoCapture",
        lambda *a, **k: caps.append(_EofCap(plugin)) or caps[-1])
    monkeypatch.setattr(_module.time, "sleep", lambda s: None)

    plugin._restart_playback()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if caps and caps[0].released:
            break
    assert caps, "executor 里没有拉起播放 worker"
    assert caps[0].reads >= 1   # worker 真的读过帧（EOF）
    assert caps[0].released     # EOF → set 把代次 +1 → worker 退出并释放
    assert plugin._play_seq >= 2
    await asyncio.sleep(0.05)


async def test_restart_playback_call_soon_runtimeerror_swallowed():
    plugin, _ = _make_plugin()
    loop = MagicMock()
    loop.is_closed.return_value = False
    loop.call_soon_threadsafe.side_effect = RuntimeError("loop closing")
    plugin._loop = loop
    plugin.camera_id = "vcam_x"
    plugin.current_video = {"path": "x.mp4", "name": "x"}
    plugin._restart_playback()  # 进程退出竞态 → 吞掉 RuntimeError


# ================================================================ 上传目录清理


async def test_cleanup_old_uploads_missing_dir_is_noop(monkeypatch, tmp_path):
    plugin, _ = _make_plugin()
    monkeypatch.setenv("AETHER_PLUGIN_UPLOAD_DIR", str(tmp_path / "nope"))
    plugin._cleanup_old_uploads("keep.mp4")  # 目录不存在 → 直接返回，不抛


async def test_cleanup_old_uploads_oserror_logged(monkeypatch, tmp_path, caplog):
    plugin, _ = _make_plugin()
    upload_dir = tmp_path / "up"
    upload_dir.mkdir()
    monkeypatch.setenv("AETHER_PLUGIN_UPLOAD_DIR", str(upload_dir))

    class _BoomRoot:
        def is_dir(self):
            return True

        def resolve(self):
            return self

        def iterdir(self):
            raise OSError("permission denied")

    monkeypatch.setattr(_module, "Path", lambda p: _BoomRoot())
    with caplog.at_level(logging.WARNING, logger="test-camera"):
        plugin._cleanup_old_uploads("keep.mp4")  # OSError 被吞并记告警
    assert any("清理旧上传文件失败" in r.message for r in caplog.records)


# ================================================================ playback.set / restart / status


async def test_playback_set_empty_path_rejected():
    plugin, _ = _make_plugin()
    result = await plugin.handle("playback.set", {"path": "   "})
    assert result == {"error": "path required"}
    assert plugin.current_video is None


async def test_playback_set_unopenable_video(monkeypatch, tmp_path):
    plugin, _ = _make_plugin()
    real = tmp_path / "bad.mp4"
    real.write_bytes(b"nope")
    dead = MagicMock()
    dead.isOpened.return_value = False
    dead.get.return_value = 0.0
    monkeypatch.setattr(_module.cv2, "VideoCapture", lambda *a, **k: dead)
    result = await plugin.handle("playback.set", {"path": str(real)})
    assert "无法打开" in result["error"]
    assert plugin.current_video is None
    dead.release.assert_called_once()  # probe 句柄必须释放


async def test_playback_restart_ok(monkeypatch, tmp_path):
    plugin, _ = _make_plugin()
    plugin._loop = asyncio.get_running_loop()
    plugin.current_video = {"path": str(tmp_path / "v.mp4"), "name": "v"}
    caps: list[_EofCap] = []
    monkeypatch.setattr(
        _module.cv2, "VideoCapture",
        lambda *a, **k: caps.append(_EofCap(plugin)) or caps[-1])
    monkeypatch.setattr(_module.time, "sleep", lambda s: None)

    result = await plugin.handle("playback.restart", {})
    assert result == {"ok": True}
    for _ in range(100):
        await asyncio.sleep(0.02)
        if caps and caps[0].released:
            break
    assert caps and caps[0].released
    await asyncio.sleep(0.05)


async def test_playback_status_reflects_state():
    plugin, _ = _make_plugin()
    plugin._sent, plugin._dropped = 7, 3
    empty = await plugin.handle("playback.status", {})
    assert empty == {"camera_id": "vcam_x", "current": None, "playing": False,
                     "sent": 7, "dropped": 3}

    video = {"path": "/tmp/v.mp4", "name": "v", "fps": 25.0, "duration_s": 0.3}
    with plugin._lock:
        plugin.current_video = video
    status = await plugin.handle("playback.status", {})
    assert status["playing"] is True
    assert status["current"] == video
    assert status["current"] is not plugin.current_video  # 返回快照副本


# ================================================================ 播放 worker 分支


def test_play_worker_exits_without_video_or_camera(monkeypatch):
    plugin, _ = _make_plugin()
    factory = MagicMock()
    monkeypatch.setattr(_module.cv2, "VideoCapture", factory)

    plugin.camera_id = ""
    plugin._play_worker(seq=1)  # 无视频 → 直接返回
    plugin.current_video = {"path": "x.mp4", "name": "x"}
    plugin.camera_id = ""
    plugin._play_worker(seq=1)  # 无摄像头 → 直接返回
    factory.assert_not_called()  # 两种情况都没碰视频源


def test_play_worker_open_failure_logs(monkeypatch, caplog):
    plugin, _ = _make_plugin()
    plugin.current_video = {"path": "x.mp4", "name": "x"}
    dead = MagicMock()
    dead.isOpened.return_value = False
    monkeypatch.setattr(_module.cv2, "VideoCapture", lambda *a, **k: dead)
    with caplog.at_level(logging.WARNING, logger="test-camera"):
        plugin._play_worker(seq=1)
    assert any("打开视频失败" in r.message for r in caplog.records)
    dead.release.assert_not_called()


def test_play_worker_step_skips_frames(monkeypatch):
    """25fps 视频 + MAX_PUSH_FPS=12.5 → step=2：奇数序号帧被跳过。"""
    plugin, _ = _make_plugin()
    monkeypatch.setattr(_module, "MAX_PUSH_FPS", 12.5)
    frames = [_frame(i * 10 + 1) for i in range(6)]
    cap = _SeqCap(frames, plugin)
    _run_worker(monkeypatch, plugin, cap,
                monotonic_values=[100.0, 101.0, 102.0])  # 时间流逝够快，不限频
    assert plugin._sent == 3        # 仅 idx=2/4/6 入队
    assert plugin._dropped == 0
    assert plugin._send_queue.qsize() == 3
    assert cap.released


def test_play_worker_rate_limit_skips_sample(monkeypatch):
    """时钟不走：首帧发出后，后续采样全部被限频跳过。"""
    plugin, _ = _make_plugin()
    monkeypatch.setattr(_module, "MAX_PUSH_FPS", 50.0)  # step=1，每帧都到限频判断
    frames = [_frame() for _ in range(5)]
    cap = _SeqCap(frames, plugin)
    _run_worker(monkeypatch, plugin, cap, monotonic_values=[100.0])
    assert plugin._sent == 1
    assert plugin._send_queue.qsize() == 1


def test_play_worker_encode_failure_skips_frame(monkeypatch):
    plugin, _ = _make_plugin()
    monkeypatch.setattr(_module, "MAX_PUSH_FPS", 50.0)
    frames = [_frame() for _ in range(3)]
    cap = _SeqCap(frames, plugin)
    monkeypatch.setattr(plugin, "_encode_frame", lambda frame: None)
    _run_worker(monkeypatch, plugin, cap,
                monotonic_values=[100.0, 200.0, 300.0])
    assert plugin._sent == 0
    assert plugin._send_queue.qsize() == 0
    assert cap.released


# ================================================================ 帧编码


def test_encode_frame_downscales_large_frame():
    plugin, _ = _make_plugin()
    b64 = plugin._encode_frame(np.zeros((100, 800, 3), dtype=np.uint8))  # 长边 800 > 640
    assert isinstance(b64, str)
    assert base64.b64decode(b64)[:2] == b"\xff\xd8"  # JPEG SOI


def test_encode_frame_returns_none_on_encode_failure(monkeypatch):
    plugin, _ = _make_plugin()
    monkeypatch.setattr(_module.cv2, "imencode", lambda *a, **k: (False, None))
    assert plugin._encode_frame(_frame(3)) is None


# ================================================================ config


async def test_config_set_camera_name_updates_and_get_roundtrip():
    plugin, cam = _make_plugin()
    result = await plugin.handle("config.set", {"camera_name": "  前门摄像头  "})
    assert result == {"ok": True}
    assert plugin.camera_name == "前门摄像头"  # strip 生效

    got = await plugin.handle("config.get", {})
    assert got == {"real_exec": False, "camera_name": "前门摄像头",
                   "camera_id": "vcam_x"}

    await plugin.handle("config.set", {"camera_name": "   "})  # 空白名不改
    assert plugin.camera_name == "前门摄像头"
    cam.set_flags.assert_not_awaited()  # 无 real_exec → 不同步宿主 flags


async def test_config_set_flags_sync_failure_swallowed(caplog):
    plugin, cam = _make_plugin()
    cam.set_flags.side_effect = RuntimeError("host gone")
    with caplog.at_level(logging.WARNING, logger="test-camera"):
        result = await plugin.handle("config.set", {"real_exec": True})
    assert result == {"ok": True}   # 同步失败不影响开关本身生效
    assert plugin.real_exec is True
    assert any("set_flags 同步失败" in r.message for r in caplog.records)
