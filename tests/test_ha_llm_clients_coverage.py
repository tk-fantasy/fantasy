"""Final cleanup batch: 补齐 app/clients 三个模块的剩余覆盖。

- ha_client.update_entity_name：websockets 边界 mock（fake connect + 收发脚本）
- llm_base_client：_resolve_enabled 环境变量分支、post_json 的 429 重试 /
  5xx 日志 / 共享客户端重建 / JSON 解析失败、批量 embedding 空入参
- llm_chat_client：metrics 容器未就绪的吞异常分支、失败路径的错误计数与重抛

边界 mock：HTTP（httpx.AsyncClient 替身 / _get_shared_client）、websocket、
asyncio.sleep；无真实网络。conftest 已把 CONFIG/CONFIG_PATH 指到 tmp。
"""
from __future__ import annotations

import copy
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import app.clients.ha_client as ha_mod
import app.clients.llm_base_client as base_cli
import app.container as container_mod
import websockets
from app.clients.ha_client import HomeAssistantClient
from app.clients.llm_base_client import LlmBaseClient
from app.clients.llm_chat_client import LlmChatClient
from app.core.config import get_config
from app.core.exceptions import ModelServiceException


# ================================================================ ha_client


def _fake_http_response(status=200, body=None):
    return httpx.Response(status, json=body if body is not None else [])


