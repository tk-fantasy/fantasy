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

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token


@pytest.fixture(scope="module")
def client():
    """启动 FastAPI app，进入完整 lifespan（health/中间件依赖启动期状态）。

    注意：lifespan 会提交 RAG 后台索引构建到 stream 线程池；此前该线程通过
    run_coroutine_threadsafe 死等已停止的事件循环，导致 pytest 进程退出时被
    join 卡死。该问题已在 rag_service._embed_batch 内修复（投递前验循环活性
    + result 带超时），故这里可以安全使用上下文管理器。
    """
    import app.core.config as cfg

    # 模块级 fixture 先于 conftest 的函数级 CONFIG 补丁执行，此时代码读到
    # 的是未打补丁的全局 CONFIG：本地靠真实 config.json/.env 侥幸通过，
    # CI 干净环境（两者都没有）在 lifespan 的 agent 构建处直接 RuntimeError。
    # 补一颗 dummy chat key 让引导走通（smoke 测试不真调 LLM），退出恢复。
    original = cfg.CONFIG
    seeded = dict(original)
    seeded["llm_keys"] = [
        {
            "id": "smoke-chat-key",
            "base_url": "https://dummy.invalid",
            "model": "test-chat-model",
            "type": "chat",
            "chat_path": "/chat/completions",
            "api_key": "sk-test-dummy-not-a-real-key",
        },
    ]
    cfg.CONFIG = seeded
    try:
        import app.main as m
        with TestClient(m.app) as c:
            yield c
    finally:
        cfg.CONFIG = original


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
        # health 路由在 lifespan 未启动时仍可访问(不依赖摄像头)
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "ok"
        assert "data" in body
        assert "llm_enabled" in body["data"]

    def test_invalid_token_returns_401(self, client: TestClient):
        resp = client.get("/api/health", headers={"Authorization": "Bearer not.a.real.token"})
        assert resp.status_code == 401

    def test_healthz_no_auth_needed(self, client: TestClient):
        """/healthz 无认证可达（docker healthcheck 探针，不在 /api 前缀下）。"""
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "uptime_seconds" in body
        assert "version" in body

    def test_options_preflight_passes_guard(self, client: TestClient):
        """跨域预检 OPTIONS 不被 api_token_guard 401（CORS 中间件在 guard 内层，
        预检不带凭据，放行给 CORSMiddleware 应答）。"""
        resp = client.options(
            "/api/health",
            headers={"Origin": "http://192.168.1.20:5173",
                     "Access-Control-Request-Method": "GET"},
        )
        assert resp.status_code != 401

    # 注：refresh token 中间件拒绝的端到端测试见 test_auth.py 的
    # test_refresh_token_rejected_by_middleware_logic（纯逻辑，不启动 app）。
    # 此处不重复 TestClient 版本——本机摄像头副作用会让 TestClient 卡住。


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
        ("/api/auth/login", "POST"),
        ("/api/scheduled-tasks", "GET"),
        ("/api/sg/status", "GET"),
        ("/api/rules/pending/pd1/explain", "POST"),
        ("/api/rules/pending/pd1/revise", "POST"),
        ("/api/rules/pending/pd1/confirm", "POST"),
        ("/api/rules/pending/pd1/cancel", "POST"),
    ])
    def test_route_registered(self, client: TestClient, path: str, method: str):
        """这些路由应该存在（不是 404）。未认证会 401，但不是 404。"""
        resp = client.request(method, path, headers=_auth_header())
        assert resp.status_code != 404, f"{method} {path} 未注册"

    def test_legacy_single_camera_endpoints_removed(self, client: TestClient):
        """单摄兼容层已删除：/api/state 与 /api/video_feed 应 404。

        多路化后前端一律走 /api/cameras/*，这两个旧入口已无消费方
        （useCameraPreview 的回退分支同步移除）。
        """
        assert client.get("/api/state", headers=_auth_header()).status_code == 404
        assert client.get("/api/video_feed", headers=_auth_header()).status_code == 404


