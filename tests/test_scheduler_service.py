"""Tests for SchedulerService — 定时任务调度器。

验证：
- compute_next_run 三种触发类型
- CRUD（add/list/delete/set_enabled）
- 到点执行 tool payload（mock tool_executor）
- 到点执行 message payload（mock dispatcher.dispatch）
- disabled 任务不被触发
- at 任务跑完自动禁用
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.scheduler_service import SchedulerService, compute_next_run, summarize_schedule


# ---------------------------------------------------------------------------
# compute_next_run — 纯函数测试
# ---------------------------------------------------------------------------

class TestComputeNextRun:
    def test_at_future(self):
        now = time.time()
        nxt = compute_next_run({"kind": "at", "at": "2030-01-01T08:00:00"}, now)
        assert nxt is not None
        assert nxt > now

    def test_at_past_returns_none(self):
        now = time.time()
        nxt = compute_next_run({"kind": "at", "at": "2020-01-01T08:00:00"}, now)
        assert nxt is None

    def test_every(self):
        now = 1_000_000.0
        nxt = compute_next_run({"kind": "every", "every_seconds": 3600}, now)
        assert nxt == pytest.approx(now + 3600)

    def test_every_zero_returns_none(self):
        nxt = compute_next_run({"kind": "every", "every_seconds": 0}, time.time())
        assert nxt is None

    def test_cron(self):
        # cron "0 8 * * *"：从某时刻算下一次 08:00 本地时间。
        # 用本地时区构造，避免跨时区断言歧义。
        from datetime import datetime
        # 2024-01-01 00:00:00 本地时间
        base_dt = datetime(2024, 1, 1, 0, 0, 0)
        now = base_dt.timestamp()
        # 下次 08:00 本地 = 当天 08:00
        expected = datetime(2024, 1, 1, 8, 0, 0).timestamp()
        nxt = compute_next_run({"kind": "cron", "expr": "0 8 * * *"}, now)
        assert nxt is not None
        assert nxt == pytest.approx(expected, abs=1)

    def test_cron_invalid_returns_none(self):
        nxt = compute_next_run({"kind": "cron", "expr": "not a cron"}, time.time())
        assert nxt is None

    def test_unknown_kind_returns_none(self):
        nxt = compute_next_run({"kind": "bogus"}, time.time())
        assert nxt is None

    def test_summarize(self):
        assert "08:00" in summarize_schedule({"kind": "at", "at": "2030-01-01T08:00:00"})
        assert "小时" in summarize_schedule({"kind": "every", "every_seconds": 3600})
        assert "cron" in summarize_schedule({"kind": "cron", "expr": "0 8 * * *"})


# ---------------------------------------------------------------------------
# SchedulerService — 用 mock 依赖构造
# ---------------------------------------------------------------------------

def _make_service():
    """构造带 mock 依赖的 SchedulerService。"""
    db = MagicMock()
    db.scheduled_tasks_all = AsyncMock(return_value=[])
    db.scheduled_task_insert = AsyncMock()
    db.scheduled_task_update = AsyncMock()
    db.scheduled_task_delete = AsyncMock()

    tool_executor = MagicMock()
    tool_executor.resolve_tool_name = MagicMock(side_effect=lambda n: n)
    tool_executor.execute_tool_by_name = AsyncMock(return_value={"success": True, "result": "ok"})

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value=[])
    dispatcher_ref = [dispatcher]

    session_store = MagicMock()
    session_store.list_summaries = AsyncMock(return_value=[{"id": "sess-1"}])

    # reminder kind 直接调 llm_chat_client.chat(messages, timeout) -> str
    llm_chat_client = MagicMock()
    llm_chat_client.chat = AsyncMock(return_value="该下班了，路上注意安全～")

    svc = SchedulerService(
        db=db,
        tool_executor=tool_executor,
        dispatcher_ref=dispatcher_ref,
        session_store=session_store,
        llm_chat_client=llm_chat_client,
    )
    return svc, db, tool_executor, dispatcher, session_store


class TestSchedulerCRUD:
    @pytest.mark.asyncio
    async def test_add_task(self):
        svc, db, *_ = _make_service()
        task = await svc.add_task({
            "name": "测试",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "message", "message": "hi"},
        })
        assert task["id"]
        assert task["enabled"] is True
        assert task["next_run_at"] is not None
        db.scheduled_task_insert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_tasks(self):
        svc, *_ = _make_service()
        await svc.add_task({"name": "a", "schedule": {"kind": "every", "every_seconds": 60}, "payload": {"kind": "message", "message": "x"}})
        await svc.add_task({"name": "b", "schedule": {"kind": "every", "every_seconds": 60}, "payload": {"kind": "message", "message": "y"}})
        tasks = await svc.list_tasks()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_delete_task(self):
        svc, db, *_ = _make_service()
        task = await svc.add_task({"name": "x", "schedule": {"kind": "every", "every_seconds": 60}, "payload": {"kind": "message", "message": "z"}})
        ok = await svc.delete_task(task["id"])
        assert ok is True
        assert await svc.list_tasks() == []
        db.scheduled_task_delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_enabled(self):
        svc, *_ = _make_service()
        task = await svc.add_task({"name": "x", "schedule": {"kind": "every", "every_seconds": 60}, "payload": {"kind": "message", "message": "z"}})
        updated = await svc.set_enabled(task["id"], False)
        assert updated["enabled"] is False
        assert updated["next_run_at"] is None


# ---------------------------------------------------------------------------
# 执行路径
# ---------------------------------------------------------------------------

class TestSchedulerExecution:
    @pytest.mark.asyncio
    async def test_execute_tool_payload(self):
        svc, db, tool_executor, *_ = _make_service()
        task = await svc.add_task({
            "name": "关灯",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "tool", "tool_name": "ha_devices___call_service", "tool_input": {"domain": "light"}},
        })
        await svc._execute_task(task)
        tool_executor.execute_tool_by_name.assert_awaited_once()
        assert task["last_status"] == "success"
        # 周期任务执行后应重算 next_run
        assert task["next_run_at"] is not None

    @pytest.mark.asyncio
    async def test_execute_message_payload(self):
        svc, db, _, dispatcher, session_store = _make_service()
        task = await svc.add_task({
            "name": "提醒",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "message", "message": "该起床了"},
            "user_id": "u1",
        })
        await svc._execute_task(task)
        dispatcher.dispatch.assert_awaited_once()
        # dispatch 必须带上创建者 user_id，走 per-user agent（而非全局 agent 兜底）
        assert dispatcher.dispatch.call_args.kwargs.get("user_id") == "u1"
        session_store.list_summaries.assert_awaited()
        assert task["last_status"] == "success"

    @pytest.mark.asyncio
    async def test_message_payload_no_user_id_refuses(self):
        """无 user_id 的 message 任务应拒绝执行（避免回退全局 agent 撞 Connection error）。

        旧任务由 _load_tasks 打标禁用，正常不会执行；此测试覆盖 _execute_task 直接
        被调时的兜底防线。
        """
        svc, db, _, dispatcher, session_store = _make_service()
        task = await svc.add_task({
            "name": "提醒",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "message", "message": "该起床了"},
            # 故意不传 user_id
        })
        await svc._execute_task(task)
        dispatcher.dispatch.assert_not_awaited()
        assert task["last_status"] == "failed"
        assert "user_id" in (task["last_error"] or "")

    @pytest.mark.asyncio
    async def test_message_payload_no_session_skips(self):
        svc, db, _, dispatcher, session_store = _make_service()
        session_store.list_summaries = AsyncMock(return_value=[])
        task = await svc.add_task({
            "name": "提醒",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "message", "message": "该起床了"},
            "user_id": "u1",
        })
        await svc._execute_task(task)
        dispatcher.dispatch.assert_not_awaited()
        assert task["last_status"] == "failed"
        assert "无可用会话" in (task["last_error"] or "")

    @pytest.mark.asyncio
    async def test_execute_reminder_payload(self):
        """reminder payload 应直接调 LLM 生成提醒，不走 dispatcher/ReAct。"""
        from app.services.session_store import SessionState
        svc, db, _, dispatcher, session_store = _make_service()
        # 补 session_store 的 get_or_create / store_session
        session = SessionState(session_id="sess-1", request_id="r1")
        session_store.get_or_create = AsyncMock(return_value=session)
        session_store.store_session = AsyncMock()

        task = await svc.add_task({
            "name": "下班提醒",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "reminder", "intent": "下班提醒", "original": "在18点27分提醒我下班"},
        })
        await svc._execute_task(task)

        # LLM 被调一次，dispatcher 不该被调（绕开 ReAct）
        svc._llm_chat_client.chat.assert_awaited_once()
        dispatcher.dispatch.assert_not_awaited()
        # 回复写进 session.model_messages（user 指令 + assistant 回复）
        roles = [m["role"] for m in session.model_messages]
        assert "user" in roles and "assistant" in roles
        reply = next(m["content"] for m in session.model_messages if m["role"] == "assistant")
        assert reply  # 非空
        session_store.store_session.assert_awaited_once()
        assert task["last_status"] == "success"

    @pytest.mark.asyncio
    async def test_reminder_payload_llm_receives_original_intent(self):
        """投给 LLM 的消息应包含用户原始意图。"""
        from app.services.session_store import SessionState
        svc, db, _, _, session_store = _make_service()
        session = SessionState(session_id="sess-1", request_id="r1")
        session_store.get_or_create = AsyncMock(return_value=session)
        session_store.store_session = AsyncMock()

        task = await svc.add_task({
            "name": "喝水",
            "schedule": {"kind": "every", "every_seconds": 3600},
            "payload": {"kind": "reminder", "intent": "提醒喝水"},
        })
        await svc._execute_task(task)
        # 检查投给 LLM 的 messages 含 intent
        call_messages = svc._llm_chat_client.chat.call_args.args[0]
        last_user = next(m["content"] for m in reversed(call_messages) if m["role"] == "user")
        assert "提醒喝水" in last_user
        assert "定时提醒触发" in last_user

    @pytest.mark.asyncio
    async def test_reminder_payload_no_session_fails(self):
        """无会话时 reminder 应记失败，不调 LLM。"""
        svc, db, _, dispatcher, session_store = _make_service()
        session_store.list_summaries = AsyncMock(return_value=[])
        task = await svc.add_task({
            "name": "提醒",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "reminder", "intent": "下班提醒", "original": "提醒下班"},
        })
        await svc._execute_task(task)
        svc._llm_chat_client.chat.assert_not_awaited()
        dispatcher.dispatch.assert_not_awaited()
        assert task["last_status"] == "failed"
        assert "无可用会话" in (task["last_error"] or "")

    @pytest.mark.asyncio
    async def test_tool_failure_records_error(self):
        svc, db, tool_executor, *_ = _make_service()
        tool_executor.execute_tool_by_name = AsyncMock(return_value={"success": False, "error": "device offline"})
        task = await svc.add_task({
            "name": "关灯",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "tool", "tool_name": "ha_devices___call_service", "tool_input": {}},
        })
        await svc._execute_task(task)
        assert task["last_status"] == "failed"
        assert "device offline" in (task["last_error"] or "")

    @pytest.mark.asyncio
    async def test_at_task_disabled_after_run(self):
        svc, db, tool_executor, *_ = _make_service()
        # at 任务：next_run 设为过去，触发后应自动禁用
        task = await svc.add_task({
            "name": "一次性",
            "schedule": {"kind": "at", "at": "2030-01-01T08:00:00"},
            "payload": {"kind": "tool", "tool_name": "x", "tool_input": {}},
        })
        # 手动改成过去时刻触发
        task["next_run_at"] = time.time() - 1
        await svc._execute_task(task)
        assert task["enabled"] is False
        assert task["next_run_at"] is None
        assert task["last_status"] == "success"


# ---------------------------------------------------------------------------
# _load_tasks — 升级迁移：无 user_id 旧任务打标禁用
# ---------------------------------------------------------------------------

class TestSchedulerLoadTasksMigration:
    @pytest.mark.asyncio
    async def test_pre_upgrade_task_without_user_id_disabled(self):
        """升级前创建的任务（无 user_id）启动时应被禁用，避免到点回退全局 agent 报错。"""
        svc, db, *_ = _make_service()
        # 模拟 DB 里有一条升级前的旧任务：无 user_id、enabled=True
        db.scheduled_tasks_all = AsyncMock(return_value=[{
            "id": "old-task",
            "name": "旧任务",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "message", "message": "hi"},
            "enabled": True,
            # 注意：没有 user_id 字段
        }])
        await svc._load_tasks()
        task = svc._tasks["old-task"]
        assert task["enabled"] is False
        assert task["next_run_at"] is None
        assert task["last_status"] == "interrupted"
        assert "创建者" in (task["last_error"] or "")
        # 禁用状态已持久化
        db.scheduled_task_update.assert_awaited()

    @pytest.mark.asyncio
    async def test_task_with_user_id_loaded_normally(self):
        """升级后带 user_id 的任务正常载入，不被迁移逻辑误伤。"""
        svc, db, *_ = _make_service()
        db.scheduled_tasks_all = AsyncMock(return_value=[{
            "id": "new-task",
            "name": "新任务",
            "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "message", "message": "hi"},
            "enabled": True,
            "user_id": "u1",
        }])
        await svc._load_tasks()
        task = svc._tasks["new-task"]
        assert task["enabled"] is True
        assert task["next_run_at"] is not None
        assert task["last_status"] != "interrupted"


# ---------------------------------------------------------------------------
# tick 循环 — disabled 不触发
# ---------------------------------------------------------------------------

class TestSchedulerTick:
    @pytest.mark.asyncio
    async def test_disabled_task_not_executed(self):
        svc, db, tool_executor, *_ = _make_service()
        task = await svc.add_task({
            "name": "停用的",
            "schedule": {"kind": "every", "every_seconds": 1},
            "payload": {"kind": "tool", "tool_name": "x", "tool_input": {}},
        })
        await svc.set_enabled(task["id"], False)
        # next_run 即使在过去，disabled 也不执行
        task["next_run_at"] = time.time() - 100
        await svc._tick_once()
        # tick_once 是 spawn 执行的，给一点时间让协程跑完（不应有）
        await asyncio.sleep(0.05)
        tool_executor.execute_tool_by_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_due_task_gets_executed(self):
        svc, db, tool_executor, *_ = _make_service()
        task = await svc.add_task({
            "name": "到点的",
            "schedule": {"kind": "every", "every_seconds": 1},
            "payload": {"kind": "tool", "tool_name": "x", "tool_input": {}},
        })
        task["next_run_at"] = time.time() - 1
        await svc._tick_once()
        # tick_once spawn 了执行协程，等它完成
        await asyncio.sleep(0.1)
        tool_executor.execute_tool_by_name.assert_awaited_once()
