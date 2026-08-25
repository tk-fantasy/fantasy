"""跨线程向事件循环投递协程的有界等待工具。

背景：`asyncio.run_coroutine_threadsafe(coro, loop).result()` 在 loop
已停止（但未关闭）时 future 永远不会被调度，裸 result() 无限阻塞。
这类代码跑在非 daemon 线程池工作线程里时，解释器退出阶段
`threading._shutdown` 会 join 它——测试全过但 pytest 进程永远不退出。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging

logger = logging.getLogger(__name__)

# 单次投递的默认等待上限；正常 embed/chat 请求远低于此值
DEFAULT_TIMEOUT_SECONDS = 120


class LoopUnavailableError(RuntimeError):
    """目标事件循环未绑定/已停止/已关闭，无法投递协程。

    不可重试：循环死亡后重试同样失败，调用方应立即放弃整个任务。
    """


def submit_and_wait(
    coro,
    loop: asyncio.AbstractEventLoop | None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
):
    """从工作线程向 loop 投递协程并限时等待结果。

    - loop 不可用（None/关闭/未运行）→ LoopUnavailableError，立即失败；
    - 等待超时 → 取消 future 后抛 TimeoutError，避免无限挂起。
    """
    if loop is None or loop.is_closed() or not loop.is_running():
        state = "未绑定" if loop is None else f"closed={loop.is_closed()}, running={loop.is_running()}"
        raise LoopUnavailableError(f"事件循环不可用({state})")
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as e:
        fut.cancel()
        raise TimeoutError(f"等待事件循环执行结果超时({timeout_seconds}s)") from e
