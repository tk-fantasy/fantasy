"""飞书 ws_client 异步化测试（审查 #5）。

验证 _send_message：
- 是 async 方法
- 内部调 lark client 的 acreate（原生异步）而非 create（同步阻塞主 loop）

不依赖真实 lark 凭证/网络，用 mock lark client 验证调用路径。
"""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
        from app.services import alert_service as alert_mod

        created = {}
        register = MagicMock()
        monkeypatch.setattr(alert_mod.alert_service, "register_notifier", register)

        class _FakeBot:
            def __init__(self, app_id, app_secret, verification_token="", encrypt_key="",
                         notify_chat_id=""):
                created.update(app_id=app_id, vt=verification_token, ek=encrypt_key,
                               notify_chat_id=notify_chat_id)
                self.notify = AsyncMock()

            def start(self, fn, loop):
                created["started"] = True

        monkeypatch.setattr(ch, "get_host_config", lambda name: {
            "app_id": "ui-id", "app_secret": "ui-sec",
            "verification_token": "vt", "encrypt_key": "ek"})
        monkeypatch.setattr(fm, "FeishuBot", _FakeBot)
        loop = asyncio.new_event_loop()
        bot = fm.start(asyncio.sleep, loop)
        assert bot is not None and created == {
            "app_id": "ui-id", "vt": "vt", "ek": "ek", "started": True,
            "notify_chat_id": ""}
        # 启动成功即自注册为告警/周报推送渠道
        register.assert_called_once_with("feishu", bot.notify)

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
        from app.services import alert_service as alert_mod

        stopped = []
        unregister = MagicMock()
        monkeypatch.setattr(alert_mod.alert_service, "unregister_notifier", unregister)

        class _FakeBot:
            def __init__(self, **kw):
                self.notify = AsyncMock()

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
        unregister.assert_called_once_with("feishu")
        fm.stop()  # 已为 None → no-op
        assert stopped == [True]
        # 注销本身幂等（alert_service 侧是 no-op pop），第二次 stop 再调无害
        assert unregister.call_count == 2

    def test_register_failure_still_returns_bot(self, monkeypatch):
        """通知渠道注册失败（如 alert_service 异常）不影响聊天启动。"""
        import integrations.feishu.main as fm
        import app.integration.config_helper as ch
        from app.services import alert_service as alert_mod

        def boom(name, fn):
            raise RuntimeError("alert service down")

        monkeypatch.setattr(alert_mod.alert_service, "register_notifier", boom)

        class _FakeBot:
            def __init__(self, **kw):
                self.notify = AsyncMock()

            def start(self, fn, loop):
                pass

        monkeypatch.setattr(ch, "get_host_config", lambda name: {
            "app_id": "id", "app_secret": "sec"})
        monkeypatch.setattr(fm, "FeishuBot", _FakeBot)
        assert fm.start(asyncio.sleep, asyncio.new_event_loop()) is not None

    def test_start_without_credentials_never_registers(self, monkeypatch):
        import integrations.feishu.main as fm
        import app.integration.config_helper as ch
        from app.services import alert_service as alert_mod

        register = MagicMock()
        monkeypatch.setattr(alert_mod.alert_service, "register_notifier", register)
        monkeypatch.setattr(ch, "get_host_config", lambda name: {})
        for env in fm._ENV_FALLBACK.values():
            monkeypatch.delenv(env, raising=False)
        assert fm.start(asyncio.sleep, asyncio.new_event_loop()) is None
        register.assert_not_called()


