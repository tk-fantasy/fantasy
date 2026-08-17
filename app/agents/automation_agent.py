"""Automation Agent - 定时器兜底的规则评估(视觉/非视觉双循环)。

多路模式下 dhash 运动事件由 CameraManager._on_automation_trigger 自闭环驱动评估
(per-camera 节流 + _auto_sem 并发闸,见 camera_manager.py),不经本 agent。

评估管道拆分后本 agent 负责两条静默兜底循环:
- 视觉兜底(_silent_tick_loop):沿用原 silent_eval_* 配置,遍历各路取帧,
  只评 vision 规则;dhash 阈值拉满时视觉规则降级为纯此循环驱动。
- 非视觉兜底(_nonvision_tick_loop):time/weather 规则唯一的评估来源,
  无帧 evaluate(rule_types=("time","weather")),不依赖摄像头。
运动触发不再顺带评估 time/weather 规则(它们改由非视觉循环按间隔精确评估)。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AutomationAgent:
    """定时器兜底的自动化规则评估(视觉/非视觉双循环)。

    用 asyncio 后台任务替代 Actor 框架。dhash 运动事件由 CameraManager 自闭环
    驱动,本 agent 只负责两条静默兜底循环,各自独立开关/间隔/防抖。
    """

    def __init__(
        self,
        automation_service: Any = None,
        silent_eval_enabled: bool = True,
        silent_eval_interval: float = 60.0,
        camera_manager: Any = None,
        nonvision_silent_enabled: bool = True,
        nonvision_silent_interval: float = 30.0,
    ) -> None:
        self._automation_service = automation_service
        # 多路 CameraManager:_run_evaluation_cycle 遍历各路(各自 evaluate(camera_id=cid))。
        self._camera_manager = camera_manager

        # 视觉兜底(原定时器兜底):dhash 拉满即降级为纯定时器驱动
        self._silent_enabled = bool(silent_eval_enabled)
        self._silent_interval = max(5.0, float(silent_eval_interval))
        self._pending_silent_interval: float | None = None
        self._silent_task: asyncio.Task | None = None
        self._silent_debounce_task: asyncio.Task | None = None

        # 非视觉兜底:time/weather 规则的唯一评估来源,与摄像头完全解耦
        self._nonvision_enabled = bool(nonvision_silent_enabled)
        self._nonvision_interval = max(5.0, float(nonvision_silent_interval))
        self._pending_nonvision_interval: float | None = None
        self._nonvision_task: asyncio.Task | None = None
        self._nonvision_debounce_task: asyncio.Task | None = None

        self._eval_count: int = 0
        self._nonvision_eval_count: int = 0
        self._eval_running = False
        self._nonvision_eval_running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        if self._silent_enabled:
            self._start_silent_tick()
        if self._nonvision_enabled:
            self._start_nonvision_tick()
        logger.info(
            "AutomationAgent started (vision-silent=%s/%.1fs, nonvision-silent=%s/%.1fs)",
            self._silent_enabled, self._silent_interval,
            self._nonvision_enabled, self._nonvision_interval,
        )

    async def stop(self) -> None:
        self._running = False
        for task in (
            self._silent_task, self._silent_debounce_task,
            self._nonvision_task, self._nonvision_debounce_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._silent_task = self._silent_debounce_task = None
        self._nonvision_task = self._nonvision_debounce_task = None
        logger.info("AutomationAgent stopped")

    # ---------- 视觉兜底（原定时器兜底） ----------

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
        """热切换视觉兜底间隔。滑块拖动期间频繁调用，加 0.5s 防抖，
        松手/停止后才生效一次，并立刻评估一次（不刷屏）。

        必须在事件循环线程内调用（路由 handler 即在此）。
        """
        if self._loop is None:
            return
        self._pending_silent_interval = max(5.0, float(seconds))
        if self._silent_debounce_task and not self._silent_debounce_task.done():
            return  # 防抖等待中，新值已记下，到时取最新
        self._silent_debounce_task = self._loop.create_task(
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
        """开关视觉兜底。可在任意线程调用（call_soon_threadsafe 调度）。"""
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

    # ---------- 非视觉兜底（time/weather 独立循环） ----------

    async def _nonvision_tick_loop(self) -> None:
        while self._running and self._nonvision_enabled:
            try:
                await asyncio.sleep(self._nonvision_interval)
                if not self._running or not self._nonvision_enabled:
                    break
                await self._run_nonvision_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("AutomationAgent nonvision tick error")
                await asyncio.sleep(self._nonvision_interval)

    def _start_nonvision_tick(self) -> None:
        if self._loop is None:
            return
        if self._nonvision_task and not self._nonvision_task.done():
            return
        self._nonvision_task = self._loop.create_task(
            self._nonvision_tick_loop(), name="automation-nonvision-tick"
        )

    def _stop_nonvision_tick(self) -> None:
        if self._nonvision_task and not self._nonvision_task.done():
            self._nonvision_task.cancel()
        self._nonvision_task = None

    def _restart_nonvision_tick(self) -> None:
        self._stop_nonvision_tick()
        self._start_nonvision_tick()

    def set_nonvision_silent_interval(self, seconds: float) -> None:
        """热切换非视觉兜底间隔,0.5s 防抖,松手生效一次并立刻评估一次。

        必须在事件循环线程内调用（路由 handler 即在此）。
        """
        if self._loop is None:
            return
        self._pending_nonvision_interval = max(5.0, float(seconds))
        if self._nonvision_debounce_task and not self._nonvision_debounce_task.done():
            return
        self._nonvision_debounce_task = self._loop.create_task(
            self._debounced_apply_nonvision_interval(), name="automation-nonvision-debounce"
        )

    async def _debounced_apply_nonvision_interval(self) -> None:
        try:
            await asyncio.sleep(0.5)
            new_interval = self._pending_nonvision_interval or self._nonvision_interval
            self._pending_nonvision_interval = None
            changed = new_interval != self._nonvision_interval
            self._nonvision_interval = new_interval
            if self._nonvision_enabled:
                if changed:
                    self._restart_nonvision_tick()
                await self._run_nonvision_cycle()
        except asyncio.CancelledError:
            pass

    def set_nonvision_silent_enabled(self, enabled: bool) -> None:
        """开关非视觉兜底。可在任意线程调用（call_soon_threadsafe 调度）。

        关掉后 time/weather 规则将不再有任何评估来源(运动触发只评 vision)。
        """
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._apply_nonvision_enabled, bool(enabled))

    def _apply_nonvision_enabled(self, enabled: bool) -> None:
        if self._nonvision_enabled == enabled:
            return
        self._nonvision_enabled = enabled
        if enabled:
            self._start_nonvision_tick()
            logger.info("AutomationAgent nonvision tick enabled (%.1fs)", self._nonvision_interval)
        else:
            self._stop_nonvision_tick()
            logger.info("AutomationAgent nonvision tick disabled")

    # ---------- 评估 ----------

    async def _run_evaluation_cycle(self) -> None:
        """视觉兜底:遍历各路取帧,只评 vision 规则。

        并发保护：dhash 触发与视觉兜底可能重叠，丢弃重叠的一次。
        """
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
                    await self._automation_service.evaluate(
                        frames=frames, camera_id=cid, rule_types=("vision",)
                    )
        except Exception:
            logger.exception("AutomationAgent evaluation cycle error")
        finally:
            self._eval_running = False

    async def _run_nonvision_cycle(self) -> None:
        """非视觉兜底:无帧评估 time/weather 规则,与摄像头完全解耦。

        独立的并发保护(与视觉循环互不阻塞);不占 camera_manager 并发闸。
        """
        if self._nonvision_eval_running:
            logger.debug("Nonvision evaluation already running, skipping this tick")
            return
        self._nonvision_eval_running = True
        try:
            self._nonvision_eval_count += 1
            if self._automation_service is None:
                return
            await self._automation_service.evaluate(
                frames=None, camera_id="", rule_types=("time", "weather")
            )
        except Exception:
            logger.exception("AutomationAgent nonvision cycle error")
        finally:
            self._nonvision_eval_running = False
