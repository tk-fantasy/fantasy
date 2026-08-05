"""集成平台路由测试。

验证 /api/integrations 路由可达。完整 e2e（真实插件 spawn）依赖运行中的容器，
此处只验证路由注册与响应结构。真实启动验证见 docs Phase 1 验收清单。
"""

import pytest


@pytest.fixture
def client(monkeypatch):
    """构造 TestClient，但禁用 IntegrationLayer 启动（避免 spawn 子进程）。"""
    monkeypatch.setattr("app.core.config.get_config", _stub_config)
    # 延迟 import，让 monkeypatch 生效
    from app.main import app
    from fastapi.testclient import TestClient
    return TestClient(app)


def _stub_config(path, default=None):
    """禁用集成平台，避免测试启动真实插件。"""
    if path == "integration.enabled":
        return False
    if path == "integration.plugin_dir":
        return "integrations"
    if path == "integration.api_version":
        return "1"
    if path == "integration.default_rpc_timeout":
        return 30.0
    if path == "integration.max_restarts":
        return 3
    return default


def test_list_integrations_returns_200_when_disabled(client):
    """集成平台禁用时，/api/integrations 返回空列表 + enabled=False。"""
    with client:
        resp = client.get("/api/integrations")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["enabled"] is False
        assert data["plugins"] == []
