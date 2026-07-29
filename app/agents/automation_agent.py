"""Automation Agent - dhash 事件触发 + 定时器兜底的规则评估。

两条入口：
- dhash 运动触发：camera_stream 检测到运动调 trigger_evaluate()（自带 ≥3s 节流，
  防 0-result 规则被连续运动 300/min 轰炸；冷却只在 result==1 后武装，挡不住一直
  返回 0 的规则）。
- 定时器兜底：_silent_tick_loop 按 silent_eval_interval 周期评估；dhash 阈值拉满
  （distance > 256 永不成立）时降级为纯定时器，等价旧轮询策略。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class AutomationAgent:
    """dhash 事件触发 + 定时器兜底的自动化规则评估。

    用 asyncio 后台任务替代 Actor 框架。摄像头线程通过 loop.call_soon_threadsafe
    跨线程触发评估。
    """

    def __init__(
        self,
        automation_service: Any = None,
        camera_stream: Any = None,
        min_trigger_interval: float = 3.0,
        silent_eval_enabled: bool = True,
        silent_eval_interval: float = 60.0,
    ) -> None:
        self._automation_service = automation_service
        self._camera_stream = camera_stream
        # dhash 触发节流闸：≥ min_trigger_interval 才放行一次 trigger。
        # 复用 vision.min_infer_interval_seconds（默认 3s）。
        self._min_trigger_interval = max(0.5, float(min_trigger_interval))
        self._last_trigger_at: float = 0.0

        # 定时器兜底（静默推理）：dhash 拉满即降级为纯定时器驱动
        self._silent_enabled = bool(silent_eval_enabled)
        self._silent_interval = max(5.0, float(silent_eval_interval))
        self._pending_silent_interval: float | None = None
        self._silent_task: asyncio.Task | None = None
        self._debounce_task: asyncio.Task | None = None

        self._eval_count: int = 0
        self._eval_running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        if self._silent_enabled:
            self._start_silent_tick()
        logger.info(
            "AutomationAgent started (min_trigger=%.1fs, silent=%s/%.1fs)",
            self._min_trigger_interval, self._silent_enabled, self._silent_interval,
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._silent_task, self._debounce_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._silent_task = self._debounce_task = None
        logger.info("AutomationAgent stopped")

    # ---------- dhash 事件入口 ----------

    def trigger_evaluate(self) -> None:
        """摄像头线程调用的 dhash 运动触发入口。

        自带 ≥ min_trigger_interval 节流：节流窗口内的重复 trigger 直接丢弃。
        这是防 0-result 规则被连续运动 300/min 轰炸的关键——冷却只在 result==1
        后武装（update_trigger_time），挡不住一直返回 0 的规则。
        """
        if self._loop is None or not self._running:
            return
        now = time.time()
        if now - self._last_trigger_at < self._min_trigger_interval:
            return  # 节流窗口内，丢弃
        self._last_trigger_at = now
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._run_evaluation_cycle())
        )

    # ---------- 定时器兜底（静默推理） ----------

    async def _silent_tick_loop(self) -> None:
        while self._running and self._silent_enabled:
            try:
                await asyncio.sleep(self._silent_interval)
                if not self._running or not self._silent_enabled:
                    break
                await self._run_evaluation_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("AutomationAgent silent tick error")
                await asyncio.sleep(self._silent_interval)

    def _start_silent_tick(self) -> None:
        if self._loop is None:
            return
        if self._silent_task and not self._silent_task.done():
            return
        self._silent_task = self._loop.create_task(
            self._silent_tick_loop(), name="automation-silent-tick"
        )

    def _stop_silent_tick(self) -> None:
        if self._silent_task and not self._silent_task.done():
            self._silent_task.cancel()
        self._silent_task = None

    def _restart_silent_tick(self) -> None:
        self._stop_silent_tick()
        self._start_silent_tick()

    def set_silent_interval(self, seconds: float) -> None:
        """热切换静默间隔。滑块拖动期间频繁调用，加 0.5s 防抖，
        松手/停止后才生效一次，并立刻评估一次（不刷屏）。

        必须在事件循环线程内调用（路由 handler 即在此）。
        """
        if self._loop is None:
            return
        self._pending_silent_interval = max(5.0, float(seconds))
        if self._debounce_task and not self._debounce_task.done():
            return  # 防抖等待中，新值已记下，到时取最新
        self._debounce_task = self._loop.create_task(
            self._debounced_apply_interval(), name="automation-debounce"
        )

    async def _debounced_apply_interval(self) -> None:
        try:
            await asyncio.sleep(0.5)
            new_interval = self._pending_silent_interval or self._silent_interval
            self._pending_silent_interval = None
            changed = new_interval != self._silent_interval
            self._silent_interval = new_interval
            if self._silent_enabled:
                if changed:
                    self._restart_silent_tick()
                # 切换后立刻评估一次
                await self._run_evaluation_cycle()
        except asyncio.CancelledError:
            pass

    def set_silent_enabled(self, enabled: bool) -> None:
        """开关定时器兜底。可在任意线程调用（call_soon_threadsafe 调度）。"""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._apply_silent_enabled, bool(enabled))

    def _apply_silent_enabled(self, enabled: bool) -> None:
        if self._silent_enabled == enabled:
            return
        self._silent_enabled = enabled
        if enabled:
            self._start_silent_tick()
            logger.info("AutomationAgent silent tick enabled (%.1fs)", self._silent_interval)
        else:
            self._stop_silent_tick()
            logger.info("AutomationAgent silent tick disabled")

    # ---------- 评估 ----------

    async def _run_evaluation_cycle(self) -> None:
        # 并发保护：dhash 触发与定时器兜底可能重叠，丢弃重叠的一次。
        # （旧 _tick_loop 串行 await，但 trigger_evaluate 可并发，本实现统一加闸。）
        if self._eval_running:
            logger.debug("Evaluation already running, skipping this trigger")
            return
        self._eval_running = True
        try:
            self._eval_count += 1
            frames = await asyncio.to_thread(
                self._camera_stream.get_recent_frames
            ) if self._camera_stream else []
            if self._automation_service is not None:
                await self._automation_service.evaluate(frames=frames)
        except Exception:
            logger.exception("AutomationAgent evaluation cycle error")
        finally:
            self._eval_running = False
