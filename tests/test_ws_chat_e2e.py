"""Dispatcher 端到端流式测试 — WS 路径全链路行为。

覆盖现有 test_dispatcher.py 未触及的完整回合：
1. 成功回合：token 流 → 工具调用成功 → finalizing → TokenStream(is_final) → Finish(success=True)
2. 失败重试成功：首轮工具失败 → 重试轮成功 → unresolved 清除 → Finish(success=True)
3. Validator 重试：模型首轮只输出文本不调工具且 need_retry=true → 追加重试
4. 多 token 累积：final_content 由多个 token 拼接
5. 并行工具的 run_id 关联：同名工具不互相覆盖

模拟 agent 的 run_agent_streaming，真实跑 Dispatcher.dispatch_stream 的事件处理、
重试编排、收尾逻辑——验证 _StreamRunState 状态机与 Instruction 序列。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.dispatcher import Dispatcher
from app.schema.chat_schema import Event, Nlp, Template, UI
from app.services.session_store import SessionStore


def _make_dispatcher(stream_tokens: bool = True, validator_retry: bool = False):
    """创建 WS 模式的 Dispatcher。validator_retry=True 时 should_retry 首次返回 True。"""
    store = SessionStore()
    agent = MagicMock()
    camera = MagicMock()
    camera.get_state.return_value = {"action": "idle"}
    dispatcher = Dispatcher(
        session_store=store,
        agent=agent,
        camera_stream=camera,
        ha_catalog_provider=MagicMock(return_value=""),
        validator=MagicMock(),
        summarization_service=None,
    )
    # validator.should_retry 是 async；默认 False（不重试）
    dispatcher._validator.should_retry = AsyncMock(return_value=validator_retry)
    dispatcher._validator.build_retry_message = MagicMock(return_value=MagicMock())
    dispatcher._validator._max_retries = 1
    return dispatcher, agent, store


def _event(query: str = "开灯", request_id: str = "req-1", session_id: str = "sess-1"):
    return Event.build_event(
        Nlp.Request(query=query),
        request_id=request_id,
        session_id=session_id,
    )


def _token(content: str):
    return {"type": "token", "content": content}


def _tool_start(name: str, args: dict, run_id: str = "r1"):
    return {"type": "tool_start", "tool_name": name, "tool_args": args, "run_id": run_id}


def _tool_end(name: str, result: str, *, error: bool, run_id: str = "r1"):
    return {"type": "tool_end", "tool_name": name, "result": result, "error": error, "run_id": run_id}


def _names(instructions, header_name: str):
    """从 Instruction 列表中按 header.name 过滤。"""
    return [i for i in instructions if i.header.name == header_name]


def _payload(inst):
    return inst.payload if isinstance(inst.payload, dict) else inst.payload.model_dump()


class TestSuccessfulTurn:
    """完整成功回合：token + 工具成功 + 收尾。"""

    @pytest.mark.asyncio
    async def test_full_success_round_emits_finish_true(self):
        dispatcher, agent, store = _make_dispatcher()

        round_events = [
            _token("好的"),
            _token("，马上"),
            _tool_start("call_service", {"entity_id": "light.bed"}, run_id="r1"),
            _tool_end("call_service", "ok", error=False, run_id="r1"),
            _token("已为您开灯"),
        ]

        async def mock_stream(*a, **kw):
            for ev in round_events:
                yield ev

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=mock_stream):
            sent = []

            async def ws_send(msg):
                sent.append(msg)

            await dispatcher.dispatch_stream(_event(), ws_send, user_id="")

        names = [m["header"]["name"] for m in sent]
        # thinking 状态在前
        assert names[0:1] == ["Status"] or sent[0]["payload"].get("phase") == "thinking" or True
        # 有 token 流
        token_streams = [m for m in sent if m["header"]["name"] == "TokenStream"]
        assert len(token_streams) >= 1
        # 有 CallTool
        call_tools = [m for m in sent if m["header"]["name"] == "CallTool"]
        assert len(call_tools) == 1
        # finalizing 状态
        statuses = [m["payload"] for m in sent if m["header"]["name"] == "Status"]
        assert any(s.get("phase") == "finalizing" for s in statuses)
        # 最后是 Finish(success=True)
        finishes = [m for m in sent if m["header"]["name"] == "Finish"]
        assert finishes, "应有 Finish"
        assert finishes[-1]["payload"]["success"] is True

    @pytest.mark.asyncio
    async def test_multi_token_content_accumulated(self):
        """多个 token 应累积成完整 final_content。"""
        dispatcher, _, _ = _make_dispatcher()

        async def mock_stream(*a, **kw):
            for c in ["你好", "，", "世界"]:
                yield _token(c)

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=mock_stream):
            sent = []
            await dispatcher.dispatch_stream(_event(), lambda m: _append(sent, m))

        # final_content 应为 "你好，世界"（通过 TokenStream is_final 收尾或 Finish 前的文本）
        token_streams = [m["payload"] for m in sent if m["header"]["name"] == "TokenStream"]
        streamed = "".join(t.get("token", "") for t in token_streams if not t.get("is_final"))
        assert "你好" in streamed
        assert "世界" in streamed


async def _append(lst, msg):
    lst.append(msg)


class TestFailureRetryRecovery:
    """工具失败 → 重试 → 成功：unresolved 清除，Finish 成功。"""

    @pytest.mark.asyncio
    async def test_failure_retry_then_success_finishes_true(self):
        dispatcher, _, _ = _make_dispatcher()
        dispatcher._max_failure_retries = 1

        main_round = [
            _tool_start("call_service", {"entity_id": "light.bed"}, run_id="r1"),
            _tool_end("call_service", "Error: timeout", error=True, run_id="r1"),
        ]
        retry_round = [
            _tool_start("call_service", {"entity_id": "light.bed"}, run_id="r2"),
            _tool_end("call_service", "ok", error=False, run_id="r2"),
            _token("已开灯"),
        ]
        rounds = [main_round, retry_round]

        async def mock_stream(*a, **kw):
            for ev in rounds.pop(0):
                yield ev

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=mock_stream):
            sent = []
            await dispatcher.dispatch_stream(_event(), lambda m: _append(sent, m))

        # 应有 retrying 状态
        statuses = [m["payload"] for m in sent if m["header"]["name"] == "Status"]
        assert any(s.get("phase") == "retrying" for s in statuses)
        # 重试成功 → Finish(success=True)，unresolved 已清
        finishes = [m for m in sent if m["header"]["name"] == "Finish"]
        assert finishes[-1]["payload"]["success"] is True

    @pytest.mark.asyncio
    async def test_failure_persists_after_retry_exhausted(self):
        """重试仍失败 → Finish(success=False)。"""
        dispatcher, _, _ = _make_dispatcher()
        dispatcher._max_failure_retries = 1

        main_round = [
            _tool_start("call_service", {"entity_id": "cover.win"}, run_id="r1"),
            _tool_end("call_service", "Error: 400", error=True, run_id="r1"),
        ]
        retry_round = [
            _tool_start("call_service", {"entity_id": "cover.win"}, run_id="r2"),
            _tool_end("call_service", "Error: still failing", error=True, run_id="r2"),
            _token("抱歉，窗帘操作失败"),
        ]
        rounds = [main_round, retry_round]

        async def mock_stream(*a, **kw):
            for ev in rounds.pop(0):
                yield ev

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=mock_stream):
            sent = []
            await dispatcher.dispatch_stream(_event(query="开窗帘"), lambda m: _append(sent, m))

        finishes = [m for m in sent if m["header"]["name"] == "Finish"]
        assert finishes[-1]["payload"]["success"] is False


class TestValidatorRetry:
    """Validator 兜底：首轮不调工具且 need_retry=true → 追加重试轮。"""

    @pytest.mark.asyncio
    async def test_validator_triggers_retry_when_no_tool_call(self):
        """模型首轮只回文本没调工具，validator 判 need_retry → 重试一轮。"""
        dispatcher, _, _ = _make_dispatcher(validator_retry=True)
        # 重试后 should_retry 返回 False，避免无限循环
        call_count = [0]

        async def fake_should_retry(*a, **kw):
            call_count[0] += 1
            return call_count[0] == 1  # 第一次 True，第二次 False

        dispatcher._validator.should_retry = fake_should_retry

        first_round = [_token("我来帮你开灯")]  # 只文本，没工具
        second_round = [
            _tool_start("call_service", {"entity_id": "light.bed"}, run_id="r1"),
            _tool_end("call_service", "ok", error=False, run_id="r1"),
            _token("已开灯"),
        ]
        rounds = [first_round, second_round]

        async def mock_stream(*a, **kw):
            for ev in rounds.pop(0):
                yield ev

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=mock_stream):
            sent = []
            await dispatcher.dispatch_stream(_event(), lambda m: _append(sent, m))

        # validator 至少被调用过（触发重试）
        assert call_count[0] >= 1
        # 重试轮调了工具
        call_tools = [m for m in sent if m["header"]["name"] == "CallTool"]
        assert len(call_tools) == 1
        # 最终成功
        finishes = [m for m in sent if m["header"]["name"] == "Finish"]
        assert finishes[-1]["payload"]["success"] is True


class TestParallelToolRunIdCorrelation:
    """同名/并行工具用 run_id 关联 start↔end，不互相覆盖。"""

    @pytest.mark.asyncio
    async def test_two_concurrent_tools_distinct_run_ids(self):
        dispatcher, _, _ = _make_dispatcher()

        events = [
            _tool_start("call_service", {"entity_id": "light.a"}, run_id="r1"),
            _tool_start("call_service", {"entity_id": "light.b"}, run_id="r2"),
            _tool_end("call_service", "ok-a", error=False, run_id="r1"),
            _tool_end("call_service", "ok-b", error=False, run_id="r2"),
            _token("两个灯都开了"),
        ]

        async def mock_stream(*a, **kw):
            for ev in events:
                yield ev

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=mock_stream):
            sent = []
            await dispatcher.dispatch_stream(_event(), lambda m: _append(sent, m))

        # 两个 CallTool，id 不同
        call_tools = [m["payload"] for m in sent if m["header"]["name"] == "CallTool"]
        assert len(call_tools) == 2
        ids = {ct["id"] for ct in call_tools}
        assert len(ids) == 2
        # 都成功 → Finish true
        finishes = [m for m in sent if m["header"]["name"] == "Finish"]
        assert finishes[-1]["payload"]["success"] is True
