"""HTTP 端到端冒烟测试 — 启动真实 FastAPI app，打真实 HTTP 请求。

验证现有路由函数级测试覆盖不到的链路：
- 中间件链（api_token_guard / global_rate_limit / request_tracing）
- 依赖注入（get_container → 真实 AppContainer）
- 路由注册（include_router 是否把路由挂上来）
- ApiResponse 序列化（HTTP 层 model_dump）

用 TestClient(app) 但不进入 lifespan，避免触发 Database.init / 摄像头启动等重副作用。
认证用 auth.create_access_token 造真实 JWT，绕开测试数据库。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token


@pytest.fixture(scope="module")
def client():
    """启动 FastAPI app（不进入 lifespan，跳过重启动副作用）。"""
    import app.main as m
    with TestClient(m.app) as c:
        yield c


def _auth_header() -> dict[str, str]:
    """造一个合法 access token，绕开 DB。"""
    token = create_access_token(user_id="test-user", username="tester")
    return {"Authorization": f"Bearer {token}"}


class TestMiddlewareAuthGuard:
    """api_token_guard 中间件：未认证 /api/* → 401。"""

    def test_unauthenticated_health_returns_401(self, client: TestClient):
        resp = client.get("/api/health")
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "unauthorized"

    def test_authenticated_health_passes_guard(self, client: TestClient):
        """带合法 JWT → 穿过 guard 到达路由（health 路由不依赖 lifespan）。"""
        resp = client.get("/api/health", headers=_auth_header())
        # health 路由在 lifespan 未启动时仍可访问（camera_stream 对象存在）
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "ok"
        assert "data" in body
        assert "llm_enabled" in body["data"]

    def test_invalid_token_returns_401(self, client: TestClient):
        resp = client.get("/api/health", headers={"Authorization": "Bearer not.a.real.token"})
        assert resp.status_code == 401


class TestMiddlewareTracing:
    """request_tracing 中间件：注入 X-Request-ID。"""

    def test_response_has_request_id_header(self, client: TestClient):
        resp = client.get("/api/health", headers=_auth_header())
        assert "X-Request-ID" in resp.headers
        assert len(resp.headers["X-Request-ID"]) >= 8

    def test_custom_request_id_is_preserved(self, client: TestClient):
        rid = "my-trace-id-1234"
        resp = client.get("/api/health", headers={**_auth_header(), "X-Request-ID": rid})
        assert resp.headers["X-Request-ID"] == rid


class TestMetricsEndpoint:
    """/api/metrics — 验证 metrics_service + request_tracing 联动。"""

    def test_metrics_returns_snapshot(self, client: TestClient):
        resp = client.get("/api/metrics", headers=_auth_header())
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "http" in data
        assert "tools" in data
        assert "llm" in data
        # 打过几次请求后 total 应 > 0
        assert data["http"]["total"] >= 1

    def test_request_counted_in_metrics(self, client: TestClient):
        # 先取一次快照
        before = client.get("/api/metrics", headers=_auth_header()).json()["data"]["http"]["total"]
        # 再打一个请求
        client.get("/api/health", headers=_auth_header())
        after = client.get("/api/metrics", headers=_auth_header()).json()["data"]["http"]["total"]
        assert after > before


class TestRouteRegistration:
    """验证关键路由确实挂到了 app 上（include_router 正确）。"""

    @pytest.mark.parametrize("path,method", [
        ("/api/health", "GET"),
        ("/api/metrics", "GET"),
        ("/api/state", "GET"),
        ("/api/auth/login", "POST"),
        ("/api/scheduled-tasks", "GET"),
        ("/api/sg/status", "GET"),
    ])
    def test_route_registered(self, client: TestClient, path: str, method: str):
        """这些路由应该存在（不是 404）。未认证会 401，但不是 404。"""
        resp = client.request(method, path, headers=_auth_header())
        assert resp.status_code != 404, f"{method} {path} 未注册"
