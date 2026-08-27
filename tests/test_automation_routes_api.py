"""/api/automation/* 路由 HTTP 层测试。

此前只有 automation_service / automation_agent 的服务层测试，路由层
（参数钳制、config 落盘、agent 未启动分支）零覆盖。
容器注入走 app.dependency_overrides（Depends(get_container) 在装饰期捕获
函数引用，patch 模块属性无效）；config 由 conftest 重定向到 tmp 文件，
路由写完可即时回读。全局 api_token_guard 中间件要求所有 /api 请求带 token。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.container import get_container
from app.core.auth import create_access_token
from app.core.config import get_config
from app.main import app


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token('u1', 'tester')}"}


class _FakeAgent:
    """记录调用的 AutomationAgent 桩。"""

    def __init__(self, running=True):
        self._running = running
        self._eval_count = 5
        self._nonvision_eval_count = 2
        self.calls: list[tuple] = []

    def set_silent_enabled(self, v):
        self.calls.append(("set_silent_enabled", v))

    def set_silent_interval(self, s):
        self.calls.append(("set_silent_interval", s))

    def set_nonvision_silent_enabled(self, v):
        self.calls.append(("set_nonvision_silent_enabled", v))

    def set_nonvision_silent_interval(self, s):
        self.calls.append(("set_nonvision_silent_interval", s))


def _fake_camera():
    cm = MagicMock()
    cm.set_motion_threshold = MagicMock()
    cm.set_camera_vl_display_enabled = MagicMock()
    return cm


@pytest.fixture()
def client():
    """把伪造容器绑进依赖覆盖；测试内先调 bind 建容器再发请求。"""
    def bind(agent, camera_manager=None, service=None):
        cont = MagicMock()
        cont.automation_agent_ref = [agent]
        cont.camera_manager = camera_manager
        cont.automation_service = service
        app.dependency_overrides[get_container] = lambda: cont
        return TestClient(app)

    yield bind
    app.dependency_overrides.pop(get_container, None)


# --------------- GET /automation/status ---------------

def test_status_requires_auth(client):
    tc = client(_FakeAgent())
    assert tc.get("/api/automation/status").status_code == 401


def test_status_reports_config_and_agent_state(client):
    agent = _FakeAgent(running=True)
    svc = SimpleNamespace(_vision_eval_count=3, _context_eval_count=7)
    tc = client(agent, service=svc)

    resp = tc.get("/api/automation/status", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["running"] is True
    # 有 service 时读 service 计数，不读 agent 计数
    assert data["eval_count"] == 3
    assert data["nonvision_eval_count"] == 7


def test_status_counts_fall_back_to_agent_when_no_service(client):
    tc = client(_FakeAgent())

    data = tc.get("/api/automation/status", headers=_auth()).json()["data"]
    assert data["eval_count"] == 5
    assert data["nonvision_eval_count"] == 2


def test_status_agent_none_not_started(client):
    tc = client(None)

    data = tc.get("/api/automation/status", headers=_auth()).json()["data"]
    assert data["running"] is False
    assert data["eval_count"] == 0


# --------------- POST /automation/silent ---------------

def test_silent_enabled_vision_scope(client):
    agent = _FakeAgent()
    tc = client(agent)

    resp = tc.post("/api/automation/silent", json={"enabled": False}, headers=_auth())
    assert resp.status_code == 200
    assert ("set_silent_enabled", False) in agent.calls
    assert get_config("automation.silent_eval_enabled") is False
    assert resp.json()["data"]["saved"] is True


def test_silent_interval_clamped_to_min(client):
    agent = _FakeAgent()
    tc = client(agent)

    resp = tc.post("/api/automation/silent", json={"interval_seconds": 1}, headers=_auth())
    assert resp.status_code == 200
    # 1s 钳到下限 5s
    assert ("set_silent_interval", 5) in agent.calls
    assert get_config("automation.silent_eval_interval_seconds") == 5


def test_silent_nonvision_scope_routes_separately(client):
    agent = _FakeAgent()
    tc = client(agent)

    resp = tc.post(
        "/api/automation/silent",
        json={"enabled": True, "interval_seconds": 60, "scope": "nonvision"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert ("set_nonvision_silent_enabled", True) in agent.calls
    assert ("set_nonvision_silent_interval", 60) in agent.calls
    # 不应误写视觉管道
    assert not any(c[0].startswith("set_silent_") for c in agent.calls)
    assert get_config("automation.nonvision_silent_enabled") is True
    assert get_config("automation.nonvision_silent_interval_seconds") == 60


def test_silent_no_agent_returns_not_started(client):
    tc = client(None)

    body = tc.post("/api/automation/silent", json={"enabled": True}, headers=_auth()).json()
    assert body["code"] == "not_started"
    assert body["data"]["saved"] is False


# --------------- POST /automation/vision-recognizer ---------------

def test_vision_recognizer_toggles_camera_display(client):
    cm = _fake_camera()
    tc = client(_FakeAgent(), camera_manager=cm)

    resp = tc.post("/api/automation/vision-recognizer", json={"enabled": False}, headers=_auth())
    assert resp.status_code == 200
    cm.set_camera_vl_display_enabled.assert_called_once_with(False)
    assert get_config("automation.camera_vl_display_enabled") is False


def test_vision_recognizer_no_camera_returns_not_started(client):
    tc = client(_FakeAgent(), camera_manager=None)

    resp = tc.post("/api/automation/vision-recognizer", json={"enabled": True}, headers=_auth())
    assert resp.json()["code"] == "not_started"


# --------------- POST /automation/cooldown ---------------

def test_cooldown_updates_default_with_clamp(client):
    tc = client(_FakeAgent())

    resp = tc.post("/api/automation/cooldown", json={"cooldown_seconds": 30}, headers=_auth())
    assert resp.status_code == 200
    assert get_config("automation.default_cooldown_seconds") == 30

    # 下限钳制：0 → 1
    tc.post("/api/automation/cooldown", json={"cooldown_seconds": 0}, headers=_auth())
    assert get_config("automation.default_cooldown_seconds") == 1


# --------------- POST /automation/dhash-threshold ---------------

def test_dhash_threshold_persists_and_pushes_to_camera(client):
    cm = _fake_camera()
    tc = client(_FakeAgent(), camera_manager=cm)

    resp = tc.post("/api/automation/dhash-threshold", json={"threshold": 42}, headers=_auth())
    assert resp.status_code == 200
    cm.set_motion_threshold.assert_called_once_with(42)
    assert get_config("vision.motion_threshold") == 42


def test_dhash_threshold_clamps_to_hash_size_square(client):
    # conftest 未配 vision.motion_hash_size → 默认 16，上限 256
    tc = client(_FakeAgent(), camera_manager=None)

    resp = tc.post("/api/automation/dhash-threshold", json={"threshold": 99999}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["data"]["motion_threshold"] == 256
    assert get_config("vision.motion_threshold") == 256
