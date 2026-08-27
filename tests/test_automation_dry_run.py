"""automation_service 演练开关测试：虚拟摄像头默认演练，real_exec 才真实执行。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.services.automation_service import AutomationService


def _rule(camera_id="vcam_test-camera", actions=None):
    return {
        "id": "r1", "name": "测试规则", "type": "vision",
        "condition": "画面中出现人",
        "camera_id": camera_id,
        "enabled": True, "cooldown_seconds": 0,
        "last_triggered_at": 0.0,
        "actions": actions if actions is not None else [{
            "mcp_tool_name": "ha_devices___call_service",
            "mcp_tool_input": {"domain": "light", "service": "turn_on",
                               "entity_id": "light.a", "data": {}},
        }],
    }


def _make_service(camera_manager=None):
    registry = MagicMock()
    registry.list_rules.return_value = []
    executor = MagicMock()
    executor.resolve_tool_name = lambda n: n
    executor.execute_tool_by_name = AsyncMock(return_value={"success": True})
    svc = AutomationService(rule_registry=registry, tool_executor=executor,
                            vision_service=None, ha_service=None)
    if camera_manager is not None:
        svc.set_camera_manager(camera_manager)
    return svc, executor


class _FakeCameraManager:
    def __init__(self, virtual_ids, flags):
        self._virtual_ids = set(virtual_ids)
        self._flags = flags

    def is_virtual_camera(self, camera_id):
        return camera_id in self._virtual_ids

    def get_virtual_flag(self, camera_id, key, default=None):
        if camera_id not in self._virtual_ids:
            return default
        return self._flags.get(camera_id, {}).get(key, default)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_virtual_camera_default_is_dry_run():
    """虚拟摄像头 + 未开 real_exec → ToolExecutor 不被调用，返回 dry_run 标记。"""
    cm = _FakeCameraManager(["vcam_test-camera"], {"vcam_test-camera": {}})
    svc, executor = _make_service(cm)
    rule = _rule()

    # dry_run 由 _run_actions 按摄像头判定后传入；这里走完整路径
    results = _run(svc._run_actions(rule, now=0.0, camera_id="vcam_test-camera"))
    assert len(results) == 1
    assert results[0]["result"]["dry_run"] is True
    executor.execute_tool_by_name.assert_not_awaited()


def test_virtual_camera_real_exec_runs():
    """开了 real_exec → 与生产同出口真实执行。"""
    cm = _FakeCameraManager(["vcam_test-camera"],
                            {"vcam_test-camera": {"real_exec": True}})
    svc, executor = _make_service(cm)
    rule = _rule()

    results = _run(svc._run_actions(rule, now=0.0, camera_id="vcam_test-camera"))
    assert len(results) == 1
    assert "dry_run" not in results[0]["result"]
    executor.execute_tool_by_name.assert_awaited_once()


def test_real_camera_unchanged():
    """真实摄像头恒真实执行（生产行为不变）。"""
    cm = _FakeCameraManager(["vcam_test-camera"], {})
    svc, executor = _make_service(cm)

    results = _run(svc._run_actions(_rule(camera_id="cam_real"), now=0.0, camera_id="cam_real"))
    assert len(results) == 1
    assert "dry_run" not in results[0]["result"]
    executor.execute_tool_by_name.assert_awaited_once()


def test_no_camera_manager_is_real_execution():
    """camera_manager 未注入（旧部署/单测环境）→ 恒真实执行。"""
    svc, executor = _make_service(None)
    results = _run(svc._run_actions(_rule(), now=0.0, camera_id="vcam_test-camera"))
    assert len(results) == 1
    assert "dry_run" not in results[0]["result"]
