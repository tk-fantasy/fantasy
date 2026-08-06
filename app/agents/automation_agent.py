"""Automation Agent - 定时器兜底的规则评估。

多路模式下 dhash 运动事件由 CameraManager._on_automation_trigger 自闭环驱动评估
(per-camera 节流 + _auto_sem 并发闸,见 camera_manager.py),不经本 agent。
本 agent 只剩定时器兜底:_silent_tick_loop 按 silent_eval_interval 周期遍历各路
evaluate;dhash 阈值拉满(distance > 256 永不成立)时降级为纯定时器,等价旧轮询策略。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AutomationAgent:
    """定时器兜底的自动化规则评估。

    用 asyncio 后台任务替代 Actor 框架。dhash 运动事件由 CameraManager 自闭环
    驱动,本 agent 只负责定时器兜底(_silent_tick_loop 周期遍历各路 evaluate)。
    """

    def __init__(
        self,
        automation_service: Any = None,
        silent_eval_enabled: bool = True,
        silent_eval_interval: float = 60.0,
        camera_manager: Any = None,
    ) -> None:
        self._automation_service = automation_service
        # 多路 CameraManager:_run_evaluation_cycle 遍历各路(各自 evaluate(camera_id=cid))。
        self._camera_manager = camera_manager

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
            "AutomationAgent started (silent=%s/%.1fs)",
            self._silent_enabled, self._silent_interval,
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
        if self._eval_running:
            logger.debug("Evaluation already running, skipping this trigger")
            return
        self._eval_running = True
        try:
            self._eval_count += 1
            if self._automation_service is None:
                return
            # 多路:遍历 manager 各路,各自取帧 + evaluate(camera_id=cid)。
            # camera_manager 是唯一摄像头来源;无 manager / 无路时不评估。
            if self._camera_manager is None:
                return
            for cam in self._camera_manager.list_cameras():
                cid = cam["id"]
                frames = await asyncio.to_thread(
                    self._camera_manager.get_recent_frames, cid, 3
                )
                if frames:
                    await self._automation_service.evaluate(frames=frames, camera_id=cid)
        except Exception:
            logger.exception("AutomationAgent evaluation cycle error")
        finally:
            self._eval_running = False
