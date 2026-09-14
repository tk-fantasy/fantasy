"""InboundPipeline 测试：合并/保序/限流/超时/积压丢弃/异常隔离/停止。"""

import asyncio

from app.integration.inbound_pipeline import InboundPipeline


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Harness:
    """handler/notify 记录器 + 可控阻塞。"""

    def __init__(self, **kwargs):
        self.processed: list[tuple[str, str]] = []      # (chat_key, query)
        self.notices: list[tuple[str, str]] = []        # (chat_key, text)
        self.handler_starts: list[str] = []
        self.blockers: dict[str, asyncio.Event] = {}
        self.handler_sleep: float = kwargs.pop("handler_sleep", 0.0)
        self.handler_error_for: set[str] = kwargs.pop("handler_error_for", set())
        defaults = dict(merge_window=0.15, max_concurrency=4, lane_queue_size=8,
                        handler_timeout=5.0, processing_hint_after=0.0)
        defaults.update(kwargs)
        self.pipeline = InboundPipeline(self._handler, self._notify, **defaults)

    async def _handler(self, chat_key: str, query: str, meta: dict) -> None:
        self.handler_starts.append(query)
        blocker = self.blockers.get(query)
        if blocker is not None:
            await blocker.wait()
        if self.handler_sleep:
            await asyncio.sleep(self.handler_sleep)
        if query in self.handler_error_for:
            raise RuntimeError("boom")
        self.processed.append((chat_key, query))

    async def _notify(self, chat_key: str, text: str) -> None:
        self.notices.append((chat_key, text))


def test_merge_window_joins_burst_into_one_query():
    h = _Harness()

    async def go():
        await h.pipeline.submit("c1", "把灯开一下")
        await h.pipeline.submit("c1", "哦不对，是客厅的")
        await h.pipeline.submit("c1", "色温调暖一点")
        await asyncio.sleep(0.5)
        await h.pipeline.stop()

    _run(go())

    assert h.processed == [("c1", "把灯开一下\n哦不对，是客厅的\n色温调暖一点")]


def test_messages_after_window_form_next_batch():
    """窗口结束后到达的消息进下一批（窗口固定不顺延）。"""
    h = _Harness()

    async def go():
        await h.pipeline.submit("c1", "第一条")
        await asyncio.sleep(0.4)  # 窗口 0.15s 已过，第一批已处理
        await h.pipeline.submit("c1", "第二条")
        await asyncio.sleep(0.4)
        await h.pipeline.stop()

    _run(go())

    assert h.processed == [("c1", "第一条"), ("c1", "第二条")]


def test_same_lane_processes_in_order_even_when_first_blocks():
    """同车道串行保序：上一批阻塞未完成时，下一批（窗口外的消息）必须等待。"""
    h = _Harness()
    release = asyncio.Event()
    h.blockers["第一条"] = release

    async def go():
        await h.pipeline.submit("c1", "第一条")
        # 等合并窗口（0.15s）关闭、handler 已取走第一批并阻塞
        await asyncio.sleep(0.3)
        await h.pipeline.submit("c1", "第二条")
        # 第一条还在阻塞：第二条绝不能已开始处理
        assert h.processed == []
        release.set()
        await asyncio.sleep(0.4)
        await h.pipeline.stop()

    _run(go())

    assert h.processed == [("c1", "第一条"), ("c1", "第二条")]
    assert h.handler_starts == ["第一条", "第二条"]


def test_global_concurrency_cap_respected():
    """max_concurrency=2：不同车道并行处理数从不超过 2。"""
    h = _Harness(max_concurrency=2, handler_sleep=0.15)
    current = {"n": 0, "peak": 0}

    async def slow_handler(chat_key, query, meta):
        current["n"] += 1
        current["peak"] = max(current["peak"], current["n"])
        await asyncio.sleep(0.15)
        current["n"] -= 1

    h.pipeline._handler = slow_handler

    async def go():
        for i in range(3):
            await h.pipeline.submit(f"c{i}", f"msg{i}")
        await asyncio.sleep(0.8)
        await h.pipeline.stop()

    _run(go())

    assert current["peak"] <= 2
    assert current["n"] == 0


def test_timeout_notifies_and_lane_survives():
    h = _Harness(handler_timeout=0.1)
    stuck = asyncio.Event()
    h.blockers["卡住的消息"] = stuck

    async def go():
        await h.pipeline.submit("c1", "卡住的消息")
        # 窗口 0.15s 后 handler 起跑，0.1s 超时 → 0.4s 时超时必已发生
        await asyncio.sleep(0.4)
        assert any("超时" in text for _, text in h.notices)
        stuck.set()  # 迟到的释放：handler 已被超时取消，不再有影响
        await h.pipeline.submit("c1", "下一条")
        await asyncio.sleep(0.4)
        await h.pipeline.stop()

    _run(go())

    assert ("c1", "下一条") in h.processed


def test_processing_hint_sent_when_slow():
    h = _Harness(processing_hint_after=0.05, handler_sleep=0.25)

    async def go():
        await h.pipeline.submit("c1", "慢消息")
        await asyncio.sleep(0.5)
        await h.pipeline.stop()

    _run(go())

    assert any("还在思考" in text for _, text in h.notices)


def test_queue_full_drops_oldest_and_notifies():
    """车道满：丢最旧保新弃旧，并 notify 用户。"""
    h = _Harness(lane_queue_size=2)
    release = asyncio.Event()
    h.blockers["m1"] = release

    async def go():
        await h.pipeline.submit("c1", "m1")
        await asyncio.sleep(0.05)  # worker 取走 m1 并阻塞，队列空出
        await h.pipeline.submit("c1", "m2")
        await h.pipeline.submit("c1", "m3")   # 队列 [m2, m3] 满
        await h.pipeline.submit("c1", "m4")   # 满 → 丢 m2
        assert any("积压" in text for _, text in h.notices)
        release.set()
        await asyncio.sleep(0.4)
        await h.pipeline.stop()

    _run(go())

    merged = h.processed[0][1]
    assert "m1" in merged and "m3" in merged and "m4" in merged
    assert "m2" not in merged


def test_handler_error_isolated_per_message():
    """单条异常：notify 出错文案，本车道后续消息照常处理。"""
    h = _Harness(handler_error_for={"会炸的消息"})

    async def go():
        await h.pipeline.submit("c1", "会炸的消息")
        await asyncio.sleep(0.35)
        await h.pipeline.submit("c1", "正常消息")
        await asyncio.sleep(0.35)
        await h.pipeline.stop()

    _run(go())

    assert any("出错了" in text for _, text in h.notices)
    assert ("c1", "正常消息") in h.processed


def test_different_lanes_are_independent():
    h = _Harness()
    release = asyncio.Event()
    h.blockers["a1"] = release

    async def go():
        await h.pipeline.submit("chat-a", "a1")
        await asyncio.sleep(0.05)
        # chat-a 阻塞中，chat-b 不受影响
        await h.pipeline.submit("chat-b", "b1")
        await asyncio.sleep(0.4)
        assert ("chat-b", "b1") in h.processed
        release.set()
        await asyncio.sleep(0.3)
        await h.pipeline.stop()

    _run(go())

    assert ("chat-a", "a1") in h.processed


def test_stop_rejects_new_submissions():
    h = _Harness()

    async def go():
        await h.pipeline.stop()
        await h.pipeline.submit("c1", "迟到消息")
        await asyncio.sleep(0.2)

    _run(go())

    assert h.processed == []
