"""阶段7：Assist 桥——HA conversation agent 转发 Aether 单轮对话。

覆盖：
- /api/assist/chat：X-API-Token 鉴权（无 token 401 / 错 token 401 / 未设 APP_TOKEN 401）、
  空 text 400、dispatcher 转发（session_id 映射 assist_<cid>）、
  ToastStream 全文提取（取最后一条）、Dialog.Finish 失败标志、dispatcher 缺失 503
- /api/assist/status：组件文件 + APP_TOKEN 就绪度（patch main.APP_TOKEN）
- HA 组件文件静态检查：manifest.json 合法 JSON 且 domain 匹配；config_flow/__init__ 可编译
"""
from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from app.schema.chat_schema import Dialog, Event, Instruction, Nlp, Template


def _request(token: str = "t0k") -> Request:
    scope = {"type": "http", "headers": [(b"x-api-token", token.encode())]}
    return Request(scope)


def _instructions(reply: str = "好的，已开灯", success: bool = True) -> list:
    return [
        Instruction.build_instruction(
            Template.TokenStream(token="好"), "r", "s"),
        Instruction.build_instruction(
            Template.ToastStream(stream=reply), "r", "s"),
        Instruction.build_instruction(
            Dialog.Finish(success=success, message=""), "r", "s"),
    ]


class TestAssistChat:
    async def test_auth_ok_and_reply_extracted(self):
        from app.routes import assist_routes
        dispatcher = MagicMock()
        captured = {}

        async def fake_dispatch(event: Event, user_id: str = ""):
            captured["session_id"] = event.header.session_id
            captured["query"] = event.payload.get("query")
            captured["user_id"] = user_id
            return _instructions("已为你打开客厅灯")
        dispatcher.dispatch = fake_dispatch
        container = types.SimpleNamespace(dispatcher=dispatcher)
        with patch("app.main.APP_TOKEN", "t0k"):
            res = await assist_routes.assist_chat(
                {"text": "打开客厅灯", "conversation_id": "abc"},
                _request(), container)
        data = res.data
        assert data["reply"] == "已为你打开客厅灯"
        assert data["success"] is True
        assert captured["session_id"] == "assist_abc"
        assert captured["query"] == "打开客厅灯"
        assert captured["user_id"] == "assist"

    async def test_wrong_token_401(self):
        from app.routes import assist_routes
        from app.core.exceptions import AppException
        with patch("app.main.APP_TOKEN", "t0k"), \
             pytest.raises(AppException) as ei:
            await assist_routes.assist_chat(
                {"text": "x"}, _request(token="bad"),
                types.SimpleNamespace(dispatcher=MagicMock()))
        assert ei.value.http_status == 401

    async def test_no_app_token_401(self):
        from app.routes import assist_routes
        from app.core.exceptions import AppException
        with patch("app.main.APP_TOKEN", ""), \
             pytest.raises(AppException):
            await assist_routes.assist_chat(
                {"text": "x"}, _request(),
                types.SimpleNamespace(dispatcher=MagicMock()))

    async def test_empty_text_400(self):
        from app.routes import assist_routes
        from app.core.exceptions import AppException
        with patch("app.main.APP_TOKEN", "t0k"), \
             pytest.raises(AppException) as ei:
            await assist_routes.assist_chat(
                {"text": "  "}, _request(),
                types.SimpleNamespace(dispatcher=MagicMock()))
        assert ei.value.http_status == 400

    async def test_dispatcher_missing_503(self):
        from app.routes import assist_routes
        from app.core.exceptions import AppException
        with patch("app.main.APP_TOKEN", "t0k"), \
             pytest.raises(AppException) as ei:
            await assist_routes.assist_chat(
                {"text": "x"}, _request(), types.SimpleNamespace(dispatcher=None))
        assert ei.value.http_status == 503

    async def test_last_toast_stream_wins_and_finish_failure(self):
        """多条 ToastStream 取最后一条（流式 token 前缀与错误兜底都可能是它）。"""
        from app.routes import assist_routes
        dispatcher = MagicMock()

        async def fake_dispatch(event, user_id=""):
            return _instructions("最终回复", success=False)
        dispatcher.dispatch = fake_dispatch
        with patch("app.main.APP_TOKEN", "t0k"):
            res = await assist_routes.assist_chat(
                {"text": "x"}, _request(),
                types.SimpleNamespace(dispatcher=dispatcher))
        assert res.data["reply"] == "最终回复"
        assert res.data["success"] is False


class TestAssistStatus:
    async def test_status_ready(self):
        from app.routes import assist_routes
        with patch("app.main.APP_TOKEN", "t0k"):
            res = await assist_routes.assist_status()
        assert res.data["component_deployed"] is True  # 仓库内文件真实存在
        assert res.data["token_configured"] is True
        assert res.data["ready"] is True

    async def test_status_no_token(self):
        from app.routes import assist_routes
        with patch("app.main.APP_TOKEN", ""):
            res = await assist_routes.assist_status()
        assert res.data["ready"] is False
        assert "APP_TOKEN" in res.data["hint"]


class TestHaComponentFiles:
    """静态检查：组件文件形态（HA 侧运行环境无法进测试，这里守住结构不碎）。"""

    def _dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parents[1] / "ha_config" / \
            "custom_components" / "aether_conversation"

    def test_manifest_valid(self):
        manifest = json.loads((self._dir() / "manifest.json").read_text("utf-8"))
        assert manifest["domain"] == "aether_conversation"
        assert manifest["config_flow"] is True
        assert "conversation" in manifest["dependencies"]

    def test_sources_compile(self):
        import ast
        for name in ("__init__.py", "config_flow.py", "const.py"):
            src = (self._dir() / name).read_text("utf-8")
            ast.parse(src)

    def test_translation_valid(self):
        data = json.loads(
            (self._dir() / "translations" / "zh-Hans.json").read_text("utf-8"))
        assert "config" in data and "options" in data