class TestNotify:
    """notify()：alert_service Notifier 协议的主动推送实现。"""

    @pytest.mark.asyncio
    async def test_configured_chat_id_wins_with_warning_prefix(self):
        bot = _make_bot()
        bot._notify_chat_id = "oc_configured"
        bot._last_chat_id = "oc_last"
        sent = AsyncMock()
        bot._send_message = sent
        await bot.notify("摄像头离线", "warning")
        sent.assert_awaited_once_with("oc_configured", "⚠️ 摄像头离线")

    @pytest.mark.asyncio
    async def test_falls_back_to_last_chat_id(self):
        bot = _make_bot()
        bot._last_chat_id = "oc_last"
        await bot.notify("HA 已恢复", "info")
        bot._lark_client.im.v1.message.acreate.assert_awaited_once()
        # info 级别无前缀
        sent = AsyncMock()
        bot._send_message = sent
        await bot.notify("纯文本", "info")
        sent.assert_awaited_once_with("oc_last", "纯文本")

    @pytest.mark.asyncio
    async def test_error_level_uses_alert_prefix(self):
        bot = _make_bot()
        bot._notify_chat_id = "oc_cfg"
        sent = AsyncMock()
        bot._send_message = sent
        await bot.notify("定时任务失败", "error")
        sent.assert_awaited_once_with("oc_cfg", "🚨 定时任务失败")

    @pytest.mark.asyncio
    async def test_no_target_drops_silently(self):
        bot = _make_bot()
        sent = AsyncMock()
        bot._send_message = sent
        await bot.notify("没人聊过天")  # 不抛
        sent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_receive_records_last_chat_id(self):
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock(return_value="ok")
        bot._loop = asyncio.get_running_loop()
        bot._on_message_receive(_evt(chat_id="oc_new"))
        await asyncio.sleep(0.05)
        assert bot._last_chat_id == "oc_new"

    @pytest.mark.asyncio
    async def test_receive_records_chat_even_for_non_text(self):
        """非文本消息也证明会话活跃，同样记录（推送目标跟随最近触达）。"""
        bot = _make_bot()
        bot._dispatch_fn = AsyncMock()
        bot._loop = asyncio.get_running_loop()
        bot._on_message_receive(_evt(mtype="image", chat_id="oc_img"))
        await asyncio.sleep(0.02)
        assert bot._last_chat_id == "oc_img"


# ---------------------------------------------------------------------------
# 摄像头绑定问答（渠道专属）
#
# 视觉规则必须显式绑一路摄像头才能落库（核心 confirm_pending 会拦），飞书没有界面，
# 所以由插件问一轮。这里用**真实** SessionState + 真实 pending_rules 逻辑，只桩掉
# 容器入口和发消息，确保插件与核心的接线是真的通。
# ---------------------------------------------------------------------------

CAMERAS = [
    {"id": "cam_1", "name": "研发部", "enabled": True},
    {"id": "cam_2", "name": "门口", "enabled": True},
    {"id": "cam_3", "name": "已停用", "enabled": False},
]

VISION_RULE = {
    "name": "有人开研发部灯", "condition": "画面里有人",
    "type": "vision", "camera_id": "",
    "actions": [{"mcp_tool_input": {"entity_id": "light.rd"}}],
}


def _asked_now():
    """一条「正在等摄像头答复」的状态（刚问出口，未过期）。"""
    return {"pending_id": "pd1", "chat_id": "oc_1", "asked_at": time.monotonic()}


def _session_with_draft(rule=None, camera_chosen=False, session_id="feishu_oc_1"):
    """真实 SessionState + 真实草稿（pending_rules 的逻辑不桩）。"""
    from app.services.session_store import SessionState

    session = SessionState(session_id=session_id, request_id="r1", user_id="feishu_u1")
    session.model_messages = [{"role": "user", "content": "有人就开灯"}]
    session.pending_confirmations["pd1"] = {
        "kind": "automation_rule",
        "rule": dict(rule if rule is not None else VISION_RULE),
        "created_at": time.time(),
        **({"camera_chosen": True} if camera_chosen else {}),
    }
    return session


