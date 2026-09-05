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


# ============================================================================
# 补充：start/stop、_on_message_receive 全流程、_clear_session、发送失败分支
# ============================================================================

import json as _json
from types import SimpleNamespace as _NS


def _evt(text="你好 @_user_1", mtype="text", content=None, chat_id="oc1",
         open_id="ou_9", omit_event=False):
    """构造仿 lark P2ImMessageReceiveV1 的事件对象。"""
    if omit_event:
        return _NS()  # 无 event 属性 → _on_message_receive 进异常分支且 chat_id 为 None
    msg = _NS(chat_id=chat_id, message_type=mtype,
              content=content if content is not None else _json.dumps({"text": text}))
    return _NS(event=_NS(message=msg,
                         sender=_NS(sender_id=_NS(open_id=open_id))))


class TestStartAndStop:
    """start 构建 lark client/事件分发器/ws client 并起后台线程；stop 清理。"""

    def test_start_builds_clients_and_spawns_thread(self, monkeypatch):
        import threading

        import integrations.feishu.ws_client as wc

        built = {}

        class _FakeClientBuilder:
            def app_id(self, v):
                built["app_id"] = v
                return self

            def app_secret(self, v):
                built["app_secret"] = v
                return self

            def build(self):
                return "LARK_CLIENT"

        class _FakeEHBuilder:
            def __init__(self, vt, ek):
                built["eh_tokens"] = (vt, ek)

            def register_p2_im_message_receive_v1(self, cb):
                built["callback"] = cb
                return self

            def build(self):
                return "HANDLER"

        class _FakeWSClient:
            def __init__(self, **kw):
                built["ws_kw"] = kw

            def start(self):
                built["ws_started"] = True

        monkeypatch.setattr(wc.lark, "Client",
                            _NS(builder=_FakeClientBuilder))
        monkeypatch.setattr(wc.lark, "EventDispatcherHandler",
                            _NS(builder=_FakeEHBuilder))
        monkeypatch.setattr(wc.lark.ws, "Client", _FakeWSClient)

        import lark_oapi.ws.client as _ws_mod
        old_loop = getattr(_ws_mod, "loop", None)
        bot = FeishuBot("aid", "sec", "vtok", "ekey")
        loop = asyncio.new_event_loop()
        try:
            bot.start(asyncio.sleep, loop)  # dispatch_fn 用占位协程函数
            bot._thread.join(timeout=5)
        finally:
            loop.close()
            if old_loop is not None:  # 恢复被线程内覆盖的模块级 loop
                _ws_mod.loop = old_loop

        assert built["app_id"] == "aid" and built["app_secret"] == "sec"
        assert built["eh_tokens"] == ("vtok", "ekey")
        assert built["callback"] == bot._on_message_receive
        assert built["ws_kw"]["app_id"] == "aid"
        assert built["ws_kw"]["event_handler"] == "HANDLER"
        assert built["ws_started"] is True
        assert bot._lark_client == "LARK_CLIENT"
        assert bot._thread is not None and not bot._thread.is_alive()

    def test_stop_variants(self):
        bot = FeishuBot("a", "b")
        bot.stop()  # _ws_client 为 None → no-op
        bot._ws_client = _NS()  # 无 close 属性 → 跳过
        bot.stop()
        ok = MagicMock()
        bot._ws_client = _NS(close=ok)
        bot.stop()
        ok.assert_called_once()
        bad = MagicMock(side_effect=RuntimeError("close fail"))
        bot._ws_client = _NS(close=bad)
        bot.stop()  # close 抛异常 → 吞掉不外抛
        bad.assert_called_once()


class TestOnMessageReceive:
    """ws 线程回调：解析→去 @mention→fire-and-forget 投递主 loop。"""

    @pytest.mark.asyncio
    async def test_text_message_dispatched_and_replied(self):
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock(return_value="LLM回复")
        bot._loop = asyncio.get_running_loop()
        bot._on_message_receive(_evt("你好 @_user_1"))
        await asyncio.sleep(0.05)  # 让主 loop 执行投递的任务
        bot._dispatch_fn.assert_awaited_once_with("你好", "feishu_oc1", "feishu_ou_9")
        bot._lark_client.im.v1.message.acreate.assert_awaited_once()
        assert bot._pending_tasks == set()  # 完成后 future 被移除

    @pytest.mark.asyncio
    async def test_non_text_message_ignored(self):
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock()
        bot._loop = asyncio.get_running_loop()
        bot._on_message_receive(_evt(mtype="image"))
        await asyncio.sleep(0.02)
        bot._dispatch_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mention_only_message_ignored(self):
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock()
        bot._loop = asyncio.get_running_loop()
        bot._on_message_receive(_evt(text="@_user_1  "))  # 去掉后为空
        await asyncio.sleep(0.02)
        bot._dispatch_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_broken_content_notifies_chat(self, caplog):
        """content 非法 JSON → 异常分支：已知 chat_id 时投递错误通知。"""
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock()
        bot._loop = asyncio.get_running_loop()
        bot._on_message_receive(_evt(content="not-json"))
        await asyncio.sleep(0.05)
        bot._dispatch_fn.assert_not_awaited()
        bot._lark_client.im.v1.message.acreate.assert_awaited_once()
        assert bot._pending_tasks == set()

    @pytest.mark.asyncio
    async def test_event_missing_no_notification(self):
        """data 连 event 都没有且 chat_id 未取到 → 只记日志，不发通知。"""
        bot = _make_bot()
        bot._loop = asyncio.get_running_loop()
        bot._on_message_receive(_evt(omit_event=True))
        await asyncio.sleep(0.02)
        bot._lark_client.im.v1.message.acreate.assert_not_awaited()


