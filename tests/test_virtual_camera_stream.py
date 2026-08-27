"""VirtualCameraStream 单元测试：注入帧 → 真实管线（缓冲/运动触发/预览帧）。"""
from __future__ import annotations

import time

import numpy as np

from app.virtual_camera_stream import VirtualCameraStream


def _make_stream(trigger=None, frame_interval_ms=0):
    stream = VirtualCameraStream(
        camera_id="vcam_test",
        config={
            "name": "测试摄像头",
            "display_enabled": 0,      # 关预览：不起真实推理（vision_service 默认空）
            "motion_threshold": 15,
            "motion_check_interval": 0.01,
            "frame_interval_ms": frame_interval_ms,
            "vision_use_img_count": 3,
        },
        vision_service=None,
        on_automation_trigger=trigger,
    )
    return stream


def _frame(value: int = 128, size: int = 64) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.uint8)


def test_enqueue_and_buffer_fill():
    """注入帧 → get_recent_frames 返回缓冲帧（走与真实采集相同的管线）。"""
    stream = _make_stream(frame_interval_ms=0)
    stream.start()
    try:
        for i in range(5):
            assert stream.enqueue_frame(_frame(100 + i * 30))
        deadline = time.time() + 2
        while len(stream.get_recent_frames()) < 3 and time.time() < deadline:
            time.sleep(0.02)
        frames = stream.get_recent_frames()
        assert len(frames) >= 3
    finally:
        stream.stop()


def test_motion_triggers_automation_callback():
    """帧突变（dhash 距离超阈值）→ on_automation_trigger(camera_id) 回调。"""
    calls: list[str] = []

    def trigger(camera_id: str) -> None:
        calls.append(camera_id)

    stream = _make_stream(trigger=trigger, frame_interval_ms=0)
    stream.start()
    try:
        stream.enqueue_frame(_frame(10))
        time.sleep(0.2)
        stream.enqueue_frame(_frame(240))  # 剧变
        deadline = time.time() + 2
        while not calls and time.time() < deadline:
            time.sleep(0.02)
        assert calls == ["vcam_test"]
    finally:
        stream.stop()


def test_latest_jpeg_produced():
    """注入帧 → MJPEG 用的最新 JPEG 帧生成。"""
    stream = _make_stream(frame_interval_ms=0)
    stream.start()
    try:
        stream.enqueue_frame(_frame(128))
        deadline = time.time() + 2
        while stream.get_jpeg() is None and time.time() < deadline:
            time.sleep(0.02)
        jpeg = stream.get_jpeg()
        assert jpeg is not None and jpeg[:2] == b"\xff\xd8"  # JPEG SOI
    finally:
        stream.stop()


def test_state_reports_opened_with_fps_zero():
    """虚拟源无 cap → state.fps 走 _read_source_fps 默认 0，不崩。"""
    stream = _make_stream(frame_interval_ms=0)
    stream.start()
    try:
        stream.enqueue_frame(_frame(64))
        deadline = time.time() + 2
        st = {}
        while time.time() < deadline:
            st = stream.get_state()
            if st.get("camera_opened"):
                break
            time.sleep(0.02)
        assert st["camera_opened"] is True
        assert st["fps"] == 0.0
        assert st["backend_name"] == "virtual"
    finally:
        stream.stop()


def test_stop_is_clean():
    """stop 后线程退出、无残留。"""
    stream = _make_stream()
    stream.start()
    stream.stop()
    assert stream._running is False
    assert stream._thread is None