class _FakeWS:
    """按脚本回放服务端帧的假 websocket；记录客户端发送的所有帧。"""

    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent: list[str] = []

    async def recv(self):
        return self._incoming.pop(0)

    async def send(self, frame):
        self.sent.append(frame)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestUpdateEntityName:
    """GET/POST 之外的 WebSocket 写路径（entity_registry 更名）。"""

    def _patch_connect(self, monkeypatch, recorder, incoming):
        def _connect(url, additional_headers=None, **kwargs):
            recorder["url"] = url
            recorder["headers"] = dict(additional_headers or {})
            return _FakeWS(incoming)

        monkeypatch.setattr(websockets, "connect", _connect)

    async def test_success_frames_content(self, monkeypatch):
        recorder: dict = {}
        frames: list[_FakeWS] = []
        real_connect = None

        def _connect(url, additional_headers=None, **kwargs):
            ws = _FakeWS([
                json.dumps({"type": "auth_required"}),
                json.dumps({"type": "auth_ok"}),
                json.dumps({"id": 1, "type": "result", "success": True,
                            "result": {"name": "客厅主灯"}}),
            ])
            frames.append(ws)
            recorder["url"] = url
            recorder["headers"] = dict(additional_headers or {})
            return ws

        monkeypatch.setattr(websockets, "connect", _connect)
        c = HomeAssistantClient(base_url="http://ha:8123", token="tok")
        result = await c.update_entity_name("light.lamp", "客厅主灯")
        assert result == {"name": "客厅主灯"}
        assert recorder["url"] == "ws://ha:8123/api/websocket"
        assert recorder["headers"] == {"Authorization": "Bearer tok"}
        sent = [json.loads(f) for f in frames[0].sent]
        assert sent[0] == {"type": "auth", "access_token": "tok"}
        assert sent[1] == {"id": 1, "type": "config/entity_registry/update",
                           "entity_id": "light.lamp", "name": "客厅主灯"}

    async def test_ws_url_preserves_host_containing_http(self, monkeypatch):
        """主机名含 "http"（如 http-proxy.lan）时不被全量 replace 改坏。"""
        recorder: dict = {}

        def _connect(url, additional_headers=None, **kwargs):
            recorder["url"] = url
            return _FakeWS([
                json.dumps({"type": "auth_required"}),
                json.dumps({"type": "auth_ok"}),
                json.dumps({"id": 1, "type": "result", "success": True, "result": {}}),
            ])

        monkeypatch.setattr(websockets, "connect", _connect)
        c = HomeAssistantClient(base_url="http://http-proxy.lan:8123", token="tok")
        await c.update_entity_name("light.lamp", "x")
        assert recorder["url"] == "ws://http-proxy.lan:8123/api/websocket"

    async def test_ws_url_https_scheme(self, monkeypatch):
        """https → wss 协议头正确升级。"""
        recorder: dict = {}

        def _connect(url, additional_headers=None, **kwargs):
            recorder["url"] = url
            return _FakeWS([
                json.dumps({"type": "auth_required"}),
                json.dumps({"type": "auth_ok"}),
                json.dumps({"id": 1, "type": "result", "success": True, "result": {}}),
            ])

        monkeypatch.setattr(websockets, "connect", _connect)
        c = HomeAssistantClient(base_url="https://ha.example.com", token="tok")
        await c.update_entity_name("light.lamp", "x")
        assert recorder["url"] == "wss://ha.example.com/api/websocket"

    async def test_auth_failure_raises(self, monkeypatch):
        recorder: dict = {}
        self._patch_connect(monkeypatch, recorder, incoming=[
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_invalid", "message": "bad token"}),
        ])
        c = HomeAssistantClient(base_url="http://ha:8123", token="wrong")
        with pytest.raises(RuntimeError, match="HA auth failed"):
            await c.update_entity_name("light.lamp", "x")

    async def test_unexpected_frame_id_raises(self, monkeypatch):
        """HA 穿插了其他消息：帧 id 不配对 → 明确报错而不是误报改名失败。"""
        recorder: dict = {}
        self._patch_connect(monkeypatch, recorder, incoming=[
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
            json.dumps({"id": 99, "type": "event", "success": True}),
        ])
        c = HomeAssistantClient(base_url="http://ha:8123", token="tok")
        with pytest.raises(RuntimeError, match="unexpected frame"):
            await c.update_entity_name("light.lamp", "x")

    async def test_error_result_raises_with_ha_message(self, monkeypatch):
        recorder: dict = {}
        self._patch_connect(monkeypatch, recorder, incoming=[
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
            json.dumps({"id": 1, "type": "result", "success": False,
                        "error": {"code": "not_found",
                                  "message": "entity not found"}}),
        ])
        c = HomeAssistantClient(base_url="http://ha:8123", token="tok")
        with pytest.raises(RuntimeError, match="entity not found"):
            await c.update_entity_name("light.lamp", "x")

    async def test_no_token_sends_no_auth_header(self, monkeypatch):
        """trusted_networks 部署（无 token）：不带 Authorization 头。"""
        recorder: dict = {}
        frames: list[_FakeWS] = []

        def _connect(url, additional_headers=None, **kwargs):
            ws = _FakeWS([
                json.dumps({"type": "auth_required"}),
                json.dumps({"type": "auth_ok"}),
                json.dumps({"id": 1, "type": "result", "success": True,
                            "result": {}}),
            ])
            frames.append(ws)
            recorder["url"] = url
            recorder["headers"] = dict(additional_headers or {})
            return ws

        monkeypatch.setattr(websockets, "connect", _connect)
        c = HomeAssistantClient(base_url="http://ha:8123", token="")
        assert await c.update_entity_name("light.lamp", None) == {}
        assert "Authorization" not in recorder["headers"]
        assert json.loads(frames[0].sent[0])["access_token"] == ""

    async def test_result_key_absent_returns_empty_dict(self, monkeypatch):
        recorder: dict = {}
        self._patch_connect(monkeypatch, recorder, incoming=[
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
            json.dumps({"id": 1, "type": "result", "success": True}),
        ])
        c = HomeAssistantClient(base_url="http://ha:8123", token="tok")
        assert await c.update_entity_name("light.lamp", "x") == {}