# ---------------------------------------------------------------------------
# 待确认规则端点 — 真 HTTP 链路（路由冲突 / 认证中间件 / 校验 / 错误体形状）
#
# 函数级测试（tests/test_rule_routes.py）已覆盖业务分支，这里只补它摸不到的部分：
# 4 段路径没被 /rules/{rule_id}/xxx 吞掉、api_token_guard 覆盖到新端点、
# AppException 真的映射成前端 _unwrap 依赖的 {code, message} + 对应状态码。
# 用 dependency_overrides 换掉容器，避免 confirm 往真实规则表/会话表写数据。
# ---------------------------------------------------------------------------

DRAFT_RULE = {
    "name": "有人开研发部灯",
    "condition": "画面里有人",
    # 已绑定摄像头的视觉规则（用户在弹窗里选过之后的状态）；缺 type 会被兜底成
    # vision，再缺 camera_id 就撞上 confirm 的强校验
    "type": "vision",
    "camera_id": "cam_1",
    "actions": [{"mcp_tool_name": "ha_devices___call_service",
                 "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                    "entity_id": "light.rd"}}],
    "summary": "有人就打开研发部灯",
}


@pytest.fixture
def pending_env(client: TestClient):
    """假容器：真 SessionState + 真 pending_rules 逻辑，桩掉落库与 LLM。"""
    import app.main as m
    from app.container import get_container
    from app.services.session_store import SessionState

    session = SessionState(session_id="s-http", request_id="r-http", user_id="test-user")
    session.model_messages = [{"role": "user", "content": "如果有人就打开研发部灯"}]
    session.pending_confirmations["pd1"] = {
        "kind": "automation_rule", "rule": dict(DRAFT_RULE), "created_at": time.time(),
    }

    saved: list[dict] = []
    stored: list = []

    class _Registry:
        def add_rule(self, rule, user_id=""):
            row = {**rule, "id": "rule-http-1", "user_id": user_id, "enabled": True}
            saved.append(row)
            return row

        def get_rule(self, rule_id):
            return None

    class _Store:
        async def get_session(self, session_id):
            return session if session_id == session.session_id else None

        async def store_session(self, s):
            stored.append(s)

    class _RuleService:
        async def explain_rule(self, rule, question, user_id=""):
            return f"答：{question}"

        async def revise_rule(self, rule, instruction, user_id=""):
            return {"rule": {**rule, "condition": "画面里有两个人"}, "summary": "改成两个人"}

    class _HA:
        async def get_states_snapshot(self):
            return [{"entity_id": "light.rd"}]

    container = SimpleNamespace(
        session_store=_Store(),
        rule_registry_service=_Registry(),
        rule_service=_RuleService(),
        ha_service=_HA(),
        ha_client_ref=[object()],
    )
    m.app.dependency_overrides[get_container] = lambda: container
    try:
        yield SimpleNamespace(session=session, saved=saved, stored=stored)
    finally:
        m.app.dependency_overrides.pop(get_container, None)


def _post(client, path, body, auth=True):
    headers = _auth_header() if auth else {}
    return client.post(path, json=body, headers=headers)


class TestPendingRuleHttp:
    """POST /api/rules/pending/{id}/* 的 HTTP 层行为。"""

    def test_unauthenticated_is_401(self, client: TestClient, pending_env):
        """新端点必须落在 api_token_guard 覆盖范围内。"""
        resp = _post(client, "/api/rules/pending/pd1/confirm",
                     {"session_id": "s-http"}, auth=False)
        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthorized"

    def test_confirm_ok_body_shape(self, client: TestClient, pending_env):
        resp = _post(client, "/api/rules/pending/pd1/confirm", {"session_id": "s-http"})

        assert resp.status_code == 200
        body = resp.json()
        # 前端 apiPost 只认 code=="ok" 时解包 data，形状必须是 ApiResponse
        assert body["code"] == "ok"
        assert body["data"] == {"rule_id": "rule-http-1", "name": "有人开研发部灯",
                                "summary": "有人就打开研发部灯"}
        assert pending_env.saved[0]["user_id"] == "test-user"
        assert pending_env.session.pending_confirmations == {}
        assert pending_env.stored == [pending_env.session]
        assert pending_env.session.model_messages[-1]["content"] == (
            "（我已通过界面确认，规则「有人开研发部灯」已创建生效）")

    def test_confirm_foreign_session_is_403_with_message(self, client: TestClient, pending_env):
        pending_env.session.user_id = "someone-else"

        resp = _post(client, "/api/rules/pending/pd1/confirm", {"session_id": "s-http"})

        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == "forbidden"
        assert body["message"]  # 前端 _unwrap 靠 message 报错
        assert pending_env.saved == []

    def test_confirm_expired_draft_is_404(self, client: TestClient, pending_env):
        """进程重启后草稿必丢（不持久化）——前端据 404 提示重说需求。"""
        pending_env.session.pending_confirmations["pd1"]["created_at"] = time.time() - 601

        resp = _post(client, "/api/rules/pending/pd1/confirm", {"session_id": "s-http"})

        assert resp.status_code == 404
        assert resp.json()["code"] == "pending_rule_not_found"
        assert "重新描述需求" in resp.json()["message"]
        assert pending_env.saved == []

    def test_confirm_unknown_session_is_404(self, client: TestClient, pending_env):
        resp = _post(client, "/api/rules/pending/pd1/confirm", {"session_id": "nope"})

        assert resp.status_code == 404
        assert resp.json()["code"] == "session_not_found"

    def test_missing_session_id_is_422(self, client: TestClient, pending_env):
        """草稿按会话存，session_id 缺失必须被 Pydantic 挡在路由外。"""
        resp = _post(client, "/api/rules/pending/pd1/confirm", {})

        assert resp.status_code == 422

    def test_explain_ok(self, client: TestClient, pending_env):
        resp = _post(client, "/api/rules/pending/pd1/explain",
                     {"session_id": "s-http", "question": "啥时候触发？"})

        assert resp.status_code == 200
        assert resp.json()["data"] == {"answer": "答：啥时候触发？"}

    def test_revise_updates_draft_in_place(self, client: TestClient, pending_env):
        resp = _post(client, "/api/rules/pending/pd1/revise",
                     {"session_id": "s-http", "instruction": "要两个人才开"})

        assert resp.status_code == 200
        assert resp.json()["data"]["summary"] == "改成两个人"
        assert pending_env.session.pending_confirmations["pd1"]["rule"]["condition"] == "画面里有两个人"
        # 草稿不持久化，revise 不该触发会话落库
        assert pending_env.stored == []

    def test_cancel_ok(self, client: TestClient, pending_env):
        resp = _post(client, "/api/rules/pending/pd1/cancel", {"session_id": "s-http"})

        assert resp.status_code == 200
        assert resp.json()["data"] == {"cancelled": True, "name": "有人开研发部灯"}
        assert pending_env.session.pending_confirmations == {}
        assert pending_env.saved == []

    def test_three_segment_path_still_hits_legacy_rule_endpoint(self, client: TestClient,
                                                               pending_env):
        """路径不冲突：3 段的 /rules/{rule_id}/explain 仍走旧端点，不被 pending 抢。

        旧端点查不到规则 → 404 rule_not_found（而不是 pending_rule_not_found），
        证明命中的是 explain_rule 而非 explain_pending_rule。
        """
        resp = _post(client, "/api/rules/pd1/explain",
                     {"session_id": "s-http", "question": "？", "current": {}})

        assert resp.status_code == 404
        assert resp.json()["code"] == "rule_not_found"