@contextmanager
def _fake_core(session, cameras=CAMERAS):
    """把插件惰性 import 的 app.container.get_container 换成假容器。"""
    store = SimpleNamespace(get_session=AsyncMock(return_value=session),
                            store_session=AsyncMock())
    container = SimpleNamespace(
        session_store=store,
        camera_manager=SimpleNamespace(list_cameras=MagicMock(return_value=cameras)),
    )
    with patch("app.container.get_container", return_value=container):
        yield container


def _bot_with_sent():
    bot = _make_bot()
    bot._send_message = AsyncMock()
    return bot


def _sent_texts(bot):
    return [c.args[1] for c in bot._send_message.await_args_list]


class TestResolveCamera:
    """用户的话 → camera_id。"""

    def test_exact_name(self):
        assert FeishuBot._resolve_camera("门口", CAMERAS[:2]) == ("cam_2", True, [])

    def test_name_with_filler_words(self):
        """用户常说「门口那个」「用门口摄像头」。"""
        cid, ok, _ = FeishuBot._resolve_camera("门口那个", [{"id": "cam_1", "name": "研发部"},
                                                            {"id": "cam_2", "name": "门口"}])
        assert (cid, ok) == ("cam_2", True)

    def test_by_id(self):
        assert FeishuBot._resolve_camera("cam_1", CAMERAS[:2]) == ("cam_1", True, [])

    def test_global_words(self):
        for word in ("全部", "全局", "所有", "全部摄像头", "all"):
            cid, ok, _ = FeishuBot._resolve_camera(word, CAMERAS[:2])
            assert (cid, ok) == ("", True), word

    def test_unknown_returns_not_ok(self):
        cid, ok, amb = FeishuBot._resolve_camera("今天天气怎样", CAMERAS[:2])
        assert (cid, ok) == (None, False)
        assert amb == []

    def test_empty_returns_not_ok(self):
        assert FeishuBot._resolve_camera("   ", CAMERAS[:2]) == (None, False, [])

    def test_exact_name_beats_substring_ambiguity(self):
        """有一路就叫「门口」时，用户说「门口」就该命中它 —— 精确同名是最强信号，
        不该因为还存在「门口外」这种包含关系就报歧义。"""
        cams = [{"id": "cam_1", "name": "门口"}, {"id": "cam_2", "name": "门口外"}]
        assert FeishuBot._resolve_camera("门口", cams) == ("cam_1", True, [])

    def test_ambiguous_lists_candidates(self):
        """没有精确同名、又有两路都包含用户说的词 → 报歧义并给候选，不替用户猜。"""
        cams = [{"id": "cam_1", "name": "门口摄像头"}, {"id": "cam_2", "name": "门口外摄像头"}]
        cid, ok, amb = FeishuBot._resolve_camera("门口", cams)
        assert (cid, ok) == (None, False)
        assert set(amb) == {"门口摄像头", "门口外摄像头"}


