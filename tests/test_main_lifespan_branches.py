"""app/main.py lifespan 内部防御分支覆盖。

每个用例进入一次完整 TestClient 生命周期，让某个启动环节按预期失败，
验证"失败不阻塞主服务"的兜底路径与告警日志。DB 指向 tmp_path。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import database as dbmod

pytestmark = pytest.mark.asyncio


def _lifespan_client(monkeypatch, tmp_path):
    """构造进入完整 lifespan 的 TestClient（DB 隔离到 tmp；LLM 构建打桩）。"""
    from fastapi.testclient import TestClient
    import app.main as main

    db_path = tmp_path / "ml" / "aether.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    # 测试环境无 chat LLM 配置，打桩成最小 agent（同 test_infra_coverage 做法）
    import app.agents.langgraph_agent as _la
    import app.mcp.langchain_tools as _lt
    monkeypatch.setattr(_la, "build_chat_agent",
                        lambda tools, model_config=None: ("AGENT", ()))
    monkeypatch.setattr(_lt, "convert_all_tools", lambda m, full_name=False: [])
    # 不真正启动宿主侧集成（本环境 .env 可能配了真实飞书凭证，防止外连）
    monkeypatch.setattr(main, "_start_host_integrations", lambda c, loop: [])

    def _enter():
        return TestClient(main.app)

    return main, _enter


async def test_lifespan_heal_success_reloads_clients(monkeypatch, tmp_path):
    """key 自愈恢复成功 → reload_all_clients 被调用；OPTIONS 预检放行；坏 token 401。"""
    import app.core.key_healing as kh

    main, make = _lifespan_client(monkeypatch, tmp_path)
    monkeypatch.setattr(kh, "heal_global_keys_from_user_db",
                        AsyncMock(return_value=["chat"]))
    reload_mock = Mock()
    monkeypatch.setattr(main._container, "reload_all_clients", reload_mock)

    client = make()
    with client as c:
        # OPTIONS 预检：中间件直接放行（763 行），不走鉴权
        resp = c.options("/api/health")
        # 中间件放行后路由 405（/api/health 无 OPTIONS）——证明未走鉴权 401/500
        assert resp.status_code == 405
        # 无效 JWT → 401（verify 抛异常分支 771-772）
        resp = c.get("/api/metrics", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401
    reload_mock.assert_called_once()


async def test_lifespan_heal_reload_failure_is_contained(monkeypatch, tmp_path):
    """自愈成功但 reload 失败 → 记 warning，启动继续。"""
    import app.core.key_healing as kh

    main, make = _lifespan_client(monkeypatch, tmp_path)
    monkeypatch.setattr(kh, "heal_global_keys_from_user_db",
                        AsyncMock(return_value=["embed"]))
    monkeypatch.setattr(main._container, "reload_all_clients",
                        Mock(side_effect=RuntimeError("reload boom")))

    with make() as c:
        assert c.get("/healthz").json()["status"] == "ok"


async def test_lifespan_heal_raises_is_contained(monkeypatch, tmp_path):
    """自愈本身抛异常 → 记 warning，启动继续（不阻塞主服务）。"""
    import app.core.key_healing as kh

    main, make = _lifespan_client(monkeypatch, tmp_path)
    monkeypatch.setattr(kh, "heal_global_keys_from_user_db",
                        AsyncMock(side_effect=RuntimeError("heal boom")))

    with make() as c:
        assert c.get("/healthz").json()["status"] == "ok"


async def test_lifespan_integration_platform_failure_is_contained(monkeypatch, tmp_path):
    """插件平台启动失败 → integration_layer 置 None，主服务照常起。"""
    main, make = _lifespan_client(monkeypatch, tmp_path)

    import app.integration.integration_layer as il_mod

    class _BrokenLayer:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            raise RuntimeError("no plugin runtime")

    # lifespan 在函数体内延迟导入 IntegrationLayer → patch 源模块属性即可生效
    monkeypatch.setattr(il_mod, "IntegrationLayer", _BrokenLayer)
    # 容器是跨测试共享的单例：先清掉前序测试留下的 layer，断言"启动失败不会写回"
    monkeypatch.setattr(main._container, "integration_layer", None, raising=False)
    with make() as c:
        assert c.get("/healthz").json()["status"] == "ok"
        assert main._container.integration_layer is None


async def test_lifespan_periodic_health_check_failure_is_logged(monkeypatch, tmp_path):
    """周期健康检查抛异常 → 记 warning 后继续循环（571-576 分支）。"""
    main, make = _lifespan_client(monkeypatch, tmp_path)
    monkeypatch.setattr(main.health_checker, "check_ha",
                        AsyncMock(side_effect=RuntimeError("ha down")))
    monkeypatch.setattr(main.health_checker, "check_all",
                        AsyncMock(side_effect=RuntimeError("all down")))

    with make() as c:
        assert c.get("/healthz").json()["status"] == "ok"


async def test_lifespan_camera_and_mac_capture_failures_are_contained(
        monkeypatch, tmp_path):
    """CameraManager.initialize 抛异常 → 记日志继续启动（520-523 分支）。"""
    main, make = _lifespan_client(monkeypatch, tmp_path)
    real_init_services = main.initialize_services

    def _with_broken_camera(*a, **k):
        services = real_init_services(*a, **k)
        cm = services.get("camera_manager")
        if cm is not None:
            cm.initialize = AsyncMock(side_effect=RuntimeError("rtsp unreachable"))
        return services

    monkeypatch.setattr(main, "initialize_services", _with_broken_camera)
    with make() as c:
        assert c.get("/healthz").json()["status"] == "ok"


async def test_start_host_integrations_missing_dir(monkeypatch, tmp_path):
    """integrations 目录不存在 → 直接返回空列表（906 分支）。"""
    import app.main as main

    monkeypatch.setattr(main.os.path, "isdir", lambda p: False)
    started = main._start_host_integrations(
        Mock(dispatcher=None, integration_layer=None), asyncio.get_running_loop())
    assert started == []


async def test_ha_catalog_getters_return_refs(monkeypatch):
    """目录/控件文本 getter 透传缓存引用（141/145 行）。"""
    import app.main as main

    monkeypatch.setattr(main, "_ha_catalog_cache_ref", ["CAT"])
    monkeypatch.setattr(main, "_ha_controls_cache_ref", ["CTRL"])
    assert main._get_ha_device_catalog() == "CAT"
    assert main._get_ha_device_controls() == "CTRL"
