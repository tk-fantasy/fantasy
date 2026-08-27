"""test-camera 插件单元测试（mock host，不 spawn 子进程）。

简化后行为：单一当前视频（playback.set 设置即播、清上传目录旧文件、
循环播放）、config.set 同步 real_exec 标志、播放 worker 采样发送。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np

PLUGIN_DIR = Path(__file__).parent.parent.parent / "integrations" / "test-camera"

_spec = importlib.util.spec_from_file_location("test_camera_plugin", PLUGIN_DIR / "plugin.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TestCameraPlugin = _module.TestCameraPlugin


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeCap:
    """假视频源：读 N 帧后 EOF。seek 计数上限防循环播放把测试拖成无限。"""

    def __init__(self, total_frames=8, max_loops=1):
        self.total = total_frames
        self.read_count = 0
        self.loops = 0
        self.max_loops = max_loops

    def isOpened(self):
        return True

    def get(self, prop):
        if prop == 5:  # cv2.CAP_PROP_FPS
            return 25.0
        if prop == 7:  # cv2.CAP_PROP_FRAME_COUNT
            return float(self.total)
        return 0.0

    def read(self):
        self.read_count += 1
        if self.read_count > self.total:
            return False, None
        return True, np.full((48, 48, 3), (self.read_count * 25) % 255, dtype=np.uint8)

    def set(self, prop, value):
        if prop == 1:  # CAP_PROP_POS_FRAMES：循环播放 seek 0
            self.loops += 1
            self.read_count = 0
        return True

    def release(self):
        pass


def _make_plugin(monkeypatch, tmp_path, upload_dir=None):
    """构造插件 + mock host.camera + 手动注册方法（不走完整 setup 的异步链）。"""
    plugin = TestCameraPlugin()

    host_camera = AsyncMock()
    host_camera.register.return_value = {"camera_id": "vcam_test-camera", "name": "测试摄像头"}
    host_camera.push_frame.return_value = {"ok": True, "dropped": False}
    host_camera.set_flags.return_value = {"ok": True}
    plugin.host = types.SimpleNamespace(camera=host_camera)

    if upload_dir is not None:
        monkeypatch.setenv("AETHER_PLUGIN_UPLOAD_DIR", str(upload_dir))

    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    for name in ("playback.set", "playback.restart", "playback.status",
                 "config.set", "config.get"):
        plugin.register_method(name, getattr(plugin, f"_m_{name.replace('.', '_')}"))
    plugin.manifest = manifest
    plugin.camera_id = "vcam_test-camera"
    return plugin, host_camera


def test_playback_set_validates_path(tmp_path, monkeypatch):
    plugin, _ = _make_plugin(monkeypatch, tmp_path)
    result = _run(plugin.handle("playback.set", {"path": str(tmp_path / "nope.mp4")}))
    assert "error" in result and "不存在" in result["error"]
    assert plugin.current_video is None


def test_playback_set_accepts_and_records(tmp_path, monkeypatch):
    plugin, _ = _make_plugin(monkeypatch, tmp_path)
    real = tmp_path / "v.mp4"
    real.write_bytes(b"x" * 16)
    monkeypatch.setattr(_module.cv2, "VideoCapture", lambda *a, **k: _FakeCap())
    result = _run(plugin.handle("playback.set", {"path": str(real), "name": "测试"}))
    assert result.get("ok") is True
    assert result["video"]["name"] == "测试"
    assert plugin.current_video["path"] == str(real.resolve())
    assert result["video"]["duration_s"] > 0


def test_playback_set_cleans_old_uploads(tmp_path, monkeypatch):
    """上传目录只保留当前视频，旧的直接删；目录外文件不碰。"""
    upload_dir = tmp_path / "uploads" / "test-camera"
    upload_dir.mkdir(parents=True)
    old = upload_dir / "20260101_000000_old.mp4"
    old.write_bytes(b"old")
    keep = upload_dir / "20260102_000000_new.mp4"
    keep.write_bytes(b"new" * 100)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"out")

    plugin, _ = _make_plugin(monkeypatch, tmp_path, upload_dir=upload_dir)
    monkeypatch.setattr(_module.cv2, "VideoCapture", lambda *a, **k: _FakeCap())

    # restart 会走 loop（None 时直接 return），只验证清理与记录
    result = _run(plugin.handle("playback.set", {"path": str(keep)}))
    assert result.get("ok") is True
    assert not old.exists()          # 旧上传已删
    assert keep.exists()             # 当前视频保留
    assert outside.exists()          # 目录外文件绝不碰


def test_playback_restart_requires_video(tmp_path, monkeypatch):
    plugin, _ = _make_plugin(monkeypatch, tmp_path)
    result = _run(plugin.handle("playback.restart", {}))
    assert "error" in result


def test_config_set_syncs_real_exec(tmp_path, monkeypatch):
    plugin, host_camera = _make_plugin(monkeypatch, tmp_path)
    result = _run(plugin.handle("config.set", {"real_exec": True}))
    assert result["ok"] is True
    host_camera.set_flags.assert_awaited_once()
    args = host_camera.set_flags.call_args.args
    assert args[0] == "vcam_test-camera"
    assert args[1] == {"real_exec": True}
    cfg = _run(plugin.handle("config.get", {}))
    assert cfg["real_exec"] is True


def test_play_worker_samples_and_sends(tmp_path, monkeypatch):
    """播放线程：假视频 → 采样编码 → 发送队列出现 base64 帧，受限循环后退出。"""
    plugin, _ = _make_plugin(monkeypatch, tmp_path)
    real = tmp_path / "v.mp4"
    real.write_bytes(b"x" * 16)
    monkeypatch.setattr(_module, "MAX_PUSH_FPS", 50.0)
    fake = _FakeCap(total_frames=8, max_loops=1)
    monkeypatch.setattr(_module.cv2, "VideoCapture", lambda *a, **k: fake)

    _run(plugin.handle("playback.set", {"path": str(real)}))

    # 播完一轮 seek 时停掉 worker（模拟测试收尾）
    orig_set = fake.set

    def _set(prop, value):
        orig_set(prop, value)
        if prop == 1:
            plugin._play_seq += 1  # 让 worker 在下一轮退出

    fake.set = _set
    # sleep 置空 + monotonic 递增：限频逻辑用时间差判断，时间必须"流逝"
    fake_clock = {"now": 0.0}
    monkeypatch.setattr(_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(_module.time, "monotonic",
                        lambda: (fake_clock.__setitem__("now", fake_clock["now"] + 0.05)
                                 or fake_clock["now"]))

    plugin._play_worker(seq=plugin._play_seq)
    # 8 帧 25fps、MAX_PUSH_FPS=50 → step=1，第一轮全部入队
    assert plugin._sent >= 4
    # 队列里的帧可解码为合法 base64 JPEG
    b64 = plugin._send_queue.get_nowait()
    import base64
    raw = base64.b64decode(b64)
    assert raw[:2] == b"\xff\xd8"  # JPEG SOI