class TestMaybePromptCamera:
    @pytest.mark.asyncio
    async def test_prompts_and_records_state(self):
        bot = _bot_with_sent()
        session = _session_with_draft()

        with _fake_core(session):
            await bot._maybe_prompt_camera("feishu_oc_1", "oc_1")

        text = " ".join(_sent_texts(bot))
        assert "看哪一路" in text
        assert "研发部" in text and "门口" in text
        # 停用的那路不该出现在候选里
        assert "已停用" not in text
        assert bot._awaiting_camera["feishu_oc_1"]["pending_id"] == "pd1"

    @pytest.mark.asyncio
    async def test_no_prompt_when_rule_already_bound(self):
        bot = _bot_with_sent()
        session = _session_with_draft(rule={**VISION_RULE, "camera_id": "cam_1"})

        with _fake_core(session):
            await bot._maybe_prompt_camera("feishu_oc_1", "oc_1")

        bot._send_message.assert_not_awaited()
        assert bot._awaiting_camera == {}

    @pytest.mark.asyncio
    async def test_no_prompt_for_nonvision_rule(self):
        bot = _bot_with_sent()
        session = _session_with_draft(rule={**VISION_RULE, "type": "weather"})

        with _fake_core(session):
            await bot._maybe_prompt_camera("feishu_oc_1", "oc_1")

        bot._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_prompt_when_user_already_chose_global(self):
        """显式选过「全部摄像头」是合法决定，不该再问一遍。"""
        bot = _bot_with_sent()
        session = _session_with_draft(camera_chosen=True)

        with _fake_core(session):
            await bot._maybe_prompt_camera("feishu_oc_1", "oc_1")

        bot._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_draft_no_prompt(self):
        bot = _bot_with_sent()

        with _fake_core(None):
            await bot._maybe_prompt_camera("feishu_oc_1", "oc_1")

        bot._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_cameras_says_rule_wont_fire(self):
        """一路都没有：别让用户白答一轮，直接说清规则不会触发。"""
        bot = _bot_with_sent()
        session = _session_with_draft()

        with _fake_core(session, cameras=[]):
            await bot._maybe_prompt_camera("feishu_oc_1", "oc_1")

        assert "没有可用摄像头" in " ".join(_sent_texts(bot))
        assert bot._awaiting_camera == {}

    @pytest.mark.asyncio
    async def test_core_failure_degrades_silently(self):
        """取不到核心状态时不能把聊天主流程带崩。"""
        bot = _bot_with_sent()

        with patch("app.container.get_container", side_effect=RuntimeError("no container")):
            await bot._maybe_prompt_camera("feishu_oc_1", "oc_1")

        bot._send_message.assert_not_awaited()


class TestTryCameraAnswer:
    @pytest.mark.asyncio
    async def test_not_awaiting_passes_through(self):
        bot = _bot_with_sent()

        handled = await bot._try_camera_answer("feishu_oc_1", "门口", "oc_1")

        assert handled is False
        bot._send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_binds_named_camera_and_consumes_message(self):
        bot = _bot_with_sent()
        session = _session_with_draft()
        bot._awaiting_camera["feishu_oc_1"] = _asked_now()

        with _fake_core(session):
            handled = await bot._try_camera_answer("feishu_oc_1", "门口", "oc_1")

        assert handled is True
        assert session.pending_confirmations["pd1"]["rule"]["camera_id"] == "cam_2"
        assert session.pending_confirmations["pd1"]["camera_chosen"] is True
        assert "已绑定：门口" in " ".join(_sent_texts(bot))
        assert bot._awaiting_camera == {}  # 一次性，不重复拦

    @pytest.mark.asyncio
    async def test_binds_global_on_explicit_word(self):
        bot = _bot_with_sent()
        session = _session_with_draft()
        bot._awaiting_camera["feishu_oc_1"] = _asked_now()

        with _fake_core(session):
            handled = await bot._try_camera_answer("feishu_oc_1", "全部", "oc_1")

        assert handled is True
        assert session.pending_confirmations["pd1"]["rule"]["camera_id"] == ""
        assert session.pending_confirmations["pd1"]["camera_chosen"] is True
        assert "全部摄像头" in " ".join(_sent_texts(bot))

    @pytest.mark.asyncio
    async def test_unrecognized_answer_goes_back_to_llm(self):
        """认不出就别吞掉用户的指令 —— 交回 LLM，草稿留给核心的强校验兜底。"""
        bot = _bot_with_sent()
        session = _session_with_draft()
        bot._awaiting_camera["feishu_oc_1"] = _asked_now()

        with _fake_core(session):
            handled = await bot._try_camera_answer("feishu_oc_1", "今天天气怎样", "oc_1")

        assert handled is False
        assert bot._awaiting_camera == {}
        assert session.pending_confirmations["pd1"]["rule"]["camera_id"] == ""

    @pytest.mark.asyncio
    async def test_expired_ask_passes_through(self):
        bot = _bot_with_sent()
        bot._awaiting_camera["feishu_oc_1"] = {
            "pending_id": "pd1", "chat_id": "oc_1",
            "asked_at": time.monotonic() - 999}

        handled = await bot._try_camera_answer("feishu_oc_1", "门口", "oc_1")

        assert handled is False
        assert bot._awaiting_camera == {}

    @pytest.mark.asyncio
    async def test_draft_gone_tells_user_to_restart(self):
        """草稿过期/重启丢失后用户才回答：如实说明，别假装绑上了。"""
        from app.services.session_store import SessionState

        bot = _bot_with_sent()
        empty = SessionState(session_id="feishu_oc_1", request_id="r1", user_id="u1")
        bot._awaiting_camera["feishu_oc_1"] = _asked_now()

        with _fake_core(empty):
            handled = await bot._try_camera_answer("feishu_oc_1", "门口", "oc_1")

        assert handled is True
        assert "重新说一遍需求" in " ".join(_sent_texts(bot))


