"""camera_routes 单测(Task 6)。

验证 /api/cameras 全套 REST + /api/ha/areas 端点。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import camera_routes


def _mock_container():
    c = MagicMock()
    c.camera_manager = MagicMock()
    c.camera_manager.create_camera = AsyncMock(return_value={"id": "cam_new", "name": "x"})
    c.camera_manager.update_camera = AsyncMock(return_value={"id": "cam_new"})
    c.camera_manager.delete_camera = AsyncMock(return_value=True)
    c.camera_manager.list_cameras = MagicMock(return_value=[
        {"id": "cam_a", "name": "客厅", "area": "客厅", "online": True}
    ])
    c.camera_manager.get_state = MagicMock(return_value={"camera_id": "cam_a", "online": True})
    c.camera_manager.enable_display = AsyncMock(return_value=None)
    c.camera_manager.disable_display = AsyncMock(return_value=None)
    c.vision_service = MagicMock()
    c.vision_service.get_vision_focuses = MagicMock(return_value=[])
    c.vision_service.add_focus = MagicMock(return_value={"id": "f1", "text": "人"})
    c.vision_service.delete_focus = MagicMock(return_value=True)
    c.ha_service = MagicMock()
    c.ha_service.get_areas = AsyncMock(return_value=[{"area_id": "keting", "name": "客厅"}])
    c.db = MagicMock()
    c.db.cameras_get = AsyncMock(return_value={"id": "cam_a", "name": "客厅"})
    c.ptz_registry = MagicMock()
    c.ptz_registry.get = AsyncMock(return_value=MagicMock())
    c.discovery_service = MagicMock()
    c.discovery_service.find_and_apply = AsyncMock(return_value="1.2.3.4")
    c.discovery_service.apply_found_ip = AsyncMock(return_value=None)
    return c


@pytest.fixture
def client(monkeypatch):
    cont = _mock_container()
    monkeypatch.setattr(camera_routes, "get_container", lambda: cont)
    app = FastAPI()
    app.include_router(camera_routes.router, prefix="/api")
    return TestClient(app), cont


class TestCamerasCrud:
    def test_list_cameras(self, client):
        c, _ = client
        r = c.get("/api/cameras")
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1 and data[0]["id"] == "cam_a"

    def test_create_camera(self, client):
        c, cont = client
        r = c.post("/api/cameras", json={"name": "新摄像头", "source_type": "rtsp", "rtsp_url": "rtsp://x"})
        assert r.status_code == 200
        cont.camera_manager.create_camera.assert_called_once()

    def test_get_camera(self, client):
        c, _ = client
        r = c.get("/api/cameras/cam_a")
        assert r.status_code == 200
        assert r.json()["data"]["id"] == "cam_a"

    def test_delete_camera(self, client):
        c, cont = client
        r = c.delete("/api/cameras/cam_a")
        assert r.status_code == 200
        cont.camera_manager.delete_camera.assert_called_once_with("cam_a")


class TestDisplayEndpoints:
    def test_enable_display(self, client):
        c, cont = client
        r = c.post("/api/cameras/cam_a/display/enable")
        assert r.status_code == 200
        cont.camera_manager.enable_display.assert_called_once_with("cam_a")

    def test_disable_display(self, client):
        c, cont = client
        r = c.post("/api/cameras/cam_a/display/disable")
        assert r.status_code == 200
        cont.camera_manager.disable_display.assert_called_once_with("cam_a")


class TestFocuses:
    def test_list_focuses(self, client):
        c, _ = client
        r = c.get("/api/cameras/cam_a/focuses")
        assert r.status_code == 200

    def test_add_focus(self, client):
        c, cont = client
        r = c.post("/api/cameras/cam_a/focuses", json={"text": "人"})
        assert r.status_code == 200
        cont.vision_service.add_focus.assert_called_once()
        # camera_id 透传
        args, kwargs = cont.vision_service.add_focus.call_args
        assert kwargs.get("camera_id") == "cam_a" or "cam_a" in args


class TestAreasEndpoint:
    def test_get_areas(self, client):
        c, cont = client
        r = c.get("/api/ha/areas")
        assert r.status_code == 200
        assert r.json()["data"][0]["name"] == "客厅"


class TestDiscoveryEndpoints:
    def test_find(self, client):
        c, cont = client
        r = c.post("/api/cameras/cam_a/discovery/find")
        assert r.status_code == 200
        cont.discovery_service.find_and_apply.assert_called_once_with("cam_a")
