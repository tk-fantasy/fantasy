"""生产加固回归测试。

覆盖四项修复：
1. WS 畸形帧不再杀死连接（ws_routes._receive_payload）
2. TaskManager 异常留痕 + shutdown 收口（utils/async_utils）
3. call_service 状态回查失败显性化（tools._register_ha_call_service）
4. query-param token 移除 + WS 拒绝 refresh token（auth/main）
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocket

from app.utils.async_utils import TaskManager


# ============ 1. WS 畸形帧 ============


def _ws_with_receive(receive_json) -> WebSocket:
    ws = MagicMock(spec=WebSocket)
    ws.receive_json = receive_json
    return ws


class TestWsReceivePayload:
    """畸形帧只跳过不断连：此前 receive_json 未捕获解析异常，一条非法
    JSON 直接杀死 WS 且不留日志。"""

    async def test_malformed_json_returns_none(self):
        from app.routes.ws_routes import _receive_payload

        recv = AsyncMock(side_effect=json.JSONDecodeError("expecting value", "{", 0))
        assert await _receive_payload(_ws_with_receive(recv)) is None

    async def test_non_utf8_returns_none(self):
        from app.routes.ws_routes import _receive_payload

        recv = AsyncMock(side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"))
        assert await _receive_payload(_ws_with_receive(recv)) is None

    async def test_non_dict_json_returns_none(self):
        from app.routes.ws_routes import _receive_payload

        recv = AsyncMock(return_value=["not", "a", "dict"])
        assert await _receive_payload(_ws_with_receive(recv)) is None

    async def test_valid_dict_passthrough(self):
        from app.routes.ws_routes import _receive_payload

        recv = AsyncMock(return_value={"type": "chat", "query": "hi"})
        assert await _receive_payload(_ws_with_receive(recv)) == {"type": "chat", "query": "hi"}


# ============ 2. TaskManager ============


class TestTaskManager:
    async def test_crashed_task_logged_and_discarded(self, caplog):
        """后台任务崩溃必须进日志（此前只 discard，异常零留痕）。"""
        from app.utils.async_utils import logger as tm_logger

        mgr = TaskManager()

        async def boom():
            raise RuntimeError("boom-crash")

        with caplog.at_level("ERROR", logger=tm_logger.name):
            task = mgr.spawn(boom(), name="boom-task")
            await asyncio.sleep(0.05)

        assert task.done()
        assert mgr.pending_count == 0
        assert "boom-crash" in caplog.text

    async def test_shutdown_cancels_pending_tasks(self):
        """shutdown 必须 cancel 并等待存活任务（此前停机不收口）。"""
        mgr = TaskManager()
        started = asyncio.Event()

        async def forever():
            started.set()
            await asyncio.sleep(3600)

        task = mgr.spawn(forever(), name="forever-task")
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await mgr.shutdown(timeout=1.0)
        assert task.cancelled()
        assert mgr.pending_count == 0

    async def test_shutdown_no_tasks_is_noop(self):
        mgr = TaskManager()
        await mgr.shutdown()  # 不抛异常即可


# ============ 3. call_service 状态回查失败 ============


def _call_service_deps(ha_client):
    captured = {}

    def _register_tool(tool):
        captured["tool"] = tool

    deps = SimpleNamespace(
        mcp_client_manager=SimpleNamespace(register_tool=_register_tool),
        vision_client=None,
        ha_service=MagicMock(),
        ha_client_ref=[ha_client],
        camera_manager=None,
        scheduler_service_ref=[None],
    )
    return deps, captured


class TestCallServiceStateCheck:
    async def test_state_recheck_failure_marked_unknown(self):
        """控制成功但状态回查失败 → 必须带 state_check=failed 提示，
        不能静默吞掉让 AI 谎报已确认状态。"""
        from app.tools import _register_ha_call_service

        ha = MagicMock()
        # 第一次 get_states：entity_id 真实性校验通过；第二次：回查失败
        ha.get_states = AsyncMock(side_effect=[
            [{"entity_id": "light.x", "state": "on", "attributes": {}}],
            RuntimeError("ha temporarily down"),
        ])
        deps, captured = _call_service_deps(ha)
        _register_ha_call_service(deps)
        handler = captured["tool"].handler
        session = SimpleNamespace(current_query="")

        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={"ok": True})), \
             patch("app.services.semantic_map.get_action_map", new=AsyncMock(return_value={})):
            ret = await handler(
                {"domain": "light", "service": "turn_on", "entity_id": "light.x"},
                session,
            )

        assert ret["success"] is True
        assert ret["state_check"] == "failed"
        assert "未经核实" in ret["note"]
        assert ret["new_state"] is None

    async def test_state_recheck_success_unchanged(self):
        """回查正常时行为不变：返回 new_state，无 state_check 标记。"""
        from app.tools import _register_ha_call_service

        state = {"entity_id": "light.x", "state": "off", "attributes": {}}
        ha = MagicMock()
        ha.get_states = AsyncMock(return_value=[state])
        deps, captured = _call_service_deps(ha)
        _register_ha_call_service(deps)
        handler = captured["tool"].handler
        session = SimpleNamespace(current_query="")

        with patch("app.tools.call_with_probe", new=AsyncMock(return_value={"ok": True})), \
             patch("app.services.semantic_map.get_action_map", new=AsyncMock(return_value={})):
            ret = await handler(
                {"domain": "light", "service": "turn_off", "entity_id": "light.x"},
                session,
            )

        assert ret["success"] is True
        assert "state_check" not in ret
        assert ret["new_state"]["state"] == "off"


# ============ 4. token 传递面收敛 ============


class TestExtractTokenNoQueryParam:
    def test_query_param_token_ignored(self):
        """?token= 不再作为认证来源（会进浏览器历史与访问日志）。"""
        from app.core.auth import extract_token_from_request

        req = MagicMock(spec=WebSocket)
        req.headers = {}
        req.cookies = {}
        req.query_params = {"token": "secret-from-url"}
        assert extract_token_from_request(req) is None

    def test_bearer_header_still_works(self):
        from app.core.auth import extract_token_from_request

        req = MagicMock(spec=WebSocket)
        req.headers = {"Authorization": "Bearer abc"}
        req.cookies = {}
        req.query_params = {}
        assert extract_token_from_request(req) == "abc"


class TestWsRejectsRefreshToken:
    async def test_refresh_token_cannot_open_ws(self):
        """WS 握手只接受 access token（此前 refresh token 也能过）。"""
        import app.main as main_mod

        ws = MagicMock(spec=WebSocket)
        ws.cookies = {"aether_token": "refresh-token"}
        ws.headers = {}
        ws.query_params = {}
        ws.close = AsyncMock()

        with patch("app.core.auth.verify_token",
                   return_value={"sub": "user-1", "type": "refresh"}):
            uid = await main_mod._ws_verify_token(ws)

        assert uid is None
        ws.close.assert_awaited_once()

    async def test_access_token_accepted(self):
        import app.main as main_mod

        ws = MagicMock(spec=WebSocket)
        ws.cookies = {"aether_token": "access-token"}
        ws.headers = {}
        ws.query_params = {}

        with patch("app.core.auth.verify_token",
                   return_value={"sub": "user-1", "type": "access"}):
            uid = await main_mod._ws_verify_token(ws)

        assert uid == "user-1"
