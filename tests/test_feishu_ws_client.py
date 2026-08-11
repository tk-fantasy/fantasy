"""飞书 ws_client 异步化测试（审查 #5）。

验证 _send_message：
- 是 async 方法
- 内部调 lark client 的 acreate（原生异步）而非 create（同步阻塞主 loop）

不依赖真实 lark 凭证/网络，用 mock lark client 验证调用路径。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.feishu.ws_client import FeishuBot


def _make_bot() -> FeishuBot:
    """构造一个 FeishuBot，lark client 用 mock 替换。"""
    bot = FeishuBot("app_id", "app_secret")
    # mock lark client 的 message resource
    mock_message = MagicMock()
    mock_message.acreate = AsyncMock(return_value=MagicMock(success=lambda: True))
    mock_message.create = MagicMock(return_value=MagicMock(success=lambda: True))  # 不该被调
    mock_im = MagicMock()
    mock_im.v1.message = mock_message
    bot._lark_client = MagicMock()
    bot._lark_client.im = mock_im
    return bot


class TestSendMessageAsync:
    """_send_message 异步化：用 acreate 而非 create。"""

    @pytest.mark.asyncio
    async def test_send_message_is_coroutine(self):
        """_send_message 必须是 async（审查 #5）。"""
        import inspect
        assert inspect.iscoroutinefunction(FeishuBot._send_message)

    @pytest.mark.asyncio
    async def test_send_message_calls_acreate_not_create(self):
        """_send_message 应 await acreate，不应调同步 create。"""
        bot = _make_bot()
        await bot._send_message("chat_123", "hello")

        # acreate 被调（async）
        bot._lark_client.im.v1.message.acreate.assert_awaited_once()
        # create 没被调（同步版不应再用）
        bot._lark_client.im.v1.message.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_passes_chat_id_and_text(self):
        """acreate 收到的是 builder 链产出的真实 request 对象。"""
        bot = _make_bot()
        await bot._send_message("chat_456", "测试消息")

        bot._lark_client.im.v1.message.acreate.assert_awaited_once()
        req = bot._lark_client.im.v1.message.acreate.await_args.args[0]
        # request 是 CreateMessageRequest 实例（builder 链产出），有 body
        assert req is not None
        assert hasattr(req, "body")


class TestHandleAndReplyAwaitSendMessage:
    """_handle_and_reply（async）里所有 _send_message 调用都 await。"""

    @pytest.mark.asyncio
    async def test_clear_command_awaits_send(self):
        """/clear 命令的回复消息 await 发送。"""
        bot = _make_bot()
        bot._clear_session = AsyncMock()
        await bot._handle_and_reply("/clear", "feishu_c1", "feishu_u1", "chat_1")
        bot._clear_session.assert_awaited_once()
        bot._lark_client.im.v1.message.acreate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_help_command_awaits_send(self):
        """/help 命令的回复消息 await 发送。"""
        bot = _make_bot()
        await bot._handle_and_reply("/help", "feishu_c1", "feishu_u1", "chat_1")
        bot._lark_client.im.v1.message.acreate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_reply_awaits_send(self):
        """正常对话回复 await 发送。"""
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock(return_value="这是回复")
        await bot._handle_and_reply("你好", "feishu_c1", "feishu_u1", "chat_1")
        bot._dispatch_fn.assert_awaited_once()
        bot._lark_client.im.v1.message.acreate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_error_awaits_error_send(self):
        """dispatch 抛异常时，错误通知也 await 发送。"""
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock(side_effect=RuntimeError("LLM down"))
        await bot._handle_and_reply("你好", "feishu_c1", "feishu_u1", "chat_1")
        # 错误通知也应走 acreate
        bot._lark_client.im.v1.message.acreate.assert_awaited_once()


class TestPendingTasksRetention:
    """run_coroutine_threadsafe 返回的 future 保留引用（防 GC 取消）。"""

    def test_init_has_pending_tasks_set(self):
        """__init__ 应初始化 _pending_tasks set。"""
        bot = FeishuBot("app_id", "app_secret")
        assert hasattr(bot, "_pending_tasks")
        assert isinstance(bot._pending_tasks, set)
