"""camera.* 反向 RPC 权限与宿主方法注册测试（in-process registry，不 spawn）。"""
from __future__ import annotations

import asyncio

import pytest

from app.integration.host_registry import HostMethodRegistry
from app.integration.integration_layer import IntegrationLayer
from app.integration.rpc_protocol import (
    METHOD_HOST_CAM_PUSH,
    METHOD_HOST_CAM_REGISTER,
    METHOD_HOST_CAM_UNREGISTER,
)
from app.integration.schema import Capability, Manifest

pytestmark = pytest.mark.slow


def _manifest(permissions):
    return Manifest(
        id="test-camera", name="t", version="1", aether_api_version="1",
        capabilities=[Capability(type="output_sink", id="x")],
        permissions=permissions,
    )


class _FakeCameraManager:
    def __init__(self):
        self.registered = []

    async def register_virtual_camera(self, plugin_id, spec):
        self.registered.append((plugin_id, spec))
        return {"camera_id": f"vcam_{plugin_id}", "name": spec.get("name", "")}

    async def unregister_plugin_cameras(self, plugin_id):
        return plugin_id in [p for p, _ in self.registered]

    def push_frame(self, camera_id, jpeg_b64):
        return {"ok": True, "dropped": False}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_layer(cm):
    return IntegrationLayer(
        plugin_dir="integrations", host_deps={"camera_manager": cm},
    )


def test_register_with_camera_permission():
    cm = _FakeCameraManager()
    layer = _make_layer(cm)
    m = _manifest(["camera"])
    result = _run(layer._host_registry.dispatch(
        m, METHOD_HOST_CAM_REGISTER, {"_plugin_id": "test-camera", "spec": {"name": "测试"}}))
    assert result["camera_id"] == "vcam_test-camera"
    assert cm.registered == [("test-camera", {"name": "测试"})]


def test_register_without_permission_denied():
    cm = _FakeCameraManager()
    layer = _make_layer(cm)
    m = _manifest([])  # 无 camera 权限
    with pytest.raises(PermissionError):
        _run(layer._host_registry.dispatch(
            m, METHOD_HOST_CAM_REGISTER, {"_plugin_id": "test-camera", "spec": {}}))


def test_push_frame_routes():
    cm = _FakeCameraManager()
    layer = _make_layer(cm)
    m = _manifest(["camera"])
    result = _run(layer._host_registry.dispatch(
        m, METHOD_HOST_CAM_PUSH,
        {"camera_id": "vcam_test-camera", "jpeg_b64": "e30="}))
    assert result["ok"] is True


def test_unregister_routes():
    cm = _FakeCameraManager()
    layer = _make_layer(cm)
    m = _manifest(["camera"])
    _run(layer._host_registry.dispatch(
        m, METHOD_HOST_CAM_REGISTER, {"_plugin_id": "test-camera", "spec": {}}))
    result = _run(layer._host_registry.dispatch(
        m, METHOD_HOST_CAM_UNREGISTER, {"_plugin_id": "test-camera"}))
    assert result["ok"] is True


def test_no_camera_manager_no_methods_registered():
    """host_deps 无 camera_manager → camera.* 未注册 → RuntimeError 未知方法。"""
    layer = IntegrationLayer(plugin_dir="integrations", host_deps={})
    m = _manifest(["camera"])
    with pytest.raises(RuntimeError):
        _run(layer._host_registry.dispatch(
            m, METHOD_HOST_CAM_REGISTER, {"_plugin_id": "test-camera", "spec": {}}))
