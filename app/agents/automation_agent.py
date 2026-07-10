"""Automation Agent - periodic and event-driven rule evaluation.

Replaces the Actor-based implementation with a simple asyncio background task.
Triggers:
- Timer: every eval_interval seconds
- Camera inference callback: trigger_evaluate() from non-asyncio thread
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class AutomationAgent:
    """Periodic and event-driven automation rule evaluation.

    Uses a simple asyncio background task instead of the Actor framework.
    Thread-safe trigger via loop.call_soon_threadsafe for camera callbacks.
    """

    def __init__(
        self,
        automation_service: Any = None,
        camera_stream: Any = None,
        eval_interval: float = 10.0,
    ) -> None:
        self._automation_service = automation_service
        self._camera_stream = camera_stream
        self._eval_interval = eval_interval
        self._last_eval_at: float = 0.0
        self._eval_count: int = 0
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._tick_loop(), name="automation-tick")
        logger.info("AutomationAgent started (eval_interval=%.1fs)", self._eval_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AutomationAgent stopped")

    def trigger_evaluate(self) -> None:
        """线程安全触发评估（从摄像头推理线程调用）。"""
        if self._loop is None or not self._running:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._run_evaluation_cycle())
        )

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._eval_interval)
                now = time.time()
                if now - self._last_eval_at >= self._eval_interval:
                    self._last_eval_at = now
                    await self._run_evaluation_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("AutomationAgent tick error")

    async def _run_evaluation_cycle(self) -> None:
        self._eval_count += 1
        frames = await asyncio.to_thread(
            self._camera_stream.get_recent_frames
        ) if self._camera_stream else []
        if self._automation_service is not None:
            await self._automation_service.evaluate(frames=frames)
