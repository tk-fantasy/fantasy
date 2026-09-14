"""通用入站缓冲管道 —— 宿主侧集成的多人对话排队/合并/限流组件。

解决"每条消息一个 fire-and-forget task"模型在多人/群聊下的结构性问题：
- 无保序：同会话连发消息并发处理，回复乱序、上下文碎裂；
- 无合并：连发多条被拆成多轮 LLM 调用；
- 无并发上限：多人同时触发，LLM 调用打满；
- 无超时反馈：LLM 卡住用户干等。

设计（解耦）：管道不认识任何具体集成——
- handler: ``async (chat_key, query, meta) -> None``，调用方自行完成"调 LLM + 发回复"；
- notify: ``async (chat_key, text) -> None``，管道生成的提示（处理中/超时/丢弃）经它发出；
- 每个会话一条"车道"（lane）：队列 + 单 worker 串行消费（同会话保序），车道间并行，
  全局 semaphore 限并发；飞书/Telegram 等只是使用者。

语义细节：
- 合并窗口从批内首条消息起算、固定不顺延（避免持续打字无限推迟处理）；
- 窗口内到达的消息按到达顺序以 "\n".join 合并为一个 query；
- 队列满丢最旧（保新弃旧：宁可少答不串答），经 notify 告知；
- handler 超时被取消，车道存活继续服务后续消息；单条异常不跨车道扩散。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Handler = Callable[[str, str, dict], Awaitable[None]]
Notify = Callable[[str, str], Awaitable[None]]


@dataclass
class _Message:
    query: str
    meta: dict = field(default_factory=dict)


class _ChatLane:
    """一个会话的车道：待处理队列 + 串行 worker。"""

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[_Message | None] = asyncio.Queue(maxsize=maxsize)
        self.worker: asyncio.Task | None = None


class InboundPipeline:
    """入站缓冲管道：per-chat 串行 + 合并窗口 + 全局并发上限 + 超时保护。

    Args:
        handler: 业务处理（调 LLM 并发送回复），``async (chat_key, query, meta) -> None``。
        notify: 管道提示发送（处理中/超时/丢弃），``async (chat_key, text) -> None``。
        merge_window: 合并窗口秒数（自批内首条起算，固定不顺延）。
        max_concurrency: 全局同时处理的车道数上限。
        lane_queue_size: 每车道待处理上限，满则丢最旧并 notify。
        handler_timeout: 单轮处理超时秒数，超时取消 handler 并 notify。
        processing_hint_after: 处理超过该秒数先发"处理中"提示（0 关闭）。
    """

    def __init__(
        self,
        handler: Handler,
        notify: Notify,
        *,
        merge_window: float = 2.0,
        max_concurrency: int = 4,
        lane_queue_size: int = 8,
        handler_timeout: float = 120.0,
        processing_hint_after: float = 15.0,
    ) -> None:
        self._handler = handler
        self._notify = notify
        self._merge_window = max(0.0, float(merge_window))
        self._sem = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._lane_queue_size = max(1, int(lane_queue_size))
        self._handler_timeout = float(handler_timeout)
        self._processing_hint_after = float(processing_hint_after)
        self._lanes: dict[str, _ChatLane] = {}
        self._stopped = False

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    async def submit(self, chat_key: str, query: str, meta: dict | None = None) -> None:
        """投递一条消息（调用方的 ws/回调线程即刻返回，不等待处理）。

        车道懒创建；队列满丢最旧并 notify（保持车道容量，保新弃旧）。
        stop() 后静默丢弃（调用方通常正在收尾）。
        """
        if self._stopped:
            logger.debug("管道已停止，丢弃 %s 的消息", chat_key)
            return
        lane = self._lanes.get(chat_key)
        if lane is None:
            lane = _ChatLane(self._lane_queue_size)
            self._lanes[chat_key] = lane
        message = _Message(query=query, meta=meta or {})
        try:
            lane.queue.put_nowait(message)
        except asyncio.QueueFull:
            # 丢最旧腾位（get_nowait 在单 worker 消费下不会与 Lane 竞争出空）
            try:
                dropped = lane.queue.get_nowait()
                lane.queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover — 满队列与消费的竞态窗口
                dropped = None
            try:
                lane.queue.put_nowait(message)
            except asyncio.QueueFull:  # pragma: no cover — 极端并发下仍满则丢弃本条
                logger.warning("车道 %s 持续满载，丢弃最新消息", chat_key)
                return
            logger.info("车道 %s 积压，丢弃最旧一条消息", chat_key)
            try:
                await self._notify(chat_key, "消息积压较多，最早一条被跳过了。")
            except Exception:  # noqa: BLE001
                pass
            del dropped
        if lane.worker is None or lane.worker.done():
            lane.worker = asyncio.create_task(self._run_lane(chat_key, lane))

    async def stop(self) -> None:
        """停止管道：停止接收 + 等待所有 worker 收尾（超时强杀）。"""
        self._stopped = True
        workers = [lane.worker for lane in self._lanes.values()
                   if lane.worker is not None and not lane.worker.done()]
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await asyncio.wait_for(worker, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._lanes.clear()

    # ------------------------------------------------------------------
    # 车道 worker：合并窗口 → 限流 → 超时保护
    # ------------------------------------------------------------------

    async def _run_lane(self, chat_key: str, lane: _ChatLane) -> None:
        """车道串行消费循环：攒批（合并窗口）→ 处理 → 下一批。"""
        while True:
            first = await lane.queue.get()
            lane.queue.task_done()
            batch: list[_Message] = [first]
            # 先收干已排队的（零等待部分），再开合并窗口收"快到但还没到"的
            while True:
                try:
                    batch.append(lane.queue.get_nowait())
                    lane.queue.task_done()
                except asyncio.QueueEmpty:
                    break
            if self._merge_window > 0:
                await self._collect_window(lane, batch)
            query = "\n".join(m.query for m in batch if m.query)
            meta = batch[-1].meta or {}
            if not query:
                continue
            await self._process(chat_key, query, meta)

    async def _collect_window(self, lane: _ChatLane, batch: list[_Message]) -> None:
        """合并窗口：窗口内到达的消息全部入批。固定时长，不顺延。"""

        async def _drain() -> None:
            while True:
                batch.append(await lane.queue.get())
                lane.queue.task_done()

        try:
            await asyncio.wait_for(_drain(), timeout=self._merge_window)
        except asyncio.TimeoutError:
            pass  # 窗口正常关闭；_drain 已被取消，批内容即为窗口内全部
        except asyncio.CancelledError:
            raise

    async def _process(self, chat_key: str, query: str, meta: dict) -> None:
        """单轮处理：全局限流 + 超时 + "处理中"提示 + 异常隔离。"""

        async def _invoke() -> None:
            async with self._sem:
                await self._handler(chat_key, query, meta)

        pending = asyncio.get_running_loop().create_task(_invoke())
        hint_task: asyncio.Task | None = None
        if self._processing_hint_after > 0:
            async def _hint() -> None:
                await asyncio.sleep(self._processing_hint_after)
                if not pending.done():
                    try:
                        await self._notify(chat_key, "还在思考中，请稍等…")
                    except Exception:  # noqa: BLE001 — 提示失败不影响处理
                        pass
            hint_task = asyncio.get_running_loop().create_task(_hint())
        try:
            await asyncio.wait_for(pending, timeout=self._handler_timeout)
        except asyncio.TimeoutError:
            logger.warning("处理超时（chat=%s, %.0fs）", chat_key, self._handler_timeout)
            try:
                await self._notify(chat_key, "这条消息处理超时了，请稍后重试或拆成几条发送。")
            except Exception:  # noqa: BLE001
                pass
        except asyncio.CancelledError:
            pending.cancel()
            raise
        except Exception:  # noqa: BLE001 — handler 异常不逃出车道（保序继续服务）
            logger.exception("处理消息异常（chat=%s）", chat_key)
            try:
                await self._notify(chat_key, "抱歉，处理这条消息时出错了。")
            except Exception:  # noqa: BLE001
                pass
        finally:
            if hint_task is not None:
                hint_task.cancel()
