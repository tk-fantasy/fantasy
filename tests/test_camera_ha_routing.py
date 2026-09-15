"""阶段2：摄像头 HA 分层路由——触发源/抓帧/在线语义/PTZ 服务路径。

覆盖：
- _is_ha_only / _sync_ha_camera_row 改绑清理 / _drop_ha_camera
- HA-only 路不 spawn 常驻解码器（防误抓 /dev/video0）
- on_ha_motion_trigger 与 dHash 共用节流闸；取帧 camera_proxy 优先、常驻流兜底
- _on_ha_state_event：binary_sensor on 触发 / camera 实体可用性更新
- get_state/list_cameras 的 HA-only 在线语义
- fetch_camera_proxy_frames：JPEG 解码 / 失败返回空
- PtzService：HA 服务路径（onvif.ptz）+ 失败回退本地 ONVIF
- test-stream 路由 HA 分支 + /cameras/ha-entities 候选端点
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.services.camera_manager import CameraManager
from app.services.ptz_service import PtzService, ptz_registry


def _jpeg_bytes() -> bytes:
    import cv2
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


@pytest.fixture(autouse=True)
def _mock_stream_class():
    """全文件禁用真实 CameraStream：_spawn 起真 worker 会 cv2.VideoCapture
    抓本机摄像头/RTSP，泄漏的后台线程与后续测试的 cv2 并发会触发 Windows
    堆损坏（0xc0000374）。本文件只测 HA 路由逻辑，不需要真实取流。
    mock 流的 get_recent_frames 默认返回 1 帧（常驻流兜底路径的桩值）。
    """
    with patch("app.services.camera_manager.CameraStream") as CS:
        fake = MagicMock()
        fake.get_recent_frames = MagicMock(return_value=[np.zeros((4, 4, 3))])
        fake.get_state = MagicMock(
            return_value={"camera_id": "cam_1", "camera_opened": True})
        CS.return_value = fake
        yield CS


def _mgr() -> CameraManager:
    """轻量 manager：绕过 __init__，手工装配 HA 路相关状态。"""
    mgr = CameraManager.__new__(CameraManager)
    mgr._streams = {}
    mgr._virtual_cams = {}
    mgr._ha_rows = {}
    mgr._ha_camera_entity_map = {}
    mgr._motion_entity_map = {}
    mgr._ha_availability = {}
    mgr._unsub_events = None
    mgr._avail_task = None
    mgr._auto_sem = asyncio.Semaphore(5)
    mgr._last_trigger_at = {}
    mgr._min_trigger_interval = 3.0
    mgr._loop = None
    mgr._db = MagicMock()
    mgr._ha_service = MagicMock()
    mgr._vision_service = None
    mgr._discovery_service = None
    mgr._automation_service = None
    mgr._active_display_id = None
    return mgr


def _row(cid="cam_1", cam_entity="", motion_entity="", rtsp="", **kw):
    return {
        "id": cid, "name": kw.get("name", "门口"), "enabled": 1,
        "source_type": "rtsp", "rtsp_url": rtsp,
        "ha_camera_entity": cam_entity, "ha_motion_entity": motion_entity,
        "vision_use_img_count": 2, "area": "", **kw,
    }


class TestHaOnlySpawn:
    async def test_ha_only_row_skips_resident_decoder(self, _mock_stream_class):
        """挂 HA 实体且无 RTSP：不 spawn worker，注册实体映射。"""
        mgr = _mgr()
        row = _row(cam_entity="camera.door", motion_entity="binary_sensor.motion")
        stream = await mgr._spawn(row)
        assert stream is None
        assert _mock_stream_class.call_count == 0
        assert mgr._ha_camera_entity_map["camera.door"] == "cam_1"
        assert mgr._motion_entity_map["binary_sensor.motion"] == "cam_1"

    async def test_hybrid_row_spawns_and_registers(self, _mock_stream_class):
        """HA 实体 + RTSP 混合路：spawn 保留（预览/dHash 兜底）+ 注册映射。"""
        mgr = _mgr()
        row = _row(cam_entity="camera.door", rtsp="rtsp://x")
        stream = await mgr._spawn(row)
        assert _mock_stream_class.call_count == 1
        assert mgr._streams["cam_1"] is stream
        assert mgr._ha_camera_entity_map["camera.door"] == "cam_1"

    async def test_resync_rebinds_and_cleans_old_entities(self, _mock_stream_class):
        """改绑实体：旧反查条目必须清掉，否则事件回调打到旧摄像头。"""
        mgr = _mgr()
        await mgr._spawn(_row(cam_entity="camera.old", motion_entity="binary_sensor.a"))
        await mgr._spawn(_row(cam_entity="camera.new", motion_entity="binary_sensor.b"))
        assert "camera.old" not in mgr._ha_camera_entity_map
        assert "binary_sensor.a" not in mgr._motion_entity_map
        assert mgr._ha_camera_entity_map["camera.new"] == "cam_1"
        assert mgr._motion_entity_map["binary_sensor.b"] == "cam_1"

    async def test_drop_ha_camera_cleans_maps(self, _mock_stream_class):
        mgr = _mgr()
        await mgr._spawn(_row(cam_entity="camera.door", motion_entity="binary_sensor.m"))
        mgr._drop_ha_camera("cam_1")
        assert not mgr._ha_rows and not mgr._ha_camera_entity_map
        assert not mgr._motion_entity_map and not mgr._ha_availability


class TestHaMotionTrigger:
    async def test_trigger_fetches_proxy_frames_and_evaluates(self, _mock_stream_class):
        mgr = _mgr()
        await mgr._spawn(_row(cam_entity="camera.door", motion_entity="binary_sensor.m"))
        mgr.fetch_camera_proxy_frames_for_camera = AsyncMock(return_value=[np.zeros((4, 4, 3))])
        mgr._eval_one = AsyncMock()
        await mgr.on_ha_motion_trigger("binary_sensor.m")
        mgr.fetch_camera_proxy_frames_for_camera.assert_awaited_once_with("cam_1", 2)
        mgr._eval_one.assert_awaited_once()

    async def test_throttle_shared_with_dhash_path(self, _mock_stream_class):
        """HA 事件与本地 dHash 共用节流闸：窗口内第二个触发源被丢弃。"""
        mgr = _mgr()
        await mgr._spawn(_row(motion_entity="binary_sensor.m"))
        mgr.fetch_camera_proxy_frames_for_camera = AsyncMock(return_value=[np.zeros((4, 4, 3))])
        mgr._eval_one = AsyncMock()
        await mgr.on_ha_motion_trigger("binary_sensor.m")
        # dHash 路（worker 线程回调）紧随其后 → 被同一节流闸拦下
        mgr._on_automation_trigger("cam_1")
        assert mgr._eval_one.await_count == 1

    async def test_falls_back_to_resident_stream(self, _mock_stream_class):
        """proxy 失败 → 常驻解码流兜底（混合路）。"""
        mgr = _mgr()
        await mgr._spawn(_row(cam_entity="camera.door", motion_entity="binary_sensor.m",
                              rtsp="rtsp://x"))
        mgr.fetch_camera_proxy_frames_for_camera = AsyncMock(return_value=[])
        fake_stream = mgr._streams["cam_1"]
        mgr._eval_one = AsyncMock()
        await mgr.on_ha_motion_trigger("binary_sensor.m")
        fake_stream.get_recent_frames.assert_called_once()
        mgr._eval_one.assert_awaited_once()

    async def test_no_frames_skips_eval(self, _mock_stream_class):
        mgr = _mgr()
        await mgr._spawn(_row(motion_entity="binary_sensor.m"))
        mgr.fetch_camera_proxy_frames_for_camera = AsyncMock(return_value=[])
        mgr._streams["cam_1"].get_recent_frames = MagicMock(return_value=[])
        mgr._eval_one = AsyncMock()
        with patch("app.services.camera_manager.logger") as lg:
            await mgr.on_ha_motion_trigger("binary_sensor.m")
        mgr._eval_one.assert_not_awaited()
        lg.warning.assert_called_once()


class TestHaStateEvent:
    async def test_binary_sensor_on_triggers(self, _mock_stream_class):
        mgr = _mgr()
        await mgr._spawn(_row(motion_entity="binary_sensor.m"))
        mgr.on_ha_motion_trigger = AsyncMock()
        await mgr._on_ha_state_event({
            "event_type": "state_changed", "entity_id": "binary_sensor.m",
            "domain": "binary_sensor",
            "new_state": {"state": "on", "attributes": {}},
        })
        mgr.on_ha_motion_trigger.assert_awaited_once_with("binary_sensor.m")

    async def test_binary_sensor_off_ignored(self, _mock_stream_class):
        mgr = _mgr()
        await mgr._spawn(_row(motion_entity="binary_sensor.m"))
        mgr.on_ha_motion_trigger = AsyncMock()
        await mgr._on_ha_state_event({
            "event_type": "state_changed", "entity_id": "binary_sensor.m",
            "domain": "binary_sensor", "new_state": {"state": "off"},
        })
        mgr.on_ha_motion_trigger.assert_not_awaited()

    async def test_camera_entity_updates_availability(self, _mock_stream_class):
        mgr = _mgr()
        await mgr._spawn(_row(cam_entity="camera.door"))
        assert mgr._ha_availability["cam_1"] is False  # setdefault 初值
        await mgr._on_ha_state_event({
            "event_type": "state_changed", "entity_id": "camera.door",
            "domain": "camera", "new_state": {"state": "streaming"},
        })
        assert mgr._ha_availability["cam_1"] is True
        await mgr._on_ha_state_event({
            "event_type": "state_changed", "entity_id": "camera.door",
            "domain": "camera", "new_state": {"state": "unavailable"},
        })
        assert mgr._ha_availability["cam_1"] is False


class TestHaOnlineSemantics:
    async def test_get_state_ha_only_follows_availability(self, _mock_stream_class):
        mgr = _mgr()
        await mgr._spawn(_row(cam_entity="camera.door"))
        mgr._ha_availability["cam_1"] = True
        st = mgr.get_state("cam_1")
        assert st["camera_opened"] is True and st["source"] == "ha"
        mgr._ha_availability["cam_1"] = False
        assert mgr.get_state("cam_1")["camera_opened"] is False

    async def test_list_cameras_includes_ha_only(self, _mock_stream_class):
        mgr = _mgr()
        await mgr._spawn(_row(cid="cam_ha", cam_entity="camera.door", name="门口"))
        mgr._ha_availability["cam_ha"] = True
        entry = next(c for c in mgr.list_cameras() if c["id"] == "cam_ha")
        assert entry["online"] is True and entry["name"] == "门口"

    async def test_hybrid_state_comes_from_stream(self, _mock_stream_class):
        """混合路在线以常驻流为准（RTSP 断了要报，不被 HA 可用性掩盖）。"""
        mgr = _mgr()
        await mgr._spawn(_row(cam_entity="camera.door", rtsp="rtsp://x"))
        fake_stream = mgr._streams["cam_1"]
        fake_stream.get_state = MagicMock(
            return_value={"camera_id": "cam_1", "camera_opened": False})
        mgr._ha_availability["cam_1"] = True
        assert mgr.get_state("cam_1")["camera_opened"] is False


class TestProxyFrameFetch:
    async def test_fetch_decodes_jpeg(self):
        mgr = _mgr()
        client = MagicMock()
        client.camera_proxy = AsyncMock(return_value=_jpeg_bytes())
        mgr._ha_service._client = client
        frames = await mgr.fetch_camera_proxy_frames("camera.door", n=2)
        assert len(frames) == 2 and frames[0].shape == (8, 8, 3)
        assert client.camera_proxy.await_count == 2

    async def test_fetch_failure_returns_empty(self):
        mgr = _mgr()
        client = MagicMock()
        client.camera_proxy = AsyncMock(side_effect=RuntimeError("ha down"))
        mgr._ha_service._client = client
        assert await mgr.fetch_camera_proxy_frames("camera.door", n=3) == []

    async def test_fetch_for_camera_without_entity_returns_empty(self):
        mgr = _mgr()
        assert await mgr.fetch_camera_proxy_frames_for_camera("cam_x", 2) == []


class TestPtzHaPath:
    def _svc(self, cam_entity="camera.door", ptz_enabled=1):
        return PtzService(camera_id="cam_1", config={
            "ptz_enabled": ptz_enabled,
            "ptz_speed": 0.5,
            "ha_camera_entity": cam_entity,
        })

    def _ha_client_ok(self):
        client = MagicMock()
        client.call_service = AsyncMock(return_value={})
        ptz_registry.set_ha_client(client)
        return client

    async def test_move_via_ha_service(self):
        svc = self._svc()
        client = self._ha_client_ok()
        res = await svc.move("up")
        assert res == {"success": True, "direction": "up", "via": "ha"}
        client.call_service.assert_awaited_once_with(
            "onvif", "ptz", entity_id="camera.door", data={"tilt": 0.5, "pan": 0.0})

    async def test_stop_via_ha_service(self):
        svc = self._svc()
        client = self._ha_client_ok()
        res = await svc.stop()
        assert res == {"success": True, "via": "ha"}
        client.call_service.assert_awaited_once_with(
            "onvif", "ptz", entity_id="camera.door",
            data={"tilt": 0.0, "pan": 0.0, "zoom": 0.0})

    async def test_step_via_ha_auto_stops(self):
        svc = self._svc()
        client = self._ha_client_ok()
        res = await svc.step("left", duration_ms=0)
        assert res == {"success": True, "via": "ha"}
        # move + stop 各一次
        assert client.call_service.await_count == 2

    async def test_ha_failure_falls_back_to_local(self):
        svc = self._svc()
        client = MagicMock()
        client.call_service = AsyncMock(side_effect=RuntimeError("no onvif integration"))
        ptz_registry.set_ha_client(client)
        svc._ensure_connected = AsyncMock(return_value=True)
        svc._stop_locked = AsyncMock()
        svc._continuous_move_locked = AsyncMock()
        res = await svc.move("up")
        assert res == {"success": True, "direction": "up"}
        svc._continuous_move_locked.assert_awaited_once()

    async def test_ptz_disabled_disables_ha_path(self):
        svc = self._svc(ptz_enabled=0)
        assert svc._ha_entity() == ""
        client = self._ha_client_ok()
        svc._ensure_connected = AsyncMock(return_value=False)
        res = await svc.move("up")
        assert res["success"] is False
        client.call_service.assert_not_awaited()

    async def test_no_entity_uses_local_only(self):
        svc = self._svc(cam_entity="")
        svc._ensure_connected = AsyncMock(return_value=False)
        assert await svc.move("up") == {"success": False, "error": "PTZ not connected"}


class TestCameraRoutesHa:
    async def test_ha_entities_endpoint(self):
        from app.routes import camera_routes
        c = MagicMock()
        c.ha_service.get_entities_by_domains = AsyncMock(return_value=[
            {"entity_id": "camera.door", "domain": "camera", "name": "Door", "state": "streaming", "device_class": None},
            {"entity_id": "binary_sensor.door_motion", "domain": "binary_sensor", "name": "M", "state": "off", "device_class": "motion"},
            {"entity_id": "binary_sensor.window", "domain": "binary_sensor", "name": "W", "state": "off", "device_class": "window"},
        ])
        with patch.object(camera_routes, "get_container", return_value=c):
            res = await camera_routes.camera_ha_entities()
        data = res.data
        assert [e["entity_id"] for e in data["cameras"]] == ["camera.door"]
        # motion 类排前，window 殿后
        assert data["motion_sensors"][0]["entity_id"] == "binary_sensor.door_motion"
        assert data["motion_sensors"][1]["entity_id"] == "binary_sensor.window"

    async def test_test_stream_ha_branch(self):
        from app.routes import camera_routes
        c = MagicMock()
        c.camera_manager.fetch_camera_proxy_frames = AsyncMock(return_value=[np.zeros((4, 4))])
        with patch.object(camera_routes, "get_container", return_value=c):
            res = await camera_routes.test_stream("cam_1", {
                "rtsp_url": "", "ha_camera_entity": "camera.door"})
        assert res.data["ok"] is True

    async def test_test_stream_ha_branch_no_frame(self):
        from app.routes import camera_routes
        c = MagicMock()
        c.camera_manager.fetch_camera_proxy_frames = AsyncMock(return_value=[])
        with patch.object(camera_routes, "get_container", return_value=c):
            res = await camera_routes.test_stream("cam_1", {
                "rtsp_url": "", "ha_camera_entity": "camera.door"})
        assert res.data["ok"] is False
