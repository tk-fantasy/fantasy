"""CameraManager 虚拟摄像头 API 测试：注册/推送/注销/cameras_all 合并/flag。"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import cv2
import numpy as np

from app.services.camera_manager import CameraManager


def _make_manager():
    db = MagicMock()
    # side_effect 每次返回新列表：AsyncMock(return_value=[...]) 共享同一对象，
    # cameras_all 的 rows.extend(virtual) 会污染它（第二次调用混入过期虚拟行）
    db.cameras_all = AsyncMock(side_effect=lambda: [
        {"id": "cam_real", "name": "客厅", "enabled": 1, "source_type": "rtsp",
         "display_enabled": 1, "sort_order": 0},
    ])
    db.cameras_update = AsyncMock(return_value=True)
    manager = CameraManager(vision_service=None, db=db)
    return manager, db


def _jpeg_b64(value: int = 128) -> str:
    ok, buf = cv2.imencode(".jpg", np.full((32, 32, 3), value, dtype=np.uint8))
    assert ok
    return base64.b64encode(buf.tobytes()).decode("ascii")


def test_register_push_unregister():
    manager, _ = _make_manager()
    info = _run(manager.register_virtual_camera("test-camera", {"name": "测试摄像头"}))
    cid = info["camera_id"]
    assert cid == "vcam_test-camera"  # 确定性 id

    # is_virtual / flags
    assert manager.is_virtual_camera(cid) is True
    assert manager.is_virtual_camera("cam_real") is False
    assert manager.get_virtual_flag(cid, "real_exec", False) is False
    assert manager.set_virtual_flag(cid, "real_exec", True) is True
    assert manager.get_virtual_flag(cid, "real_exec", False) is True

    # 推帧
    stream = manager._streams[cid]
    result = manager.push_frame(cid, _jpeg_b64())
    assert result["ok"] is True

    # 注销
    assert _run(manager.unregister_plugin_cameras("test-camera")) is True
    assert cid not in manager._streams
    assert manager.is_virtual_camera(cid) is False
    stream.stop()


def test_register_idempotent_restart():
    """插件重启重复注册：旧路注销新路顶上，不留双份。"""
    manager, _ = _make_manager()
    _run(manager.register_virtual_camera("test-camera", {}))
    _run(manager.register_virtual_camera("test-camera", {"name": "第二次"}))
    cams = [c for c in manager.list_cameras() if c["id"].startswith("vcam_")]
    assert len(cams) == 1
    for s in list(manager._streams.values()):
        s.stop()


def test_cameras_all_merges_virtual_rows():
    """cameras_all = DB 真实行 + 虚拟行（同形状，source_type='test'）。"""
    manager, _ = _make_manager()
    _run(manager.register_virtual_camera("test-camera", {"name": "测试摄像头"}))
    rows = _run(manager.cameras_all())
    vrows = [r for r in rows if r.get("source_type") == "test"]
    assert len(vrows) == 1
    assert vrows[0]["id"] == "vcam_test-camera"
    assert vrows[0]["name"] == "测试摄像头"
    assert vrows[0]["virtual"] is True
    assert "rtsp_url" in vrows[0]  # 与 DB 行形状对齐
    real = [r for r in rows if r["id"] == "cam_real"]
    assert real and real[0]["virtual"] is False
    for s in list(manager._streams.values()):
        s.stop()


def test_display_toggle_skips_db_and_updates_spec():
    """虚拟路切预览：不写 cameras 表，display 态回写运行时 spec（回显用）。"""
    manager, db = _make_manager()
    info = _run(manager.register_virtual_camera("test-camera", {"display_enabled": 0}))
    cid = info["camera_id"]
    _run(manager.enable_display(cid))
    assert db.cameras_update.await_count == 0
    # spec 回写：cameras_all 回显 display_enabled=1
    rows = _run(manager.cameras_all())
    vrow = next(r for r in rows if r["id"] == cid)
    assert vrow["display_enabled"] == 1
    _run(manager.disable_display(cid))
    assert db.cameras_update.await_count == 0
    rows = _run(manager.cameras_all())
    vrow = next(r for r in rows if r["id"] == cid)
    assert vrow["display_enabled"] == 0
    for s in list(manager._streams.values()):
        s.stop()


def test_push_frame_unknown_camera():
    manager, _ = _make_manager()
    result = manager.push_frame("vcam_nope", _jpeg_b64())
    assert result["ok"] is False
    assert "not found" in result["error"]


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)
