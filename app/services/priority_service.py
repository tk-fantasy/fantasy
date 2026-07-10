from __future__ import annotations

import threading
from contextlib import contextmanager


class InteractivePriority:
    """标记"用户交互请求正在占用模型"。

    本地 LLM 同时只能跑一个请求,后台视觉轮询让位给用户聊天/问图,
    避免用户等 10 秒还排在后台任务后面。
    """

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            self._count += 1

    def release(self) -> None:
        with self._lock:
            self._count = max(0, self._count - 1)

    @contextmanager
    def hold(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def active(self) -> bool:
        with self._lock:
            return self._count > 0


interactive_priority = InteractivePriority()
