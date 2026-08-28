"""异步任务管理工具。

提供统一的后台任务管理模式,自动跟踪任务生命周期,避免任务被GC回收。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

logger = logging.getLogger(__name__)


class TaskManager:
    """后台任务管理器,自动跟踪任务生命周期。
    
    使用示例:
        task_mgr = TaskManager()
        
        # 启动后台任务
        task_mgr.spawn(some_async_function())
        
        # 带名称和回调
        task_mgr.spawn(
            another_async_function(),
            name="my_task",
            on_done=lambda t: logger.info("Task completed")
        )
        
        # 查询待完成任务数
        print(f"Pending tasks: {task_mgr.pending_count}")
    """
    
    def __init__(self):
        self._tasks: set[asyncio.Task] = set()

    def _on_done(self, task: asyncio.Task) -> None:
        """done 回调：移出跟踪表 + 异常留痕。此前只 discard，后台任务崩溃
        不留任何日志，只能靠 GC 时的 'exception was never retrieved' 显形。"""
        self._tasks.discard(task)
        if task.cancelled():
            logger.info("Background task cancelled: %s", task.get_name())
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Background task crashed: %s: %r",
                task.get_name(), exc, exc_info=exc,
            )

    def spawn(
        self,
        coro,
        *,
        name: str | None = None,
        on_done: Callable[[asyncio.Task], None] | None = None
    ) -> asyncio.Task:
        """创建后台任务并自动管理生命周期。

        Args:
            coro: 协程对象
            name: 任务名称(用于调试和日志)
            on_done: 任务完成时的额外回调函数

        Returns:
            创建的Task对象
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        if on_done:
            task.add_done_callback(on_done)
        return task

    async def shutdown(self, timeout: float = 5.0) -> None:
        """停机收口：cancel 全部存活任务并等待其收尾。

        此前经 spawn 的任务在应用关停时既不 cancel 也不 await，随事件循环
        关闭暴毙（auto_update 安装中途被杀可能留半安装态）。"""
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if not tasks:
            return
        logger.info("Shutting down %d background task(s)", len(tasks))
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            logger.warning("Background task did not finish in time: %s", t.get_name())

    @property
    def pending_count(self) -> int:
        """当前待完成任务数。"""
        return len(self._tasks)


def create_task_manager() -> TaskManager:
    """工厂函数,创建新的TaskManager实例。"""
    return TaskManager()