class TestHandleAndReplyFailureBranches:
    @pytest.mark.asyncio
    async def test_send_failure_after_dispatch_error_is_swallowed(self):
        """dispatch 失败后连错误通知也发不出去 → 双层吞掉，不外抛。"""
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock(side_effect=RuntimeError("LLM down"))
        bot._send_message = AsyncMock(side_effect=RuntimeError("send down"))
        await bot._handle_and_reply("你好", "s", "u", "c")  # 不抛


class TestClearSession:
    @pytest.mark.asyncio
    async def test_clear_session_calls_container_store(self, monkeypatch):
        import app.container as container_mod
        clear = AsyncMock()
        monkeypatch.setattr(container_mod, "get_container",
                            lambda: _NS(session_store=_NS(clear_messages=clear)))
        bot = FeishuBot("a", "b")
        await bot._clear_session("feishu_oc1")
        clear.assert_awaited_once_with("feishu_oc1")

    @pytest.mark.asyncio
    async def test_clear_session_failure_is_swallowed(self, monkeypatch):
        import app.container as container_mod

        def boom():
            raise RuntimeError("container down")

        monkeypatch.setattr(container_mod, "get_container", boom)
        bot = FeishuBot("a", "b")
        await bot._clear_session("feishu_oc1")  # 不抛


class TestSendFailureLog:
    @pytest.mark.asyncio
    async def test_failed_send_logs_warning(self, caplog):
        bot = _make_bot()
        bot._lark_client.im.v1.message.acreate = AsyncMock(
            return_value=_NS(success=lambda: False, code=99991668, msg="bad token"))
        import logging as _logging
        with caplog.at_level(_logging.WARNING, logger="integrations.feishu.ws_client"):
            await bot._send_message("oc1", "hi")
        assert "飞书发消息失败" in caplog.text and "99991668" in caplog.text


class TestFeishuMain:
    """integrations/feishu/main.py：_read_config 回退链与 start 分支。"""

    def test_read_config_ui_exception_falls_back_to_env(self, monkeypatch):
        import integrations.feishu.main as fm
        import app.integration.config_helper as ch

        def boom(name):
            raise RuntimeError("config down")

        monkeypatch.setattr(ch, "get_host_config", boom)
        monkeypatch.setenv("FEISHU_APP_ID", "env-id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "env-secret")
        monkeypatch.delenv("FEISHU_VERIFICATION_TOKEN", raising=False)
        monkeypatch.delenv("FEISHU_ENCRYPT_KEY", raising=False)
        cfg, source = fm._read_config()
        assert cfg["app_id"] == "env-id" and cfg["app_secret"] == "env-secret"
        assert source == "env"

    def test_start_without_credentials_returns_none(self, monkeypatch):
        import integrations.feishu.main as fm
        import app.integration.config_helper as ch
        monkeypatch.setattr(ch, "get_host_config", lambda name: {})
        for env in fm._ENV_FALLBACK.values():
            monkeypatch.delenv(env, raising=False)
        assert fm.start(asyncio.sleep, asyncio.new_event_loop()) is None

    def test_start_success_builds_and_starts_bot(self, monkeypatch):
        import integrations.feishu.main as fm
        import app.integration.config_helper as ch

        created = {}

        class _FakeBot:
            def __init__(self, app_id, app_secret, verification_token="", encrypt_key=""):
                created.update(app_id=app_id, vt=verification_token, ek=encrypt_key)

            def start(self, fn, loop):
                created["started"] = True

        monkeypatch.setattr(ch, "get_host_config", lambda name: {
            "app_id": "ui-id", "app_secret": "ui-sec",
            "verification_token": "vt", "encrypt_key": "ek"})
        monkeypatch.setattr(fm, "FeishuBot", _FakeBot)
        loop = asyncio.new_event_loop()
        bot = fm.start(asyncio.sleep, loop)
        assert bot is not None and created == {
            "app_id": "ui-id", "vt": "vt", "ek": "ek", "started": True}

    def test_start_exception_returns_none(self, monkeypatch):
        import integrations.feishu.main as fm
        import app.integration.config_helper as ch

        class _BadBot:
            def __init__(self, **kw):
                raise RuntimeError("lark init failed")

        monkeypatch.setattr(ch, "get_host_config", lambda name: {
            "app_id": "id", "app_secret": "sec"})
        monkeypatch.setattr(fm, "FeishuBot", _BadBot)
        assert fm.start(asyncio.sleep, asyncio.new_event_loop()) is None

    def test_stop_clears_bot(self, monkeypatch):
        import integrations.feishu.main as fm
        import app.integration.config_helper as ch

        stopped = []

        class _FakeBot:
            def __init__(self, **kw):
                pass

            def start(self, fn, loop):
                pass

            def stop(self):
                stopped.append(True)

        monkeypatch.setattr(ch, "get_host_config", lambda name: {
            "app_id": "id", "app_secret": "sec"})
        monkeypatch.setattr(fm, "FeishuBot", _FakeBot)
        fm.start(asyncio.sleep, asyncio.new_event_loop())
        fm.stop()
        assert stopped == [True] and fm._bot is None
        fm.stop()  # 已为 None → no-op
        assert stopped == [True]