class TestGetClientFactory:
    async def test_creates_once_and_reuses(self, monkeypatch):
        sentinel = MagicMock()
        sentinel.is_closed = False
        calls: list[dict] = []

        def _new_client(**kwargs):
            calls.append(kwargs)
            return sentinel

        monkeypatch.setattr(ha_mod, "new_client", _new_client)
        c = HomeAssistantClient(base_url="http://ha:8123/", token="tok")
        got1 = await c._get_client()
        got2 = await c._get_client()
        assert got1 is sentinel and got2 is sentinel  # 复用同一实例
        assert len(calls) == 1
        assert calls[0]["timeout"] == 10.0
        assert calls[0]["base_url"] == "http://ha:8123"
        assert calls[0]["headers"]["Authorization"] == "Bearer tok"

    async def test_no_token_omits_auth_header(self, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(ha_mod, "new_client",
                            lambda **kw: calls.append(kw) or MagicMock())
        c = HomeAssistantClient(base_url="http://ha:8123", token="")
        await c._get_client()
        assert "Authorization" not in calls[0]["headers"]


# ================================================================ llm_base_client


def _resp(status=200, body=None, json_raises=False):
    r = MagicMock()
    r.status_code = status
    r.text = json.dumps(body if body is not None else {})
    if json_raises:
        r.json = MagicMock(side_effect=ValueError("not valid json"))
    else:
        r.json = MagicMock(return_value=body if body is not None else {})
    r.raise_for_status = MagicMock()
    return r


def _enabled_client(role="chat"):
    c = LlmBaseClient(role=role)
    c._enabled = True
    c._base_url = "http://llm"
    c._api_key = "sk-test"
    return c


class TestResolveEnabledEnvBranches:
    def _with_clean_providers(self):
        backup = copy.deepcopy(get_config("providers") or {})
        return backup

    def test_role_config_enabled_used_when_env_absent(self, monkeypatch):
        """无角色 env 时读 providers.<role>.enabled。"""
        backup = self._with_clean_providers()
        monkeypatch.delenv("LLM_PROBE_ENABLED", raising=False)
        monkeypatch.delenv("LLM_ENABLED", raising=False)
        from app.core.config import update_memory_config
        update_memory_config("providers", {"probe": {"enabled": True}})
        try:
            assert LlmBaseClient(role="probe").enabled is True
            update_memory_config("providers", {"probe": {"enabled": 0}})
            assert LlmBaseClient(role="probe").enabled is False
        finally:
            from app.core.config import update_memory_config as umc
            umc("providers", backup)

    def test_load_resolves_params_from_bound_key_entry(self, monkeypatch):
        """providers.<role>.key_id 绑定 → _load 从 key 条目取全部后端参数。"""
        keys_backup = copy.deepcopy(get_config("llm_keys") or [])
        providers_backup = self._with_clean_providers()
        from app.core.config import update_memory_config
        update_memory_config("llm_keys", [
            {"id": "pb1", "type": "probe", "base_url": "http://prov/v1/",
             "model": "probe-model", "api_key": "sk-pb"},
        ])
        update_memory_config("providers.probe.key_id", "pb1")
        try:
            c = LlmBaseClient(role="probe")
            assert c._base_url == "http://prov/v1"  # 尾部 / 被 rstrip
            assert c._model == "probe-model"
            assert c._chat_path == "/chat/completions"  # 默认路径
            assert c._embed_path == "/embeddings"
            assert c._api_key == "sk-pb"
            assert c.model == "probe-model"
        finally:
            from app.core.config import update_memory_config as umc
            umc("llm_keys", keys_backup)
            umc("providers", providers_backup)

    def test_load_honors_custom_paths_from_key_entry(self, monkeypatch):
        keys_backup = copy.deepcopy(get_config("llm_keys") or [])
        providers_backup = self._with_clean_providers()
        from app.core.config import update_memory_config
        update_memory_config("llm_keys", [
            {"id": "pb2", "type": "probe", "base_url": "http://prov2",
             "model": "m", "api_key": "k",
             "chat_path": "/v1/chat", "embed_path": "/v1/embed"},
        ])
        update_memory_config("providers.probe.key_id", "pb2")
        try:
            c = LlmBaseClient(role="probe")
            assert c._chat_path == "/v1/chat"
            assert c._embed_path == "/v1/embed"
        finally:
            from app.core.config import update_memory_config as umc
            umc("llm_keys", keys_backup)
            umc("providers", providers_backup)

    def test_role_env_var_overrides_config(self, monkeypatch):
        backup = self._with_clean_providers()
        monkeypatch.delenv("LLM_PROBE_ENABLED", raising=False)
        monkeypatch.delenv("LLM_ENABLED", raising=False)
        from app.core.config import update_memory_config
        update_memory_config("providers", {})
        try:
            monkeypatch.setenv("LLM_PROBE_ENABLED", "1")
            assert LlmBaseClient(role="probe").enabled is True
            monkeypatch.setenv("LLM_PROBE_ENABLED", "0")
            assert LlmBaseClient(role="probe").enabled is False  # 仅 "1" 算开
        finally:
            from app.core.config import update_memory_config as umc
            umc("providers", backup)

    def test_global_env_fallback_when_role_env_absent(self, monkeypatch):
        backup = self._with_clean_providers()
        monkeypatch.delenv("LLM_PROBE_ENABLED", raising=False)
        from app.core.config import update_memory_config
        update_memory_config("providers", {})
        try:
            monkeypatch.setenv("LLM_ENABLED", "1")
            assert LlmBaseClient(role="probe").enabled is True
            monkeypatch.setenv("LLM_ENABLED", "0")
            assert LlmBaseClient(role="probe").enabled is False
        finally:
            from app.core.config import update_memory_config as umc
            umc("providers", backup)


class TestPostJsonFailures:
    async def test_disabled_raises_without_http(self, monkeypatch):
        c = LlmBaseClient(role="chat")
        c._enabled = False
        http = MagicMock()
        http.post = AsyncMock()
        monkeypatch.setattr(base_cli, "_get_shared_client", lambda: http)
        with pytest.raises(ModelServiceException, match="LLM 未启用"):
            await c.post_json("/chat/completions", {"x": 1})
        http.post.assert_not_awaited()

    async def test_429_retries_then_succeeds(self, monkeypatch):
        c = _enabled_client()
        http = MagicMock()
        http.is_closed = False
        http.post = AsyncMock(side_effect=[_resp(429), _resp(200, {"ok": 1})])
        monkeypatch.setattr(base_cli, "_get_shared_client", lambda: http)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(base_cli.asyncio, "sleep", sleep_mock)

        result = await c.post_json("/chat/completions", {"x": 1})
        assert result == {"ok": 1}
        assert http.post.await_count == 2  # 429 后重试
        backoff = sleep_mock.await_args.args[0]
        assert 0.5 <= backoff <= 0.7  # 0.5 * 2^0 + jitter(0~0.1)

    async def test_429_on_last_attempt_raises(self, monkeypatch):
        c = _enabled_client()
        err = httpx.HTTPStatusError("429", request=MagicMock(),
                                    response=MagicMock())
        resp = _resp(429)
        resp.raise_for_status.side_effect = err
        http = MagicMock()
        http.is_closed = False
        http.post = AsyncMock(side_effect=[
            _resp(429), _resp(429), resp])  # attempt0/1 重试，attempt2 用尽
        monkeypatch.setattr(base_cli, "_get_shared_client", lambda: http)
        monkeypatch.setattr(base_cli.asyncio, "sleep", AsyncMock())
        with pytest.raises(ModelServiceException):
            await c.post_json("/chat/completions", {"x": 1})
        assert http.post.await_count == 3

    async def test_500_logged_then_raises_model_service_error(
        self, monkeypatch, caplog
    ):
        c = _enabled_client()
        resp = _resp(500, {"error": "boom"})
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock())
        http = MagicMock()
        http.is_closed = False
        http.post = AsyncMock(return_value=resp)
        monkeypatch.setattr(base_cli, "_get_shared_client", lambda: http)
        with caplog.at_level(logging.ERROR, logger="app.clients.llm_base_client"):
            with pytest.raises(ModelServiceException, match="LLM 请求失败"):
                await c.post_json("/chat/completions", {"x": 1})
        assert any("500" in r.getMessage() and "boom" in r.getMessage()
                   for r in caplog.records)  # 响应体进日志便于排障

    async def test_rebuilds_shared_client_after_connect_error(
        self, monkeypatch
    ):
        """共享客户端被意外关闭：换新客户端重试成功。"""
        c = _enabled_client()
        bad = MagicMock()
        bad.is_closed = True
        bad.post = AsyncMock(side_effect=httpx.ConnectError("client closed"))
        good = MagicMock()
        good.is_closed = False
        good.post = AsyncMock(return_value=_resp(200, {"ok": 2}))
        monkeypatch.setattr(base_cli, "_get_shared_client",
                            MagicMock(side_effect=[bad, good]))
        sleep_mock = AsyncMock()
        monkeypatch.setattr(base_cli.asyncio, "sleep", sleep_mock)

        result = await c.post_json("/chat/completions", {"x": 1})
        assert result == {"ok": 2}
        good.post.assert_awaited_once()
        backoff = sleep_mock.await_args.args[0]
        assert 0.3 <= backoff <= 0.4  # 0.3 * 2^0 + jitter(0~0.05)

    async def test_invalid_json_raises_model_service_error(
        self, monkeypatch, caplog
    ):
        c = _enabled_client()
        http = MagicMock()
        http.is_closed = False
        http.post = AsyncMock(return_value=_resp(200, json_raises=True))
        monkeypatch.setattr(base_cli, "_get_shared_client", lambda: http)
        with caplog.at_level(logging.ERROR, logger="app.clients.llm_base_client"):
            with pytest.raises(ModelServiceException, match="不是有效 JSON"):
                await c.post_json("/chat/completions", {"x": 1})
        assert any("not valid JSON" in r.getMessage() for r in caplog.records)


