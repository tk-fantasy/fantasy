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
        task.add_done_callback(self._tasks.discard)
        if on_done:
            task.add_done_callback(on_done)
        return task
    
    @property
    def pending_count(self) -> int:
        """当前待完成任务数。"""
        return len(self._tasks)


def create_task_manager() -> TaskManager:
    """工厂函数,创建新的TaskManager实例。"""
    return TaskManager()
