"""第 2 批 bug 修复的回归测试。

覆盖：聊天建任务携带 user_id、at 任务停机过期标记、摘要真正裁剪+注入、
同会话保存链写序、WS 断连后 emit 静音（本轮不丢）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. 聊天建任务携带 user_id（tools.register_scheduler_tools）
# ---------------------------------------------------------------------------

def _get_registered_tool(deps, tool_name: str):
    """从注册调用记录中按 tool_name 取出工具对象。"""
    for call in deps.mcp_client_manager.register_tool.call_args_list:
        args = call.args or ()
        kwargs = call.kwargs or {}
        tool = args[0] if args else kwargs.get("tool")
        if getattr(tool, "tool_name", None) == tool_name:
            return tool
    raise AssertionError(f"tool {tool_name} not registered")


class TestScheduledTaskCreateCarriesUserId:
    @pytest.mark.asyncio
    async def test_create_handler_passes_session_user_id(self):
        """LLM 工具链路创建的任务必须带上会话的 user_id（此前丢失导致
        message 类任务到点必失败、reminder 投递到全系统最近活跃会话）。"""
        from app.tools import _register_scheduled_task_tools

        added = {}

        class FakeSvc:
            async def add_task(self, task):
                added.update(task)
                return {**task, "id": "t1"}

        deps = SimpleNamespace(
            mcp_client_manager=SimpleNamespace(register_tool=MagicMock()),
            scheduler_service_ref=[FakeSvc()],
        )
        _register_scheduled_task_tools(deps)
        tool = _get_registered_tool(deps, "scheduled_task_create")
        handler = getattr(tool, "handler", None) or tool.__dict__.get("handler")
        assert handler is not None

        session = SimpleNamespace(user_id="user-abc")
        result = await handler(
            {"name": "早安", "schedule": {"kind": "every", "every_seconds": 60},
             "payload": {"kind": "reminder", "intent": "起床"}},
            session,
        )
        assert result.get("success") is True
        assert added.get("user_id") == "user-abc"

    @pytest.mark.asyncio
    async def test_create_handler_without_user_id_still_works(self):
        from app.tools import _register_scheduled_task_tools

        added = {}

        class FakeSvc:
            async def add_task(self, task):
                added.update(task)
                return {**task, "id": "t2"}

        deps = SimpleNamespace(
            mcp_client_manager=SimpleNamespace(register_tool=MagicMock()),
            scheduler_service_ref=[FakeSvc()],
        )
        _register_scheduled_task_tools(deps)
        tool = _get_registered_tool(deps, "scheduled_task_create")
        handler = getattr(tool, "handler", None) or tool.__dict__.get("handler")

        session = SimpleNamespace()  # 无 user_id 属性（异常会话）
        result = await handler(
            {"name": "x", "schedule": {"kind": "every", "every_seconds": 60},
             "payload": {"kind": "message", "message": "hi"}},
            session,
        )
        assert result.get("success") is True
        assert added.get("user_id") == ""


# ---------------------------------------------------------------------------
# 2. at 任务停机期间过期 → 启动标记 expired 并禁用
# ---------------------------------------------------------------------------

class TestExpiredAtTaskMarked:
    @pytest.mark.asyncio
    async def test_past_at_task_disabled_with_expired_status(self):
        from datetime import datetime, timedelta
        from app.services.scheduler_service import SchedulerService

        past = (datetime.now() - timedelta(hours=2)).isoformat()
        rows = [{
            "id": "t-old", "name": "过期开灯", "enabled": 1, "user_id": "u1",
            "schedule": {"kind": "at", "at": past}, "payload": {"kind": "tool"},
            "last_status": "", "last_error": "", "next_run_at": None,
        }]
        db = MagicMock()
        db.scheduled_tasks_all = AsyncMock(return_value=rows)
        db.scheduled_task_update = AsyncMock()

        svc = SchedulerService.__new__(SchedulerService)
        svc._db = db
        svc._tasks = {}
        await svc._load_tasks()

        task = svc._tasks["t-old"]
        assert task["enabled"] is False or task["enabled"] == 0
        assert task["last_status"] == "expired"

    @pytest.mark.asyncio
    async def test_future_at_task_stays_enabled(self):
        from datetime import datetime, timedelta
        from app.services.scheduler_service import SchedulerService

        future = (datetime.now() + timedelta(hours=2)).isoformat()
        rows = [{
            "id": "t-new", "name": "晚间关灯", "enabled": 1, "user_id": "u1",
            "schedule": {"kind": "at", "at": future}, "payload": {"kind": "tool"},
            "last_status": "", "last_error": "", "next_run_at": None,
        }]
        db = MagicMock()
        db.scheduled_tasks_all = AsyncMock(return_value=rows)
        db.scheduled_task_update = AsyncMock()

        svc = SchedulerService.__new__(SchedulerService)
        svc._db = db
        svc._tasks = {}
        await svc._load_tasks()

        task = svc._tasks["t-new"]
        assert task["enabled"] in (True, 1)
        assert task["next_run_at"] is not None
        assert task["last_status"] != "expired"


# ---------------------------------------------------------------------------
# 3. 摘要真正生效：裁剪 + 注入
# ---------------------------------------------------------------------------

class TestSummarizationTrimAndInject:
    def _make_session(self, n_pairs: int = 20):
        from app.services.session_store import SessionState
        msgs = []
        for i in range(n_pairs):
            msgs.append({"role": "user", "content": f"问题{i}"})
            msgs.append({"role": "assistant", "content": f"回答{i}"})
        return SessionState(session_id="s1", request_id="r1", user_id="u1",
                            model_messages=msgs)

    @pytest.mark.asyncio
    async def test_refresh_trims_model_messages(self):
        from app.services.summarization_service import SummarizationService

        svc = SummarizationService(chat_client=None)
        svc._soft_max_turns = 5  # 强制触发 soft 压缩
        session = self._make_session()
        before = len(session.model_messages)

        with patch.object(svc, "_resolve_summary_client", return_value=None):
            await svc.refresh_summaries(session, user_id="u1")

        assert len(session.model_messages) < before
        assert len(session.summaries) >= 1
        # 消息数变少后，缓存水位应更新为裁剪后的值
        assert svc._last_message_count["s1"] == len(session.model_messages)

    @pytest.mark.asyncio
    async def test_no_re_summarize_every_turn_after_trim(self):
        """裁剪后消息数回落到阈值下，连续新消息不应每轮都触发摘要 LLM 调用。"""
        from app.services.summarization_service import SummarizationService

        svc = SummarizationService(chat_client=None)
        svc._soft_max_turns = 5
        svc._recent_turns_to_keep = 2  # 裁剪后保留 4 条消息（2 轮）< 阈值
        session = self._make_session()

        calls = []

        async def fake_summarize(chunk, client=None):
            calls.append(1)
            return "摘要"
        with patch.object(svc, "_resolve_summary_client", return_value=None), \
             patch.object(svc, "_summarize_chunk", side_effect=fake_summarize):
            await svc.refresh_summaries(session, user_id="u1")
            n_after_first = len(calls)
            # 模拟后续两轮对话（低于阈值，不应再触发摘要）
            session.model_messages.append({"role": "user", "content": "新问题"})
            session.model_messages.append({"role": "assistant", "content": "新回答"})
            await svc.refresh_summaries(session, user_id="u1")
            session.model_messages.append({"role": "user", "content": "又一个"})
            session.model_messages.append({"role": "assistant", "content": "又一个答"})
            await svc.refresh_summaries(session, user_id="u1")

        assert len(calls) == n_after_first  # 无新增摘要调用

    def test_summaries_injected_into_prompt(self):
        from app.services.session_store import SessionState
        from app.agents.langgraph_agent import session_to_langchain_messages

        session = SessionState(session_id="s1", request_id="r1",
                               summaries=[{"id": "summary-0", "text": "用户家在闵行，养了猫"}])
        msgs = session_to_langchain_messages(session, system_prompt="你是助手")
        # System(system_prompt) + System(摘要) 注入
        assert msgs[0].content == "你是助手"
        assert "用户家在闵行" in msgs[1].content

    def test_summaries_not_injected_when_disabled(self):
        from app.services.session_store import SessionState
        from app.agents.langgraph_agent import session_to_langchain_messages

        session = SessionState(session_id="s1", request_id="r1",
                               summaries=[{"id": "summary-0", "text": "旧历史"}])
        with patch("app.core.config.get_config", return_value=False):
            msgs = session_to_langchain_messages(session, system_prompt="你是助手")
        assert len(msgs) == 1  # 只有 system prompt，无摘要注入


# ---------------------------------------------------------------------------
# 4. 同会话保存链写序（后写必覆盖前写）
# ---------------------------------------------------------------------------

class TestSessionSaveOrdering:
    @pytest.mark.asyncio
    async def test_second_save_wins_even_if_first_retries(self):
        """第 1 次保存首次失败进入重试等待时，第 2 次（新快照）落库后，
        第 1 次不得再覆盖（旧覆盖新的竞态）。"""
        from app.services.session_store import SessionStore, SessionState

        store = SessionStore()
        writes: list[tuple[str, dict]] = []
        fail_first = {"count": 0}

        class FakeDB:
            async def sessions_upsert(self, session_id, data, user_id=""):
                if session_id == "s1" and fail_first["count"] == 0:
                    fail_first["count"] += 1
                    raise RuntimeError("simulated transient failure")
                writes.append((session_id, data))
        store._loaded = True

        s = SessionState(session_id="s1", request_id="r1", user_id="u1",
                         model_messages=[{"role": "user", "content": "v1"}])
        with patch("app.services.session_store.Database") as db_cls:
            db_cls.get.return_value = FakeDB()
            store._save_session_async(s)
            s2 = SessionState(session_id="s1", request_id="r1", user_id="u1",
                              model_messages=[{"role": "user", "content": "v2 新快照"}])
            store._save_session_async(s2)
            await asyncio.sleep(0.5)  # 等重试 + 链式等待完成

        # 最终落库的 s1 数据必须是 v2（新快照），且 v2 在 v1 重试之后写
        s1_writes = [d for sid, d in writes if sid == "s1"]
        contents = [w["model_messages"][0]["content"] for w in s1_writes]
        assert contents[-1] == "v2 新快照"

    @pytest.mark.asyncio
    async def test_delete_after_pending_save_not_resurrected(self):
        from app.services.session_store import SessionStore, SessionState

        store = SessionStore()
        ops: list[str] = []

        class FakeDB:
            async def sessions_upsert(self, session_id, data, user_id=""):
                ops.append("upsert")
            async def sessions_delete(self, session_id):
                ops.append("delete")
        store._loaded = True

        s = SessionState(session_id="s2", request_id="r1", user_id="u1",
                         model_messages=[{"role": "user", "content": "x"}])
        with patch("app.services.session_store.Database") as db_cls:
            db_cls.get.return_value = FakeDB()
            store._save_session_async(s)
            store._delete_session_async("s2")
            await asyncio.sleep(0.2)

        # 删除必须发生在（排队的）保存之后，不能被保存"复活"
        assert ops == ["upsert", "delete"]


# ---------------------------------------------------------------------------
# 5. WS 断连 emit 静音：本轮继续、落库不丢
# ---------------------------------------------------------------------------

class TestEmitMutingOnDisconnect:
    @pytest.mark.asyncio
    async def test_ws_send_failure_does_not_lose_turn(self):
        """ws_send 首次抛错后，emit 静音；_run_turn 继续执行（mock 验证被完整
        调用），store_session 在 finally 中执行。"""
        from app.agents.dispatcher import Dispatcher

        send_calls = {"n": 0}

        async def broken_ws_send(payload):
            send_calls["n"] += 1
            raise RuntimeError("WebSocket is disconnected")

        async def fake_run_turn(*args, **kw):
            # patch 到类上后 self 会作为首个参数传入
            _, event, session, query, ctx, emit = args
            # 模拟 _run_turn 内部多次 emit（最终 ToastStream + Finish）
            from app.schema.chat_schema import Dialog, Instruction
            await emit(Instruction.build_instruction(
                Dialog.Finish(success=True), "rid", session.session_id))
            session.model_messages.append({"role": "user", "content": query})
            session.model_messages.append({"role": "assistant", "content": "回复"})

        store = MagicMock()
        store.store_session = AsyncMock()
        dispatcher = Dispatcher.__new__(Dispatcher)
        dispatcher._session_store = store
        dispatcher._get_agent = AsyncMock(return_value=object())

        event = SimpleNamespace(
            header=SimpleNamespace(session_id="s1", request_id="rid"),
            payload={"query": "你好"},
        )
        session = MagicMock()
        session.session_id = "s1"
        store.get_or_create = AsyncMock(return_value=session)

        with patch.object(Dispatcher, "_run_turn", new=fake_run_turn), \
             patch.object(Dispatcher, "_prepare_context",
                          new=AsyncMock(return_value={})), \
             patch.object(Dispatcher, "_get_camera_state", return_value={}):
            await dispatcher.dispatch_stream(event, broken_ws_send, user_id="u1")

        # 第一次 send 失败后静音，但本轮走完并落库
        assert send_calls["n"] == 1
        store.store_session.assert_awaited_once()
