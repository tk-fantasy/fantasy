"""Tests for small untested core/util/service modules。

覆盖此前零测试的模块：
- app/core/rate_limit.py — RateLimiter 滑动窗口
- app/core/tracing.py — request_id ContextVar / RequestIdFilter
- app/utils/async_utils.py — TaskManager
- app/services/health_check.py — HealthChecker
- app/services/metrics_service.py — MetricsService
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.rate_limit import RateLimiter, global_limiter
from app.core.tracing import RequestIdFilter, new_request_id, request_id_var, set_request_id
from app.utils.async_utils import TaskManager, create_task_manager
from app.services.health_check import HealthChecker
from app.services.metrics_service import MetricsService


class TestRateLimiter:
    """RateLimiter 滑动窗口。"""

    def test_allows_under_limit(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        assert limiter.check("ip1") is True
        assert limiter.check("ip1") is True
        assert limiter.check("ip1") is True

    def test_blocks_over_limit(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        limiter.check("ip1")
        limiter.check("ip1")
        assert limiter.check("ip1") is False

    def test_different_keys_independent(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        assert limiter.check("ip1") is True
        assert limiter.check("ip2") is True
        assert limiter.check("ip1") is False  # ip1 已耗尽

    def test_window_expiry_allows_again(self):
        """窗口过期后旧记录被清理，可再次通过。"""
        limiter = RateLimiter(max_requests=1, window_seconds=1)
        assert limiter.check("ip1") is True
        assert limiter.check("ip1") is False
        time.sleep(1.1)
        assert limiter.check("ip1") is True

    def test_global_limiter_exists(self):
        assert global_limiter is not None
        # global_limiter 配置为 120/min
        assert global_limiter.check("test-key") is True


class TestTracing:
    """request_id ContextVar 与日志过滤器。"""

    def test_new_request_id_is_8_chars(self):
        rid = new_request_id()
        assert len(rid) == 8

    def test_new_request_id_unique(self):
        ids = {new_request_id() for _ in range(100)}
        assert len(ids) >= 95  # 极小概率碰撞

    def test_set_and_get_request_id(self):
        set_request_id("trace-abc")
        assert request_id_var.get() == "trace-abc"
        set_request_id("-")  # reset

    def test_default_request_id_is_dash(self):
        set_request_id("-")
        assert request_id_var.get() == "-"

    def test_filter_injects_request_id_into_record(self):
        set_request_id("rid-xyz")
        filt = RequestIdFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", None, None)
        filt.filter(record)
        assert record.request_id == "rid-xyz"  # type: ignore[attr-defined]
        set_request_id("-")

    def test_filter_returns_true(self):
        filt = RequestIdFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", None, None)
        assert filt.filter(record) is True

    def test_filter_uses_default_when_unset(self):
        set_request_id("-")
        filt = RequestIdFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", None, None)
        filt.filter(record)
        assert record.request_id == "-"  # type: ignore[attr-defined]


class TestTaskManager:
    """TaskManager 后台任务管理（spawn 需运行中的事件循环，全部 async）。"""

    @pytest.mark.asyncio
    async def test_spawn_creates_task(self):
        mgr = create_task_manager()
        async def work():
            await asyncio.sleep(0.01)
        task = mgr.spawn(work())
        assert isinstance(task, asyncio.Task)
        assert mgr.pending_count >= 1
        await task

    @pytest.mark.asyncio
    async def test_pending_count_decreases_after_completion(self):
        mgr = TaskManager()
        async def quick():
            return 42
        task = mgr.spawn(quick())
        await task
        await asyncio.sleep(0.01)  # 让 done_callback 执行 discard
        assert mgr.pending_count == 0

    @pytest.mark.asyncio
    async def test_spawn_with_name(self):
        mgr = TaskManager()
        async def work():
            return 1
        task = mgr.spawn(work(), name="my-task")
        assert task.get_name() == "my-task"
        await task

    @pytest.mark.asyncio
    async def test_spawn_with_on_done_callback(self):
        mgr = TaskManager()
        results = []
        async def work():
            return "done"
        def on_done(t):
            results.append(t.result())
        task = mgr.spawn(work(), on_done=on_done)
        await task
        await asyncio.sleep(0.01)  # 让 on_done 回调执行
        assert results == ["done"]

    def test_create_task_manager_returns_instance(self):
        mgr = create_task_manager()
        assert isinstance(mgr, TaskManager)

    @pytest.mark.asyncio
    async def test_task_completes_and_is_tracked(self):
        mgr = TaskManager()
        async def work():
            await asyncio.sleep(0.01)
            return "ok"
        task = mgr.spawn(work())
        result = await task
        assert result == "ok"
        # 任务完成后从 _tasks 移除（done_callback discard）
        await asyncio.sleep(0.01)
        assert mgr.pending_count == 0


class TestHealthChecker:
    """HealthChecker 外部服务可用性。"""

    @pytest.mark.asyncio
    async def test_check_ha_success(self):
        checker = HealthChecker()
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[{"e": 1}, {"e": 2}])
        result = await checker.check_ha(ha_client)
        assert result is True
        assert checker.ha_available is True

    @pytest.mark.asyncio
    async def test_check_ha_no_entities(self):
        checker = HealthChecker()
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[])
        result = await checker.check_ha(ha_client)
        assert result is False
        assert checker.ha_available is False

    @pytest.mark.asyncio
    async def test_check_ha_exception(self):
        checker = HealthChecker()
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(side_effect=RuntimeError("conn refused"))
        result = await checker.check_ha(ha_client)
        assert result is False

    @pytest.mark.asyncio
    async def test_check_llm_disabled(self):
        checker = HealthChecker()
        llm = MagicMock()
        llm.enabled = False
        result = await checker.check_llm(llm)
        assert result is False

    @pytest.mark.asyncio
    async def test_check_llm_success(self):
        checker = HealthChecker()
        llm = MagicMock()
        llm.enabled = True
        llm.chat = AsyncMock(return_value="hi")
        result = await checker.check_llm(llm)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_llm_empty_response(self):
        checker = HealthChecker()
        llm = MagicMock()
        llm.enabled = True
        llm.chat = AsyncMock(return_value=None)
        result = await checker.check_llm(llm)
        assert result is False

    @pytest.mark.asyncio
    async def test_check_all_returns_both(self):
        checker = HealthChecker()
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[{"e": 1}])
        llm = MagicMock()
        llm.enabled = True
        llm.chat = AsyncMock(return_value="ok")
        status = await checker.check_all(ha_client, llm)
        assert status == {"ha": True, "llm": True}

    def test_get_status_reflects_state(self):
        checker = HealthChecker()
        checker.ha_available = True
        checker.llm_available = False
        status = checker.get_status()
        assert status == {"ha_available": True, "llm_available": False}


class TestMetricsService:
    """MetricsService 内存指标。"""

    def test_record_request_increments_count(self):
        svc = MetricsService()
        svc.record_request(0.1)
        svc.record_request(0.2)
        snap = svc.snapshot()
        assert snap["http"]["total"] == 2
        assert snap["http"]["errors"] == 0

    def test_record_request_error(self):
        svc = MetricsService()
        svc.record_request(0.1, error=True)
        snap = svc.snapshot()
        assert snap["http"]["errors"] == 1

    def test_record_tool_call(self):
        svc = MetricsService()
        svc.record_tool_call("call_service")
        svc.record_tool_call("call_service")
        svc.record_tool_call("search", error=True)
        snap = svc.snapshot()
        assert snap["tools"]["calls"]["call_service"] == 2
        assert snap["tools"]["calls"]["search"] == 1
        assert snap["tools"]["errors"]["search"] == 1

    def test_record_llm_call(self):
        svc = MetricsService()
        svc.record_llm_call()
        svc.record_llm_call(error=True)
        snap = svc.snapshot()
        assert snap["llm"]["calls"] == 2
        assert snap["llm"]["errors"] == 1

    def test_record_automation_eval(self):
        svc = MetricsService()
        svc.record_automation_eval()
        svc.record_automation_eval()
        snap = svc.snapshot()
        assert snap["automation"]["evals"] == 2

    def test_latency_stats(self):
        svc = MetricsService()
        for lat in [0.1, 0.2, 0.3, 0.4, 0.5]:
            svc.record_request(lat)
        snap = svc.snapshot()
        assert snap["http"]["latency_samples"] == 5
        assert snap["http"]["avg_latency_s"] > 0
        assert snap["http"]["p95_latency_s"] >= snap["http"]["avg_latency_s"]

    def test_empty_snapshot(self):
        svc = MetricsService()
        snap = svc.snapshot()
        assert snap["http"]["total"] == 0
        assert snap["http"]["avg_latency_s"] == 0.0
        assert snap["http"]["p95_latency_s"] == 0.0

    def test_latency_deque_maxlen(self):
        """latency 样本上限 200，超出后丢弃旧值。"""
        svc = MetricsService()
        for _ in range(300):
            svc.record_request(0.01)
        snap = svc.snapshot()
        assert snap["http"]["latency_samples"] == 200
        assert snap["http"]["total"] == 300  # count 不限