class TestPostEmbeddingsBatchEmpty:
    async def test_empty_texts_short_circuits(self, monkeypatch):
        c = _enabled_client(role="embed")
        http = MagicMock()
        http.post = AsyncMock()
        monkeypatch.setattr(base_cli, "_get_shared_client", lambda: http)
        assert await c.post_embeddings_batch([]) == []
        http.post.assert_not_awaited()  # 空批量不发请求


# ================================================================ llm_chat_client


def _chat_client(post_result=None, post_error=None):
    c = LlmChatClient(role="chat")
    if post_error is not None:
        c.post_chat = AsyncMock(side_effect=post_error)
    else:
        c.post_chat = AsyncMock(return_value=post_result)
    return c


class TestChatMetricsPaths:
    async def test_container_failure_on_entry_logging_is_swallowed(
        self, monkeypatch
    ):
        client = _chat_client(post_result={
            "choices": [{"message": {"content": "答案"}}]})
        monkeypatch.setattr(container_mod, "get_container",
                            MagicMock(side_effect=RuntimeError("not ready")))
        assert await client.chat([{"role": "user", "content": "q"}]) == "答案"

    async def test_error_records_metric_and_reraises(self, monkeypatch):
        client = _chat_client(post_error=ModelServiceException("LLM down"))
        metrics = MagicMock()
        container = MagicMock()
        container.metrics_service = metrics
        monkeypatch.setattr(container_mod, "get_container", lambda: container)
        with pytest.raises(ModelServiceException):
            await client.chat([{"role": "user", "content": "q"}])
        metrics.record_llm_call.assert_any_call()            # 进入时计数
        metrics.record_llm_call.assert_any_call(error=True)  # 失败再计数

    async def test_error_metric_failure_still_reraises_original(
        self, monkeypatch
    ):
        client = _chat_client(post_error=ModelServiceException("LLM down"))
        monkeypatch.setattr(container_mod, "get_container",
                            MagicMock(side_effect=RuntimeError("metrics broken")))
        with pytest.raises(ModelServiceException, match="LLM down"):
            await client.chat([{"role": "user", "content": "q"}])
