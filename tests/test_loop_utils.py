"""app/core/loop_utils.py 测试：跨线程投递协程的有界等待。

submit_and_wait 存在的意义是消灭两类事故：loop 已死时 run_coroutine_threadsafe
的 future 永不被调度导致无限阻塞（LoopUnavailableError 立即失败），以及正常
loop 上协程挂死时裸 result() 无限等（TimeoutError 且 cancel future）。
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from app.core.loop_utils import DEFAULT_TIMEOUT_SECONDS, LoopUnavailableError, submit_and_wait


def _bg_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """起一个后台线程跑事件循环，返回 (loop, thread)。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True, name="bg-loop")
    t.start()
    return loop, t


def _stop(loop: asyncio.AbstractEventLoop, t: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)


async def _add(a: int, b: int) -> int:
    await asyncio.sleep(0.01)
    return a + b


class TestSubmitAndWait:
    def test_returns_result_from_running_loop(self):
        loop, t = _bg_loop()
        try:
            assert submit_and_wait(_add(1, 2), loop, timeout_seconds=5) == 3
        finally:
            _stop(loop, t)

    def test_none_loop_raises_unavailable(self):
        coro = _add(1, 2)
        with pytest.raises(LoopUnavailableError, match="未绑定"):
            submit_and_wait(coro, None)
        coro.close()

    def test_stopped_loop_raises_unavailable(self):
        """loop 停止但未关闭——最危险的场景，必须立即失败而非挂死。"""
        loop, t = _bg_loop()
        try:
            _stop(loop, t)
            coro = _add(1, 2)
            with pytest.raises(LoopUnavailableError):
                submit_and_wait(coro, loop, timeout_seconds=2)
            coro.close()
        finally:
            loop.close()

    def test_timeout_cancels_future(self):
        loop, t = _bg_loop()
        release = asyncio.Event()
        try:
            async def slow():
                await release.wait()

            with pytest.raises(TimeoutError, match="超时"):
                submit_and_wait(slow(), loop, timeout_seconds=0.2)
        finally:
            # 先唤醒挂死协程再停循环，避免遗留 pending task
            loop.call_soon_threadsafe(release.set)
            _stop(loop, t)

    def test_default_timeout_is_generous(self):
        assert DEFAULT_TIMEOUT_SECONDS >= 30  # 正常 embed/chat 请求不应被默认值误杀