class TestHandlerWiring:
    """两条消息路径都要接上问答 —— 漏一条等于对那条路径完全失效。"""

    @pytest.mark.asyncio
    async def test_handle_chat_intercepts_camera_answer(self):
        bot = _bot_with_sent()
        bot._dispatch_fn = AsyncMock(return_value="不该被调到")
        session = _session_with_draft()
        bot._awaiting_camera["feishu_oc_1"] = _asked_now()

        with _fake_core(session):
            await bot._handle_chat("oc_1", "门口", {"chat_id": "oc_1", "open_id": "u1"})

        bot._dispatch_fn.assert_not_awaited()
        assert session.pending_confirmations["pd1"]["rule"]["camera_id"] == "cam_2"

    @pytest.mark.asyncio
    async def test_handle_chat_prompts_after_reply(self):
        bot = _bot_with_sent()
        bot._dispatch_fn = AsyncMock(return_value="规则尚未创建，请确认")
        session = _session_with_draft()

        with _fake_core(session):
            await bot._handle_chat("oc_1", "有人就开灯", {"chat_id": "oc_1", "open_id": "u1"})

        texts = _sent_texts(bot)
        assert texts[0] == "规则尚未创建，请确认"
        assert "看哪一路" in texts[1]  # 追问排在模型回复之后

    @pytest.mark.asyncio
    async def test_handle_and_reply_intercepts_camera_answer(self):
        bot = _bot_with_sent()
        bot._dispatch_fn = AsyncMock(return_value="不该被调到")
        session = _session_with_draft()
        bot._awaiting_camera["feishu_oc_1"] = _asked_now()

        with _fake_core(session):
            await bot._handle_and_reply("门口", "feishu_oc_1", "feishu_u1", "oc_1")

        bot._dispatch_fn.assert_not_awaited()
        assert session.pending_confirmations["pd1"]["rule"]["camera_id"] == "cam_2"

    @pytest.mark.asyncio
    async def test_handle_and_reply_prompts_after_reply(self):
        bot = _bot_with_sent()
        bot._dispatch_fn = AsyncMock(return_value="规则尚未创建")
        session = _session_with_draft()

        with _fake_core(session):
            await bot._handle_and_reply("有人就开灯", "feishu_oc_1", "feishu_u1", "oc_1")

        assert "看哪一路" in " ".join(_sent_texts(bot))

    @pytest.mark.asyncio
    async def test_clear_command_drops_pending_camera_ask(self):
        """/clear 之后不该再把下一条消息当成摄像头答复吃掉。"""
        bot = _bot_with_sent()
        bot._dispatch_fn = AsyncMock(return_value="ok")
        session = _session_with_draft()
        bot._awaiting_camera["feishu_oc_1"] = _asked_now()

        with _fake_core(session):
            await bot._handle_and_reply("/clear", "feishu_oc_1", "feishu_u1", "oc_1")

        assert bot._awaiting_camera == {}
