"""Targeted coverage tests for agents + automation_service (uncovered branches).

Every test asserts real behavior (emitted instructions, routing decisions,
state transitions) rather than merely executing lines. LLM/HTTP/tool boundaries
are mocked with AsyncMock/MagicMock; no real network, ports, or app/data writes.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.langgraph_agent import (
    _friendly_api_error,
    _load_model_config_from_config,
    build_chat_agent,
    load_model_config_for_user,
    make_post_model_hook,
    run_agent_streaming,
    session_to_langchain_messages,
)
from app.agents.dispatcher import Dispatcher
from app.agents.automation_agent import AutomationAgent
from app.agents.validator_agent import ValidatorAgent, _expected_state
from app.agents.model_family_adapters import (
    ModelFamilyAdapter,
    _default_plugin_dir,
    _load_adapters_module,
    get_adapter,
    refresh_plugin_adapters,
    reset_adapters,
)
from app.schema.chat_schema import Event, Nlp
from app.services.automation_service import AutomationService
from app.services.session_store import SessionState, SessionStore


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _event(query="test", rid="req-x", sid="sess-x"):
    return Event.build_event(Nlp.Request(query=query), request_id=rid, session_id=sid)


def _tok(t):
    return {"type": "token", "content": t}


def _tstart(name, args, run_id="r1"):
    return {"type": "tool_start", "tool_name": name, "tool_args": args, "run_id": run_id}


def _tend(name, result, *, error, run_id="r1"):
    return {"type": "tool_end", "tool_name": name, "result": result, "error": error, "run_id": run_id}


def _rounds_stream(rounds, capture=None):
    """mock run_agent_streaming：按轮弹出事件列表。capture 收集每轮 lc_messages。"""
    async def gen(agent, messages, session, timeout=120.0, succeeded_tool_calls=None, **kw):
        if capture is not None:
            capture.append(list(messages))
        if rounds:
            for ev in rounds.pop(0):
                yield ev
    return gen


def _payload(msg, field):
    p = msg.get("payload", {})
    return p.get(field) if isinstance(p, dict) else getattr(p, field)


def _msgs_of(ws_send):
    return [c[0][0] for c in ws_send.call_args_list]


class _Chunk:
    """on_chat_model_stream 事件的 chunk 桩。"""

    def __init__(self, content):
        self.content = content


class _ToolMsg:
    """langgraph 0.4 的 ToolMessage 桩（on_tool_end output 带 .content）。"""

    def __init__(self, content):
        self.content = content


class _FakeAgent:
    """astream_events 桩：按序产出事件或抛异常/延迟。"""

    def __init__(self, events=None, exc=None, delay=0.0):
        self._events = events or []
        self._exc = exc
        self._delay = delay
        self.captured_config = None

    async def astream_events(self, _input, config=None, version=None):
        self.captured_config = config
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        for ev in self._events:
            yield ev


# ---------------------------------------------------------------------------
# app/agents/langgraph_agent.py
# ---------------------------------------------------------------------------

class TestSessionSummariesInjection:
    def test_summaries_injected_as_system_message(self):
        session = SessionState(session_id="s", request_id="r")
        session.summaries = [{"text": "摘要一"}, {"text": "  "}, {"text": "摘要二"}]
        with patch("app.core.config.get_config", return_value=True):
            messages = session_to_langchain_messages(session, system_prompt="SYS")
        # SystemMessage(SYS) → SystemMessage(摘要) ；空白摘要被过滤
        assert len(messages) == 2
        assert isinstance(messages[1], SystemMessage)
        assert "摘要一" in messages[1].content and "摘要二" in messages[1].content
        assert "以下是本会话更早对话的摘要" in messages[1].content

    def test_summaries_skipped_when_trim_disabled(self):
        session = SessionState(session_id="s", request_id="r")
        session.summaries = [{"text": "摘要一"}]
        with patch("app.core.config.get_config", return_value=False):
            messages = session_to_langchain_messages(session, system_prompt="SYS")
        assert len(messages) == 1  # 只剩 system prompt

    def test_blank_summaries_no_injection(self):
        session = SessionState(session_id="s", request_id="r")
        session.summaries = [{"text": ""}, {"other": 1}]
        with patch("app.core.config.get_config", return_value=True):
            messages = session_to_langchain_messages(session, system_prompt="SYS")
        assert len(messages) == 1


class TestPostModelHookEdgeBranches:
    @pytest.mark.asyncio
    async def test_last_message_not_ai_returns_empty(self):
        hook = make_post_model_hook()
        result = await hook(
            {"messages": [HumanMessage("hi")]},
            {"configurable": {"succeeded_tool_calls": {"t::{}"}}},
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_call_matches_succeeded_returns_empty(self):
        hook = make_post_model_hook()
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "a", "args": {"x": 1}, "id": "c1", "type": "tool_call"}],
            id="ai1",
        )
        result = await hook(
            {"messages": [ai]},
            {"configurable": {"succeeded_tool_calls": {"b::{}"}}},
        )
        assert result == {}  # removed_names 为空 → 不过滤


class TestBuildChatAgent:
    def test_builds_with_explicit_model_config(self):
        llm_mock = MagicMock()
        agent_sentinel = MagicMock()
        sync_c, async_c = MagicMock(), MagicMock()
        with patch("app.agents.langgraph_agent.ChatOpenAI", return_value=llm_mock) as mock_llm, \
             patch("app.agents.langgraph_agent.create_react_agent", return_value=agent_sentinel) as mock_cra, \
             patch("app.clients.http_client.new_sync_client", return_value=sync_c), \
             patch("app.clients.http_client.new_client", return_value=async_c):
            tools = [MagicMock()]
            agent, clients = build_chat_agent(tools, model_config={
                "base_url": "http://x/v1", "model": "m1", "api_key": "k"})

        assert agent is agent_sentinel
        assert clients == (sync_c, async_c)
        kwargs = mock_llm.call_args.kwargs
        assert kwargs["model"] == "m1"
        assert kwargs["temperature"] == 0.3
        assert kwargs["streaming"] is True
        llm_mock.bind_tools.assert_called_once_with(tools)
        mock_cra.assert_called_once()
        hook = mock_cra.call_args.kwargs["post_model_hook"]

    @pytest.mark.asyncio
    async def test_built_hook_passes_normal_round_through(self):
        """build_chat_agent 装配的 post_model_hook 在正常轮（无 succeeded）不过滤。"""
        agent_sentinel = MagicMock()
        captured = {}
        with patch("app.agents.langgraph_agent.ChatOpenAI", return_value=MagicMock()), \
             patch("app.agents.langgraph_agent.create_react_agent",
                   return_value=agent_sentinel) as mock_cra, \
             patch("app.clients.http_client.new_sync_client", return_value=MagicMock()), \
             patch("app.clients.http_client.new_client", return_value=MagicMock()):
            agent, _ = build_chat_agent([], model_config={"base_url": "u", "model": "m", "api_key": "k"})
        assert agent is agent_sentinel
        hook = mock_cra.call_args.kwargs["post_model_hook"]
        ai = AIMessage(content="", tool_calls=[
            {"name": "t", "args": {"a": 1}, "id": "c1", "type": "tool_call"}], id="ai1")
        result = await hook({"messages": [ai]}, {"configurable": {}})
        assert result == {}

    def test_no_tools_skips_bind(self):
        llm_mock = MagicMock()
        with patch("app.agents.langgraph_agent.ChatOpenAI", return_value=llm_mock), \
             patch("app.agents.langgraph_agent.create_react_agent", return_value=MagicMock()), \
             patch("app.clients.http_client.new_sync_client", return_value=MagicMock()), \
             patch("app.clients.http_client.new_client", return_value=MagicMock()):
            build_chat_agent([], model_config={"base_url": "u", "model": "m", "api_key": "k"})
        llm_mock.bind_tools.assert_not_called()

    def test_default_config_loader_used_when_no_model_config(self):
        with patch("app.agents.langgraph_agent._load_model_config_from_config",
                   return_value={"base_url": "u", "model": "mm", "api_key": "k"}), \
             patch("app.agents.langgraph_agent.ChatOpenAI", return_value=MagicMock()) as mock_llm, \
             patch("app.agents.langgraph_agent.create_react_agent", return_value=MagicMock()), \
             patch("app.clients.http_client.new_sync_client", return_value=MagicMock()), \
             patch("app.clients.http_client.new_client", return_value=MagicMock()):
            build_chat_agent([])
        assert mock_llm.call_args.kwargs["model"] == "mm"


class TestLoadModelConfigForUser:
    @pytest.mark.asyncio
    async def test_empty_user_returns_none_without_resolve(self):
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock()) as mock_resolve:
            assert await load_model_config_for_user("") is None
        mock_resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_key_entry_returns_none(self):
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)):
            assert await load_model_config_for_user("u1") is None

    @pytest.mark.asyncio
    async def test_entry_without_api_key_returns_none(self):
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value={"base_url": "http://x/v1/", "model": "m", "api_key": ""})):
            assert await load_model_config_for_user("u1") is None

    @pytest.mark.asyncio
    async def test_full_entry_normalized(self):
        entry = {"base_url": "http://x/v1/", "model": "m1", "api_key": "sk"}
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=entry)):
            cfg = await load_model_config_for_user("u1")
        assert cfg == {"base_url": "http://x/v1", "model": "m1", "api_key": "sk"}


class TestRunAgentStreaming:
    @pytest.mark.asyncio
    async def test_stream_events_emitted_and_final(self):
        agent = _FakeAgent(events=[
            {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("你")}},
            {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("好")}},
            {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("")}},      # 空内容跳过
            {"event": "on_chat_model_stream", "data": {"chunk": None}},            # None 跳过
            {"event": "on_chat_model_stream", "data": {"chunk": _Chunk(["x"])}},   # 非 str 跳过
            {"event": "on_tool_start", "name": "call_service",
             "data": {"input": {"entity_id": "light.a"}}, "run_id": "r1"},
            {"event": "on_tool_end", "name": "call_service",
             "data": {"output": _ToolMsg("Error: 400")}, "run_id": "r1"},
            {"event": "on_tool_end", "name": "get_entities",
             "data": {"output": {"k": "灯"}}, "run_id": "r2"},
            {"event": "on_tool_end", "name": "raw", "data": {"output": "plain"}, "run_id": "r3"},
            {"event": "other_kind", "data": {}},  # 未知事件忽略
        ])
        metrics = MagicMock()
        container = MagicMock()
        container.metrics_service = metrics

        session = SessionState(session_id="s", request_id="r")
        with patch("app.container.get_container", return_value=container):
            events = [ev async for ev in run_agent_streaming(agent, [], session)]

        tokens = [ev for ev in events if ev["type"] == "token"]
        assert [t["content"] for t in tokens] == ["你", "好"]
        starts = [ev for ev in events if ev["type"] == "tool_start"]
        assert len(starts) == 1
        assert starts[0]["tool_name"] == "call_service"
        assert starts[0]["tool_args"] == {"entity_id": "light.a"}
        assert starts[0]["run_id"] == "r1"
        ends = [ev for ev in events if ev["type"] == "tool_end"]
        assert len(ends) == 3
        assert ends[0]["error"] is True and ends[0]["result"] == "Error: 400"
        # dict 输出被 json dumps（ToolMessage.content 优先、dict 序列化）
        assert json.loads(ends[1]["result"]) == {"k": "灯"}
        assert ends[1]["error"] is False
        assert ends[2]["result"] == "plain"
        final = [ev for ev in events if ev["type"] == "final"]
        assert final and final[0]["content"] == "你好"
        # 指标：LLM 记一次、每个 tool_end 记一次（错误标记正确）
        metrics.record_llm_call.assert_called_once()
        assert metrics.record_tool_call.call_args_list[0].kwargs == {"error": True}
        assert metrics.record_tool_call.call_args_list[1].kwargs == {"error": False}

    @pytest.mark.asyncio
    async def test_config_carries_recursion_limit_and_succeeded_calls(self):
        agent = _FakeAgent(events=[])
        session = SessionState(session_id="s", request_id="r")
        with patch("app.container.get_container", side_effect=RuntimeError):
            events = [ev async for ev in run_agent_streaming(
                agent, [], session, succeeded_tool_calls={"t::{}"})]
        assert events == [{"type": "final", "content": ""}]  # metrics 不可用也正常收尾
        assert agent.captured_config["recursion_limit"] >= 1
        assert agent.captured_config["configurable"]["succeeded_tool_calls"] == {"t::{}"}
        assert agent.captured_config["configurable"]["session"] is session

    @pytest.mark.asyncio
    async def test_timeout_yields_error(self):
        agent = _FakeAgent(delay=0.5)
        session = SessionState(session_id="s", request_id="r")
        with patch("app.container.get_container", side_effect=RuntimeError):
            events = [ev async for ev in run_agent_streaming(agent, [], session, timeout=0.05)]
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "超时" in events[0]["message"]

    @pytest.mark.asyncio
    async def test_recursion_error_yields_reason_marker(self):
        from langgraph.errors import GraphRecursionError
        agent = _FakeAgent(exc=GraphRecursionError("limit"))
        session = SessionState(session_id="s", request_id="r")
        with patch("app.container.get_container", side_effect=RuntimeError):
            events = [ev async for ev in run_agent_streaming(agent, [], session)]
        assert events[0]["type"] == "error"
        assert events[0]["reason"] == "recursion_limit"

    @pytest.mark.asyncio
    async def test_generic_exception_yields_friendly_error(self):
        agent = _FakeAgent(exc=ValueError("boom"))
        session = SessionState(session_id="s", request_id="r")
        with patch("app.container.get_container", side_effect=RuntimeError):
            events = [ev async for ev in run_agent_streaming(agent, [], session)]
        assert events[0]["type"] == "error"
        assert "boom" in events[0]["message"]


class TestFriendlyApiError:
    class _ApiErr(Exception):
        def __init__(self, code=None, msg=None):
            super().__init__(msg or f"err {code}")
            if code is not None:
                self.status_code = code

    def test_502(self):
        assert "502" in _friendly_api_error(self._ApiErr(502))

    def test_429(self):
        assert "429" in _friendly_api_error(self._ApiErr(429))

    def test_401(self):
        assert "401" in _friendly_api_error(self._ApiErr(401))

    def test_403(self):
        assert "403" in _friendly_api_error(self._ApiErr(403))

    def test_other_5xx(self):
        msg = _friendly_api_error(self._ApiErr(503))
        assert "HTTP 503" in msg

    def test_4xx_generic(self):
        assert "Agent 执行出错" in _friendly_api_error(self._ApiErr(400))

    def test_no_status_code(self):
        assert "Agent 执行出错" in _friendly_api_error(RuntimeError("x"))

    def test_long_message_truncated(self):
        msg = _friendly_api_error(RuntimeError("x" * 500))
        assert msg.endswith("...")
        assert len(msg) < 300


# ---------------------------------------------------------------------------
# app/agents/dispatcher.py
# ---------------------------------------------------------------------------

def _mk_dispatcher(**overrides):
    store = SessionStore()
    agent = MagicMock()
    camera = MagicMock()
    camera.get_state.return_value = {"action": "idle"}
    camera.list_cameras.return_value = []
    deps = dict(
        session_store=store, agent=agent, camera_manager=camera,
        ha_catalog_provider=MagicMock(return_value=""),
    )
    deps.update(overrides)
    dispatcher = Dispatcher(**deps)
    return dispatcher, agent, store


class TestDispatcherEventRouting:
    """handler 事件分支：friendly_name、WS executing、成功签名、error 事件。"""

    @pytest.mark.asyncio
    async def test_ws_call_tool_translates_friendly_name_and_pushes_executing(self):
        ha = MagicMock()
        ha.get_entity_name_map = AsyncMock(return_value={"light.bed": "床头灯"})
        dispatcher, _, _ = _mk_dispatcher(ha_service=ha)
        rounds = [[
            _tstart("ha___call_service", {"entity_id": "light.bed"}, run_id="r1"),
            _tend("ha___call_service", "ok", error=False, run_id="r1"),
            _tok("已打开床头灯"),
        ]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)

        sent = _msgs_of(ws_send)
        call_tool = [m for m in sent if m["header"]["name"] == "CallTool"]
        assert call_tool, "应推送 CallTool"
        assert _payload(call_tool[0], "friendly_name") == "床头灯"
        assert _payload(call_tool[0], "tool_params") == {"entity_id": "light.bed"}
        # WS 先推 executing 状态再推 CallTool
        assert sent.index(call_tool[0]) > 0
        executing = [m for m in sent if m["header"]["name"] == "Status"
                     and _payload(m, "phase") == "executing"]
        assert executing and _payload(executing[0], "detail") == "ha___call_service"
        # 成功结果
        result = [m for m in sent if m["header"]["name"] == "CallToolResult"]
        assert _payload(result[0], "success") is True
        assert _payload(result[0], "id") == _payload(call_tool[0], "id")  # run_id 配对
        finish = [m for m in sent if m["header"]["name"] == "Finish"]
        assert _payload(finish[-1], "success") is True

    @pytest.mark.asyncio
    async def test_retry_success_removes_unresolved_and_finish_true(self):
        """主轮工具失败 → 重试轮同名工具成功 → unresolved 清空、Finish success=True。"""
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [
            [_tstart("call_service", {"entity_id": "light.a"}, run_id="r1"),
             _tend("call_service", "Error: timeout", error=True, run_id="r1")],
            [_tstart("call_service", {"entity_id": "light.a"}, run_id="r2"),
             _tend("call_service", "ok", error=False, run_id="r2"),
             _tok("已重试成功")],
        ]
        captured: list[list] = []
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds, capture=captured)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            instructions = await dispatcher.dispatch(_event("打开灯", rid="r", sid="s"))

        finish = [i for i in instructions if i.header.name == "Finish"]
        assert _payload(finish[-1].model_dump(), "success") is True
        # 重试轮消息带失败说明
        retry_round_msgs = captured[1]
        assert any("刚才部分工具调用失败" in getattr(m, "content", "") for m in retry_round_msgs)

    @pytest.mark.asyncio
    async def test_error_event_rest_sets_toast_content(self):
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [[{"type": "error", "message": "API 挂了"}]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            instructions = await dispatcher.dispatch(_event(rid="r", sid="s"))
        toast = [i for i in instructions if i.header.name == "ToastStream"]
        assert toast and "抱歉，处理出错" in _payload(toast[0].model_dump(), "stream")
        assert "API 挂了" in _payload(toast[0].model_dump(), "stream")

    @pytest.mark.asyncio
    async def test_error_event_ws_emits_exception_and_failed_finish(self):
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [[_tok("部分"), {"type": "error", "message": "模型 429"}]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)
        sent = _msgs_of(ws_send)
        exc = [m for m in sent if m["header"]["name"] == "Exception"]
        assert exc and "模型 429" in _payload(exc[0], "message")
        finish = [m for m in sent if m["header"]["name"] == "Finish"]
        assert _payload(finish[-1], "success") is False
        # 已流式输出的部分内容仍会以 ToastStream 收尾（Exception 另行展示错误）
        toast = [m for m in sent if m["header"]["name"] == "ToastStream"]
        assert toast and _payload(toast[0], "stream") == "部分"

    @pytest.mark.asyncio
    async def test_missing_run_id_gets_synth_ids(self):
        """run_id 为空时用计数兜底生成，start/end 仍配对。"""
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [[
            {"type": "tool_start", "tool_name": "t", "tool_args": {}, "run_id": ""},
            {"type": "tool_end", "tool_name": "t", "result": "ok", "error": False, "run_id": ""},
        ]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            instructions = await dispatcher.dispatch(_event(rid="r", sid="s"))
        call = [i for i in instructions if i.header.name == "CallTool"][0]
        result = [i for i in instructions if i.header.name == "CallToolResult"][0]
        assert _payload(call.model_dump(), "id") == _payload(result.model_dump(), "id")
        assert _payload(call.model_dump(), "service_name") == "local"


class TestDispatcherRecursionWrapup:
    @pytest.mark.asyncio
    async def test_recursion_limit_triggers_wrapup_round(self):
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [
            [_tok("已打开"), {"type": "error", "reason": "recursion_limit", "message": "上限"}],
            [_tok("，已完成的操作总结如下")],
        ]
        captured: list[list] = []
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds, capture=captured)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)

        sent = _msgs_of(ws_send)
        finalizing = [m for m in sent if m["header"]["name"] == "Status"
                      and _payload(m, "phase") == "finalizing"]
        assert any("正在整理已完成操作" in str(_payload(m, "detail")) for m in finalizing)
        # 收尾提示消息追加到 lc_messages
        wrapup = [m for m in captured[1]
                  if isinstance(m, HumanMessage) and "不能再调用工具" in m.content]
        assert wrapup, "收尾轮应追加 HumanMessage 收尾提示"
        toast = [m for m in sent if m["header"]["name"] == "ToastStream"]
        assert toast and "总结" in _payload(toast[0], "stream")
        finish = [m for m in sent if m["header"]["name"] == "Finish"]
        assert _payload(finish[-1], "success") is True

    def test_build_wrapup_message_content(self):
        msg = Dispatcher._build_wrapup_message()
        assert isinstance(msg, HumanMessage)
        assert "不要再调用任何工具" in msg.content


class TestDispatcherValidatorRetryLoop:
    @pytest.mark.asyncio
    async def test_validator_retry_reruns_and_finishes(self):
        dispatcher, _, _ = _mk_dispatcher()
        validator = MagicMock()
        validator.max_retries = 2
        validator.should_retry = AsyncMock(side_effect=[True, False])
        validator.build_retry_message = MagicMock(return_value=HumanMessage(content="请调用工具"))
        dispatcher._validator = validator

        rounds = [
            [_tok("我将帮你打开灯")],
            [_tok("已经打开客厅灯了")],
        ]
        captured: list[list] = []
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds, capture=captured)):
            instructions = await dispatcher.dispatch(_event("把客厅灯打开", rid="r", sid="s"))

        # 首轮判定 True → 重试；重试后判定 False → 退出（共两次判定）
        assert validator.should_retry.await_count == 2
        statuses = [i for i in instructions if i.header.name == "Status"]
        assert any(_payload(s.model_dump(), "phase") == "retrying" for s in statuses)
        # validator 的重试消息进入后续轮
        assert any(getattr(m, "content", "") == "请调用工具" for m in captured[1])
        toast = [i for i in instructions if i.header.name == "ToastStream"]
        assert toast and "已经打开客厅灯了" in _payload(toast[0].model_dump(), "stream")
        finish = [i for i in instructions if i.header.name == "Finish"]
        assert _payload(finish[-1].model_dump(), "success") is True


class TestDispatcherSilentFailureWS:
    @pytest.mark.asyncio
    async def test_ws_silent_failure_emits_toast_and_final_reset(self):
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [
            [_tstart("call_service", {}, run_id="r1"),
             _tend("call_service", "Error: 设备离线", error=True, run_id="r1")],
            [],  # 重试轮空转
        ]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)
        sent = _msgs_of(ws_send)
        toast = [m for m in sent if m["header"]["name"] == "ToastStream"]
        assert toast and "部分操作未能完成" in _payload(toast[0], "stream")
        assert "call_service" in _payload(toast[0], "stream")
        finals = [m for m in sent if m["header"]["name"] == "TokenStream"
                  and _payload(m, "is_final") is True]
        assert finals, "静默兜底需发 is_final 复位前端流式索引"
        finish = [m for m in sent if m["header"]["name"] == "Finish"]
        assert _payload(finish[-1], "success") is False


class TestDispatcherErrors:
    @pytest.mark.asyncio
    async def test_main_round_exception_emits_exception_and_failed_finish(self):
        dispatcher, _, _ = _mk_dispatcher()

        async def boom(*a, **kw):
            raise RuntimeError("boom")
            yield  # pragma: no cover - 使其成为 async generator

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=boom), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            instructions = await dispatcher.dispatch(_event(rid="r", sid="s"))
        exc = [i for i in instructions if i.header.name == "Exception"]
        assert exc and "boom" in _payload(exc[0].model_dump(), "message")
        finish = [i for i in instructions if i.header.name == "Finish"]
        assert _payload(finish[-1].model_dump(), "success") is False

    @pytest.mark.asyncio
    async def test_ws_emit_failure_during_error_is_swallowed(self):
        dispatcher, _, _ = _mk_dispatcher()

        async def boom(*a, **kw):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=boom), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock(side_effect=RuntimeError("ws gone"))
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)  # 不抛

    @pytest.mark.asyncio
    async def test_retry_round_exception_marks_error(self):
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [
            [_tstart("t", {}, run_id="r1"), _tend("t", "Error: x", error=True, run_id="r1")],
        ]

        async def fail_on_retry(*a, **kw):
            if rounds:
                for ev in rounds.pop(0):
                    yield ev
            else:
                raise RuntimeError("retry blew up")

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=fail_on_retry), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            instructions = await dispatcher.dispatch(_event(rid="r", sid="s"))
        exc = [i for i in instructions if i.header.name == "Exception"]
        assert exc and "retry blew up" in _payload(exc[0].model_dump(), "message")
        finish = [i for i in instructions if i.header.name == "Finish"]
        assert _payload(finish[-1].model_dump(), "success") is False

    @pytest.mark.asyncio
    async def test_dispatch_prepare_context_failure_rest(self):
        dispatcher, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.session_to_langchain_messages",
                   side_effect=RuntimeError("ctx fail")):
            instructions = await dispatcher.dispatch(_event(rid="r", sid="s"))
        assert [i.header.name for i in instructions] == ["Exception", "Finish"]
        assert "ctx fail" in _payload(instructions[0].model_dump(), "message")
        assert _payload(instructions[1].model_dump(), "success") is False

    @pytest.mark.asyncio
    async def test_dispatch_stream_prepare_context_failure_sends_over_ws(self):
        dispatcher, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.session_to_langchain_messages",
                   side_effect=RuntimeError("ctx fail")):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)
        names = [m["header"]["name"] for m in _msgs_of(ws_send)]
        assert names == ["Exception", "Finish"]

    @pytest.mark.asyncio
    async def test_dispatch_stream_prepare_failure_with_broken_ws(self):
        dispatcher, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.session_to_langchain_messages",
                   side_effect=RuntimeError("ctx fail")):
            ws_send = AsyncMock(side_effect=RuntimeError("ws gone"))
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)  # 不抛

    @pytest.mark.asyncio
    async def test_ws_broken_mutes_emits_but_persists(self):
        """首条 emit 失败 → 后续静默；本轮仍落库一次。"""
        store = SessionStore()
        dispatcher, _, _ = _mk_dispatcher()
        store_session = AsyncMock()
        dispatcher._session_store = store
        store.get_or_create = AsyncMock(return_value=SessionState(session_id="s", request_id="r"))
        store.store_session = store_session
        rounds = [[_tok("你好")]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock(side_effect=RuntimeError("ws gone"))
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)
        assert ws_send.await_count == 1  # 首条失败后全部静音
        store_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_store_session_failure_in_finally_is_swallowed(self):
        dispatcher, _, _ = _mk_dispatcher()
        store = AsyncMock()
        store.get_or_create = AsyncMock(return_value=SessionState(session_id="s", request_id="r"))
        store.store_session = AsyncMock(side_effect=RuntimeError("db closed"))
        dispatcher._session_store = store
        rounds = [[_tok("你好")]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), AsyncMock())  # 不抛


class TestRoundLevelFailureHandlers:
    """失败重试轮 / 收尾轮 / validator 重试轮的 CancelledError 与异常兜底。"""

    @pytest.mark.asyncio
    async def test_emit_turn_error_swallows_emit_failure(self):
        dispatcher, _, _ = _mk_dispatcher()
        state = MagicMock()
        state.has_error = False

        async def emit(instr):
            raise RuntimeError("emit blew up")

        await dispatcher._emit_turn_error(RuntimeError("boom"), state, emit, "r", "s", "WS")
        assert state.has_error is True

    @pytest.mark.asyncio
    async def test_controls_provider_failure_degrades_to_none(self):
        """controls provider 本身抛错 → device_controls=None，主流程继续。"""
        dispatcher, _, _ = _mk_dispatcher(
            ha_controls_provider=MagicMock(side_effect=RuntimeError("controls down")),
        )
        session = SessionState(session_id="s", request_id="r")
        with patch("app.agents.dispatcher.build_system_prompt", new=AsyncMock(return_value="SYS")):
            ctx = await dispatcher._prepare_context(session, "你好")
        assert ctx["device_controls"] is None

    @pytest.mark.asyncio
    async def test_failure_retry_round_cancelled_emits_failed_finish(self):
        dispatcher, _, _ = _mk_dispatcher()
        sink = MagicMock()
        sink.interrupt_all = AsyncMock()
        dispatcher._sink_manager = sink
        rounds = [
            [_tstart("t", {}, run_id="r1"), _tend("t", "Error: x", error=True, run_id="r1")],
        ]

        async def cancelled_on_retry(*a, **kw):
            if rounds:
                for ev in rounds.pop(0):
                    yield ev
            else:
                raise asyncio.CancelledError()

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=cancelled_on_retry), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)
        finish = [m for m in _msgs_of(ws_send) if m["header"]["name"] == "Finish"]
        assert finish and _payload(finish[-1], "success") is False
        sink.interrupt_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wrapup_round_cancelled_emits_failed_finish(self):
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [
            [_tok("已打开"), {"type": "error", "reason": "recursion_limit", "message": "上限"}],
        ]

        async def cancelled_on_wrapup(*a, **kw):
            if rounds:
                for ev in rounds.pop(0):
                    yield ev
            else:
                raise asyncio.CancelledError()

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=cancelled_on_wrapup), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)
        finish = [m for m in _msgs_of(ws_send) if m["header"]["name"] == "Finish"]
        assert finish and _payload(finish[-1], "success") is False

    @pytest.mark.asyncio
    async def test_wrapup_round_exception_emits_error(self):
        dispatcher, _, _ = _mk_dispatcher()
        rounds = [
            [_tok("已打开"), {"type": "error", "reason": "recursion_limit", "message": "上限"}],
        ]

        async def raises_on_wrapup(*a, **kw):
            if rounds:
                for ev in rounds.pop(0):
                    yield ev
            else:
                raise RuntimeError("wrapup blew up")

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=raises_on_wrapup), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)
        sent = _msgs_of(ws_send)
        exc = [m for m in sent if m["header"]["name"] == "Exception"]
        assert exc and "wrapup blew up" in _payload(exc[0], "message")
        finish = [m for m in sent if m["header"]["name"] == "Finish"]
        assert _payload(finish[-1], "success") is False

    @pytest.mark.asyncio
    async def test_validator_retry_round_cancelled_emits_failed_finish(self):
        dispatcher, _, _ = _mk_dispatcher()
        validator = MagicMock()
        validator.max_retries = 2
        validator.should_retry = AsyncMock(side_effect=[True])
        validator.build_retry_message = MagicMock(return_value=HumanMessage(content="请调用工具"))
        dispatcher._validator = validator
        rounds = [[_tok("我将帮你打开灯")]]

        async def cancelled_on_validator_retry(*a, **kw):
            if rounds:
                for ev in rounds.pop(0):
                    yield ev
            else:
                raise asyncio.CancelledError()

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=cancelled_on_validator_retry):
            ws_send = AsyncMock()
            await dispatcher.dispatch_stream(_event(rid="r", sid="s"), ws_send)
        finish = [m for m in _msgs_of(ws_send) if m["header"]["name"] == "Finish"]
        assert finish and _payload(finish[-1], "success") is False

    @pytest.mark.asyncio
    async def test_validator_retry_round_exception_emits_error(self):
        dispatcher, _, _ = _mk_dispatcher()
        validator = MagicMock()
        validator.max_retries = 2
        validator.should_retry = AsyncMock(side_effect=[True])
        validator.build_retry_message = MagicMock(return_value=HumanMessage(content="请调用工具"))
        dispatcher._validator = validator
        rounds = [[_tok("我将帮你打开灯")]]

        async def raises_on_validator_retry(*a, **kw):
            if rounds:
                for ev in rounds.pop(0):
                    yield ev
            else:
                raise RuntimeError("validator retry blew up")

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=raises_on_validator_retry):
            instructions = await dispatcher.dispatch(_event(rid="r", sid="s"))
        exc = [i for i in instructions if i.header.name == "Exception"]
        assert exc and "validator retry blew up" in _payload(exc[0].model_dump(), "message")
        finish = [i for i in instructions if i.header.name == "Finish"]
        assert _payload(finish[-1].model_dump(), "success") is False


class TestDispatcherAgentClientLifecycle:
    @pytest.mark.asyncio
    async def test_sync_client_close_failure_swallowed(self):
        dispatcher, agent, _ = _mk_dispatcher()
        sync_c = MagicMock()
        sync_c.close.side_effect = RuntimeError("close fail")
        async_c = MagicMock()
        async_c.aclose = AsyncMock()
        dispatcher._agent_clients[id(agent)] = (sync_c, async_c)
        await dispatcher._close_agent_clients(agent)  # 不抛
        assert id(agent) not in dispatcher._agent_clients

    @pytest.mark.asyncio
    async def test_get_agent_double_check_returns_concurrently_built(self):
        dispatcher, global_agent, _ = _mk_dispatcher()
        prebuilt = MagicMock()
        cfg = {"base_url": "http://x", "model": "m", "api_key": "k"}

        async def racing_load(user_id):
            dispatcher._user_agents[(user_id, "full")] = prebuilt  # 模拟持锁前已被他协程构建（key 含回合变体）
            return cfg

        with patch("app.agents.dispatcher.load_model_config_for_user", side_effect=racing_load), \
             patch("app.agents.dispatcher.build_chat_agent") as mock_build:
            result = await dispatcher._get_agent("u1")
        assert result is prebuilt
        mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_agent_build_failure_falls_back_to_global(self):
        dispatcher, global_agent, _ = _mk_dispatcher()
        cfg = {"base_url": "http://x", "model": "m", "api_key": "k"}
        with patch("app.agents.dispatcher.load_model_config_for_user",
                   new=AsyncMock(return_value=cfg)), \
             patch("app.agents.dispatcher.build_chat_agent",
                   side_effect=RuntimeError("no key")):
            result = await dispatcher._get_agent("u2")
        assert result is global_agent
        assert ("u2", "full") not in dispatcher._user_agents


class TestDispatcherCameraAndHA:
    def test_init_wires_ha_service_into_validator(self):
        validator = ValidatorAgent()
        ha = MagicMock()
        _mk_dispatcher(validator=validator, ha_service=ha)
        assert validator._ha_service is ha

    def test_primary_camera_prefers_getter(self):
        dispatcher, _, _ = _mk_dispatcher()
        cm = MagicMock()
        cm.primary_camera_id = MagicMock(return_value="cam2")
        dispatcher._camera_manager = cm
        assert dispatcher._primary_camera_id() == "cam2"

    def test_primary_camera_getter_empty_falls_back_to_empty_string(self):
        dispatcher, _, _ = _mk_dispatcher()
        cm = MagicMock()
        cm.primary_camera_id = MagicMock(return_value=None)
        cm.list_cameras.return_value = [{"id": "cam1"}]
        dispatcher._camera_manager = cm
        assert dispatcher._primary_camera_id() == ""

    def test_primary_camera_duck_type_without_getter(self):
        dispatcher, _, _ = _mk_dispatcher()

        class _OldMgr:
            def list_cameras(self):
                return [{"id": "legacy"}]

        dispatcher._camera_manager = _OldMgr()
        assert dispatcher._primary_camera_id() == "legacy"

    def test_primary_camera_none_manager_returns_empty(self):
        dispatcher, _, _ = _mk_dispatcher()
        dispatcher._camera_manager = None
        assert dispatcher._primary_camera_id() == ""

    def test_get_camera_state_none_manager_returns_empty(self):
        dispatcher, _, _ = _mk_dispatcher()
        dispatcher._camera_manager = None
        assert dispatcher._get_camera_state() == Dispatcher.EMPTY_CAMERA_STATE

    def test_get_camera_state_with_primary(self):
        dispatcher, _, _ = _mk_dispatcher()
        cm = MagicMock()
        cm.primary_camera_id = MagicMock(return_value="cam1")
        cm.get_state.return_value = {"camera_id": "cam1", "action": "recording"}
        dispatcher._camera_manager = cm
        assert dispatcher._get_camera_state()["camera_id"] == "cam1"

    def test_get_camera_state_without_cameras_returns_empty(self):
        dispatcher, _, _ = _mk_dispatcher()
        cm = MagicMock()
        cm.primary_camera_id = MagicMock(return_value="")
        dispatcher._camera_manager = cm
        assert dispatcher._get_camera_state() == Dispatcher.EMPTY_CAMERA_STATE

    def test_set_ha_service_rebinds_validator(self):
        dispatcher, _, _ = _mk_dispatcher()
        ha = MagicMock()
        dispatcher.set_ha_service(ha)
        assert dispatcher._ha_service is ha
        assert dispatcher._validator._ha_service is ha

    @pytest.mark.asyncio
    async def test_handle_cancelled_swallows_interrupt_failure(self):
        dispatcher, _, _ = _mk_dispatcher()
        sink = MagicMock()
        sink.interrupt_all = AsyncMock(side_effect=RuntimeError("sink gone"))
        dispatcher._sink_manager = sink
        emitted: list = []

        async def emit(instr):
            emitted.append(instr)

        await dispatcher._handle_cancelled(emit, "r", "s")  # 不抛
        finish = [i for i in emitted if i.header.name == "Finish"]
        assert finish and _payload(finish[0].model_dump(), "success") is False
        sink.interrupt_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_broadcasting_after_emits_empty_status(self):
        dispatcher, _, _ = _mk_dispatcher()
        emitted: list = []

        async def emit(instr):
            emitted.append(instr)

        await dispatcher._clear_broadcasting_after(emit, "r", "s", delay=0.01)
        status = [i for i in emitted if i.header.name == "Status"]
        assert status and _payload(status[0].model_dump(), "phase") == ""

    @pytest.mark.asyncio
    async def test_clear_broadcasting_after_emit_failure_swallowed(self):
        dispatcher, _, _ = _mk_dispatcher()

        async def emit(instr):
            raise RuntimeError("ws gone")

        await dispatcher._clear_broadcasting_after(emit, "r", "s", delay=0.01)  # 不抛


class TestPrepareContext:
    def _dispatcher_full(self):
        ha_catalog = MagicMock(return_value="catalog")
        controls_results = [None, ["ctrl-1"]]
        ha_controls = MagicMock(side_effect=lambda: controls_results.pop(0))
        refresh = AsyncMock()
        vision = MagicMock()
        vision.get_vision_focuses = MagicMock(side_effect=RuntimeError("vision down"))
        summarization = MagicMock()
        summarization.refresh_summaries = AsyncMock(side_effect=RuntimeError("sum down"))
        dispatcher, _, _ = _mk_dispatcher(
            ha_catalog_provider=ha_catalog, ha_controls_provider=ha_controls,
            catalog_refresh_fn=refresh, vision_service=vision,
            summarization_service=summarization,
        )
        return dispatcher, refresh

    @pytest.mark.asyncio
    async def test_prepare_context_degrades_and_collects(self):
        dispatcher, refresh = self._dispatcher_full()
        session = SessionState(session_id="s", request_id="r")
        with patch("app.agents.dispatcher.build_system_prompt",
                   new=AsyncMock(return_value="SYS")) as mock_prompt:
            ctx = await dispatcher._prepare_context(session, "你好")
        # 依赖各自失败都被吞掉，主流程继续
        assert ctx["device_catalog"] == "catalog"
        assert ctx["device_controls"] == ["ctrl-1"]  # 空 controls → 同步刷新后取到
        refresh.assert_awaited_once()
        assert ctx["vision_focuses"] is None
        assert ctx["system_prompt"] == "SYS"
        assert ctx["query"] == "你好"
        # 最后一条是用户消息
        assert isinstance(ctx["lc_messages"][-1], HumanMessage)
        assert ctx["lc_messages"][-1].content == "你好"
        mock_prompt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prepare_context_catalog_failure_and_refresh_failure(self):
        ha_catalog = MagicMock(side_effect=RuntimeError("catalog down"))
        ha_controls = MagicMock(return_value=[])  # 空 → 触发刷新；刷新抛错
        refresh = AsyncMock(side_effect=RuntimeError("refresh down"))
        dispatcher, _ = self._dispatcher_full()
        dispatcher._ha_catalog_provider = ha_catalog
        dispatcher._ha_controls_provider = ha_controls
        dispatcher._catalog_refresh_fn = refresh
        session = SessionState(session_id="s", request_id="r")
        with patch("app.agents.dispatcher.build_system_prompt", new=AsyncMock(return_value="SYS")):
            ctx = await dispatcher._prepare_context(session, "你好")
        assert ctx["device_catalog"] is None
        assert ctx["device_controls"] == []  # 刷新失败 → controls 保持空列表
        refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prepare_context_system_prompt_failure_falls_back(self):
        dispatcher, _ = self._dispatcher_full()
        session = SessionState(session_id="s", request_id="r")
        with patch("app.agents.dispatcher.build_system_prompt",
                   new=AsyncMock(side_effect=RuntimeError("prompt down"))):
            ctx = await dispatcher._prepare_context(session, "你好")
        assert ctx["system_prompt"] == "你是 Aether 家庭智能助手。请尽力回答用户问题。"

    @pytest.mark.asyncio
    async def test_current_chat_model_user_override(self):
        dispatcher, _, _ = _mk_dispatcher()
        cfg = {"base_url": "http://x", "model": "user-model", "api_key": "k"}
        with patch("app.agents.dispatcher.load_model_config_for_user",
                   new=AsyncMock(return_value=cfg)):
            assert await dispatcher._current_chat_model("u1") == "user-model"

    @pytest.mark.asyncio
    async def test_current_chat_model_user_failure_returns_empty(self):
        dispatcher, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.load_model_config_for_user",
                   new=AsyncMock(side_effect=RuntimeError("db"))):
            # 全局 key 也未配置 → 空串
            with patch("app.agents.langgraph_agent._load_model_config_from_config",
                       side_effect=RuntimeError("no key")):
                assert await dispatcher._current_chat_model("u1") == ""

    @pytest.mark.asyncio
    async def test_prepare_context_injects_family_switch(self):
        """_current_chat_model 命中 per-user 模型 → 适配器改写本轮消息。"""
        dispatcher, _ = self._dispatcher_full()
        dispatcher._ha_controls_provider = MagicMock(return_value=["c"])
        session = SessionState(session_id="s", request_id="r")

        adapter = MagicMock()
        adapter.no_think.return_value = ("SYS/no_think", "你好/no_think")
        with patch("app.agents.dispatcher.build_system_prompt", new=AsyncMock(return_value="SYS")), \
             patch("app.agents.dispatcher.load_model_config_for_user",
                   new=AsyncMock(return_value={"model": "user-model"})), \
             patch("app.agents.dispatcher.get_adapter", return_value=adapter) as mock_ga:
            ctx = await dispatcher._prepare_context(session, "你好", user_id="u9")
        mock_ga.assert_called_with("user-model")
        assert ctx["lc_messages"][0].content == "SYS/no_think"
        assert ctx["lc_messages"][-1].content == "你好/no_think"


class TestInjectFamilySwitch:
    def _adapter(self):
        adapter = MagicMock()
        adapter.no_think.side_effect = lambda s, u: (s + "/S", u + "/U")
        return adapter

    def test_empty_model_noop(self):
        msgs = [HumanMessage("hi")]
        d, _, _ = _mk_dispatcher()
        d._inject_family_switch(msgs, "")
        assert msgs[0].content == "hi"

    def test_adapter_none_noop(self):
        msgs = [HumanMessage("hi")]
        d, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.get_adapter", return_value=None):
            d._inject_family_switch(msgs, "m")
        assert msgs[0].content == "hi"

    def test_adapter_query_failure_noop(self):
        msgs = [HumanMessage("hi")]
        d, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.get_adapter", side_effect=RuntimeError("boom")):
            d._inject_family_switch(msgs, "m")
        assert msgs[0].content == "hi"

    def test_last_message_not_human_noop(self):
        msgs = [SystemMessage("sys"), AIMessage("ai")]
        d, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.get_adapter", return_value=self._adapter()):
            d._inject_family_switch(msgs, "m")
        assert msgs[1].content == "ai"

    def test_inject_with_system(self):
        msgs = [SystemMessage("sys"), HumanMessage("hi")]
        d, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.get_adapter", return_value=self._adapter()):
            d._inject_family_switch(msgs, "m")
        assert msgs[0].content == "sys/S"
        assert msgs[1].content == "hi/U"

    def test_inject_without_system(self):
        msgs = [HumanMessage("hi")]
        d, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.get_adapter", return_value=self._adapter()):
            d._inject_family_switch(msgs, "m", include_system=False)
        assert msgs[0].content == "hi/U"

    def test_inject_no_system_message_only_user_replaced(self):
        msgs = [HumanMessage("a"), HumanMessage("b")]
        d, _, _ = _mk_dispatcher()
        with patch("app.agents.dispatcher.get_adapter", return_value=self._adapter()):
            d._inject_family_switch(msgs, "m", include_system=True)
        assert msgs[0].content == "a"  # 无 system 首条不动
        assert msgs[1].content == "b/U"

    @pytest.mark.asyncio
    async def test_run_turn_without_agent_arg_uses_global(self):
        """_run_turn(agent=None) 回退 self._agent。"""
        dispatcher, _, _ = _mk_dispatcher()
        store = dispatcher._session_store
        session = await store.get_or_create("s", "r")
        ctx = {"lc_messages": [HumanMessage("hi")], "system_prompt": "SYS",
               "device_catalog": None, "chat_model": ""}
        emitted: list = []

        async def emit(instr):
            emitted.append(instr)

        rounds = [[_tok("回复")]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)) as mock_stream, \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            await dispatcher._run_turn(_event(rid="r", sid="s"), session, "hi", ctx, emit,
                                       stream_tokens=False)
        # 第一个位置参数是 self._agent（MagicMock）
        assert mock_stream.call_args.args[0] is dispatcher._agent
        toast = [i for i in emitted if i.header.name == "ToastStream"]
        assert toast and _payload(toast[0].model_dump(), "stream") == "回复"

    @pytest.mark.asyncio
    async def test_run_turn_entity_name_map_fetch_and_failure(self):
        """ha_service 提供/抛错时 entity_name_map 不阻塞主流程。"""
        ha = MagicMock()
        ha.get_entity_name_map = AsyncMock(return_value={"light.a": "灯"})
        dispatcher, _, _ = _mk_dispatcher(ha_service=ha)
        session = SessionState(session_id="s", request_id="r")
        ctx = {"lc_messages": [HumanMessage("hi")], "system_prompt": "SYS",
               "device_catalog": None, "chat_model": ""}

        async def emit(instr):
            pass

        rounds = [[_tok("ok")]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False) as sr:
            await dispatcher._run_turn(_event(rid="r", sid="s"), session, "hi", ctx, emit,
                                       stream_tokens=False)
        # entity_name_map 传入 validator
        assert sr.await_args.kwargs.get("entity_name_map") == {"light.a": "灯"}

        ha.get_entity_name_map = AsyncMock(side_effect=RuntimeError("ha down"))
        rounds2 = [[_tok("ok")]]
        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_rounds_stream(rounds2)), \
             patch.object(dispatcher._validator, "should_retry", return_value=False) as sr2:
            await dispatcher._run_turn(_event(rid="r", sid="s"), session, "hi", ctx, emit,
                                       stream_tokens=False)
        assert sr2.await_args.kwargs.get("entity_name_map") == {}


# ---------------------------------------------------------------------------
# app/agents/automation_agent.py
# ---------------------------------------------------------------------------

def _svc_mock():
    svc = MagicMock()
    svc.evaluate = AsyncMock()
    return svc


def _cam_mgr_mock():
    mgr = MagicMock()
    mgr.list_cameras.return_value = [{"id": "cam1"}]
    mgr.get_state.return_value = {"camera_opened": True}
    mgr.get_recent_frames.return_value = [[1, 2]]
    return mgr


class TestSilentTickLoopControl:
    @pytest.mark.asyncio
    async def test_loop_breaks_when_running_false(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc, camera_manager=_cam_mgr_mock())
        agent._silent_interval = 0.05
        agent._running = True
        agent._loop = asyncio.get_running_loop()
        t = asyncio.create_task(agent._silent_tick_loop())
        await asyncio.sleep(0.12)
        agent._running = False
        await asyncio.wait_for(t, 1.0)  # _running=False → break 而非挂死
        assert svc.evaluate.await_count >= 1

    @pytest.mark.asyncio
    async def test_loop_breaks_when_silent_disabled_midway(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc)
        agent._silent_interval = 0.05
        agent._running = True
        agent._loop = asyncio.get_running_loop()
        t = asyncio.create_task(agent._silent_tick_loop())
        await asyncio.sleep(0.12)
        agent._silent_enabled = False
        await asyncio.wait_for(t, 1.0)

    @pytest.mark.asyncio
    async def test_loop_swallows_cycle_exception(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc)
        agent._silent_interval = 0.05
        agent._running = True
        agent._loop = asyncio.get_running_loop()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            raise RuntimeError("cycle boom")

        agent._run_evaluation_cycle = flaky
        t = asyncio.create_task(agent._silent_tick_loop())
        await asyncio.sleep(0.3)
        agent._running = False
        await asyncio.wait_for(t, 1.0)
        assert calls["n"] >= 2  # 异常后循环继续而非退出

    def test_start_silent_tick_without_loop_noop(self):
        agent = AutomationAgent()
        agent._start_silent_tick()
        assert agent._silent_task is None

    @pytest.mark.asyncio
    async def test_start_silent_tick_twice_keeps_same_task(self):
        agent = AutomationAgent(silent_eval_enabled=False)
        await agent.start()
        try:
            agent._start_silent_tick()
            t1 = agent._silent_task
            agent._start_silent_tick()  # 已有未完成任务 → 不重建
            assert agent._silent_task is t1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_restart_silent_tick(self):
        agent = AutomationAgent(silent_eval_enabled=False)
        await agent.start()
        try:
            agent._start_silent_tick()
            t1 = agent._silent_task
            agent._restart_silent_tick()
            assert agent._silent_task is not t1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_silent_interval_debounce_applies_and_evaluates(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc, camera_manager=_cam_mgr_mock(),
                                silent_eval_interval=60.0)
        await agent.start()
        try:
            old_task = agent._silent_task
            agent.set_silent_interval(20.0)
            await asyncio.sleep(0.7)
            assert agent._silent_interval == 20.0
            assert agent._pending_silent_interval is None
            assert agent._silent_task is not old_task  # 间隔变化 → 重启 tick
            assert svc.evaluate.await_count >= 1       # 切换后立刻评估一次
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_silent_interval_coalesces_calls(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc, silent_eval_interval=60.0)
        await agent.start()
        try:
            agent.set_silent_interval(10.0)
            first_debounce = agent._silent_debounce_task
            agent.set_silent_interval(30.0)  # 防抖等待中新值只记 pending
            assert agent._silent_debounce_task is first_debounce
            await asyncio.sleep(0.7)
            assert agent._silent_interval == 30.0
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_silent_interval_same_value_no_restart_but_evaluates(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc, camera_manager=_cam_mgr_mock(),
                                silent_eval_interval=60.0)
        await agent.start()
        try:
            task_before = agent._silent_task
            agent.set_silent_interval(60.0)  # 值不变 → 不重启
            await asyncio.sleep(0.7)
            assert agent._silent_task is task_before
            assert svc.evaluate.await_count >= 1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_debounce_cancelled_is_swallowed(self):
        agent = AutomationAgent()
        await agent.start()
        try:
            agent.set_silent_interval(15.0)
            await asyncio.sleep(0.01)  # 让防抖任务先进入 sleep，取消才落在函数体内
            task = agent._silent_debounce_task
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1.0)
            assert agent._silent_interval != 15.0  # 被取消 → 未生效
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_nonvision_debounce_cancelled_is_swallowed(self):
        agent = AutomationAgent()
        await agent.start()
        try:
            agent.set_nonvision_silent_interval(15.0)
            await asyncio.sleep(0.01)
            task = agent._nonvision_debounce_task
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1.0)
            assert agent._nonvision_interval != 15.0
        finally:
            await agent.stop()

    def test_set_silent_interval_without_loop_noop(self):
        agent = AutomationAgent()
        agent.set_silent_interval(10.0)
        assert agent._pending_silent_interval is None

    @pytest.mark.asyncio
    async def test_set_silent_enabled_without_loop_noop(self):
        agent = AutomationAgent()
        agent.set_silent_enabled(True)  # 无 loop → 不调度不抛
        assert agent._silent_task is None

    def test_apply_silent_enabled_same_value_noop(self):
        agent = AutomationAgent(silent_eval_enabled=True)
        agent._apply_silent_enabled(True)  # 值相同 → 早退
        assert agent._silent_task is None


class TestNonvisionTickLoopControl:
    @pytest.mark.asyncio
    async def test_loop_breaks_when_running_false(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc)
        agent._nonvision_interval = 0.05
        agent._running = True
        agent._loop = asyncio.get_running_loop()
        t = asyncio.create_task(agent._nonvision_tick_loop())
        await asyncio.sleep(0.12)
        agent._running = False
        await asyncio.wait_for(t, 1.0)
        assert svc.evaluate.await_count >= 1

    @pytest.mark.asyncio
    async def test_loop_swallows_cycle_exception(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc)
        agent._nonvision_interval = 0.05
        agent._running = True
        agent._loop = asyncio.get_running_loop()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            raise RuntimeError("nonvision boom")

        agent._run_nonvision_cycle = flaky
        t = asyncio.create_task(agent._nonvision_tick_loop())
        await asyncio.sleep(0.3)
        agent._running = False
        await asyncio.wait_for(t, 1.0)
        assert calls["n"] >= 2

    def test_start_nonvision_tick_without_loop_noop(self):
        agent = AutomationAgent()
        agent._start_nonvision_tick()
        assert agent._nonvision_task is None

    @pytest.mark.asyncio
    async def test_start_nonvision_tick_twice_keeps_same_task(self):
        agent = AutomationAgent(nonvision_silent_enabled=False)
        await agent.start()
        try:
            agent._start_nonvision_tick()
            t1 = agent._nonvision_task
            agent._start_nonvision_tick()
            assert agent._nonvision_task is t1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_restart_nonvision_tick(self):
        agent = AutomationAgent(nonvision_silent_enabled=False)
        await agent.start()
        try:
            agent._start_nonvision_tick()
            t1 = agent._nonvision_task
            agent._restart_nonvision_tick()
            assert agent._nonvision_task is not t1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_nonvision_interval_debounce_applies(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc, nonvision_silent_interval=60.0)
        await agent.start()
        try:
            old_task = agent._nonvision_task
            agent.set_nonvision_silent_interval(25.0)
            await asyncio.sleep(0.7)
            assert agent._nonvision_interval == 25.0
            assert agent._nonvision_task is not old_task
            assert svc.evaluate.await_count >= 1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_nonvision_interval_coalesces(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc, nonvision_silent_interval=60.0)
        await agent.start()
        try:
            agent.set_nonvision_silent_interval(10.0)
            first = agent._nonvision_debounce_task
            agent.set_nonvision_silent_interval(40.0)
            assert agent._nonvision_debounce_task is first
            await asyncio.sleep(0.7)
            assert agent._nonvision_interval == 40.0
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_nonvision_interval_same_value_no_restart(self):
        svc = _svc_mock()
        agent = AutomationAgent(automation_service=svc, nonvision_silent_interval=60.0)
        await agent.start()
        try:
            task_before = agent._nonvision_task
            agent.set_nonvision_silent_interval(60.0)
            await asyncio.sleep(0.7)
            assert agent._nonvision_task is task_before
            assert svc.evaluate.await_count >= 1
        finally:
            await agent.stop()

    def test_set_nonvision_interval_without_loop_noop(self):
        agent = AutomationAgent()
        agent.set_nonvision_silent_interval(10.0)
        assert agent._pending_nonvision_interval is None

    @pytest.mark.asyncio
    async def test_set_nonvision_enabled_without_loop_noop(self):
        agent = AutomationAgent()
        agent.set_nonvision_silent_enabled(True)
        assert agent._nonvision_task is None

    def test_apply_nonvision_enabled_same_value_noop(self):
        agent = AutomationAgent(nonvision_silent_enabled=False)
        agent._apply_nonvision_enabled(False)
        assert agent._nonvision_task is None


class TestEvaluationCycleEdgeBranches:
    @pytest.mark.asyncio
    async def test_offline_camera_skipped(self):
        svc = _svc_mock()
        mgr = MagicMock()
        mgr.list_cameras.return_value = [{"id": "cam_off"}]
        mgr.get_state.return_value = {"camera_opened": False}  # 离线
        agent = AutomationAgent(automation_service=svc, camera_manager=mgr)
        await agent._run_evaluation_cycle()
        svc.evaluate.assert_not_awaited()
        assert agent._eval_count == 1  # 计数仍 +1

    @pytest.mark.asyncio
    async def test_cycle_exception_resets_running_flag(self):
        svc = _svc_mock()
        mgr = MagicMock()
        mgr.list_cameras.side_effect = RuntimeError("manager exploded")
        agent = AutomationAgent(automation_service=svc, camera_manager=mgr)
        await agent._run_evaluation_cycle()  # 不抛
        assert agent._eval_running is False

    @pytest.mark.asyncio
    async def test_nonvision_no_service_returns_after_count(self):
        agent = AutomationAgent(automation_service=None)
        await agent._run_nonvision_cycle()
        assert agent._nonvision_eval_count == 1

    @pytest.mark.asyncio
    async def test_nonvision_cycle_exception_resets_running_flag(self):
        svc = _svc_mock()
        svc.evaluate = AsyncMock(side_effect=RuntimeError("eval boom"))
        agent = AutomationAgent(automation_service=svc)
        await agent._run_nonvision_cycle()  # 不抛
        assert agent._nonvision_eval_running is False


# ---------------------------------------------------------------------------
# app/agents/validator_agent.py
# ---------------------------------------------------------------------------

class _StubHA:
    def __init__(self, states=None, exc=None):
        self._states = states or []
        self._exc = exc
        self.calls = 0

    async def get_states_snapshot(self):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._states


class TestExpectedState:
    def test_cover_open_verbs(self):
        assert _expected_state("拉开", "cover.c") == "open"
        assert _expected_state("升起", "cover.c") == "open"

    def test_cover_close_verbs(self):
        assert _expected_state("拉上", "cover.c") == "closed"
        assert _expected_state("降下", "cover.c") == "closed"

    def test_cover_unknown_verb_none(self):
        assert _expected_state("暂停", "cover.c") is None

    def test_onoff_domains(self):
        assert _expected_state("关", "light.l") == "off"
        assert _expected_state("熄灭", "light.l") == "off"
        assert _expected_state("停止", "switch.s") == "off"
        assert _expected_state("启动", "fan.f") == "on"

    def test_onoff_unknown_verb_none(self):
        assert _expected_state("调节", "light.l") is None

    def test_non_onoff_domain_none(self):
        assert _expected_state("打开", "climate.c") is None

    def test_entity_without_dot_none(self):
        assert _expected_state("打开", "light") is None


class TestValidatorClientLifecycle:
    @pytest.mark.asyncio
    async def test_invalidate_user_closes_cached_clients(self):
        validator = ValidatorAgent()
        sync_c = MagicMock()
        async_c = MagicMock()
        async_c.aclose = AsyncMock()
        validator._user_llms["u1"] = MagicMock()
        validator._user_clients["u1"] = (sync_c, async_c)
        validator.invalidate_user("u1")
        sync_c.close.assert_called_once()
        assert "u1" not in validator._user_llms
        assert "u1" not in validator._user_clients
        await asyncio.sleep(0)  # 让 aclose task 跑起来
        async_c.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_invalidate_user_noop_for_unknown(self):
        validator = ValidatorAgent()
        validator.invalidate_user("ghost")  # 不抛
        assert validator._user_llms == {}

    @pytest.mark.asyncio
    async def test_close_clients_sync_failure_swallowed(self):
        validator = ValidatorAgent()
        sync_c = MagicMock()
        sync_c.close.side_effect = RuntimeError("boom")
        async_c = MagicMock()
        async_c.aclose = AsyncMock()
        validator._close_clients((sync_c, async_c))
        await asyncio.sleep(0)
        async_c.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_close_clients_without_running_loop_tolerated(self):
        validator = ValidatorAgent()
        async_c = MagicMock()
        async_c.aclose = AsyncMock()
        # 工作线程无事件循环 → RuntimeError → 静默交由 GC
        await asyncio.to_thread(validator._close_clients, (MagicMock(), async_c))

    @pytest.mark.asyncio
    async def test_close_all_closes_every_client(self):
        validator = ValidatorAgent()
        c1, c2 = MagicMock(), MagicMock()
        a1, a2 = MagicMock(), MagicMock()
        a1.aclose = AsyncMock()
        a2.aclose = AsyncMock()
        validator._user_llms = {"u1": MagicMock(), "u2": MagicMock()}
        validator._user_clients = {"u1": (c1, a1), "u2": (c2, a2)}
        await validator.close_all()
        assert validator._user_clients == {} and validator._user_llms == {}
        c1.close.assert_called_once()
        a1.aclose.assert_awaited()
        a2.aclose.assert_awaited()

    def test_set_ha_service_rebinds(self):
        validator = ValidatorAgent()
        ha = _StubHA([])
        validator.set_ha_service(ha)
        assert validator._ha_service is ha

    @pytest.mark.asyncio
    async def test_get_llm_builds_when_none(self):
        validator = ValidatorAgent()
        mock_llm = MagicMock()
        with patch.object(ValidatorAgent, "_build_llm", return_value=mock_llm) as mock_build:
            assert validator._get_llm("") is mock_llm
            mock_build.assert_called_once()
            # 二次调用命中缓存
            assert validator._get_llm("") is mock_llm
            assert mock_build.call_count == 1

    @pytest.mark.asyncio
    async def test_resolve_user_llm_failure_returns_none(self):
        validator = ValidatorAgent()
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            assert await validator._resolve_user_llm("u1") is None
        assert "u1" not in validator._user_llms


class TestValidatorBuildLLM:
    def test_build_llm_with_global_key(self):
        key_entry = {"base_url": "https://api.x.com/v1/", "model": "m1", "api_key": ""}
        with patch("app.agents.validator_agent.resolve_key_for_role", return_value=key_entry), \
             patch("app.agents.validator_agent.ChatOpenAI") as mock_chat, \
             patch("app.clients.http_client.new_client"), \
             patch("app.clients.http_client.new_sync_client"):
            ValidatorAgent._build_llm()
        kwargs = mock_chat.call_args.kwargs
        assert kwargs["base_url"] == "https://api.x.com/v1"  # 尾斜杠去除
        assert kwargs["api_key"] == "not-needed"  # 空 key 兜底
        assert kwargs["model"] == "m1"
        assert kwargs["temperature"] == 0.0

    def test_build_llm_fallback_local_base_url_gets_v1(self):
        with patch("app.agents.validator_agent.resolve_key_for_role", return_value=None), \
             patch("app.agents.validator_agent.get_config",
                   side_effect=lambda k, d=None: "http://127.0.0.1:11434" if k == "llm.base_url" else d), \
             patch("app.agents.validator_agent.ChatOpenAI") as mock_chat, \
             patch("app.clients.http_client.new_client"), \
             patch("app.clients.http_client.new_sync_client"):
            ValidatorAgent._build_llm()
        assert mock_chat.call_args.kwargs["base_url"] == "http://127.0.0.1:11434/v1"

    def test_build_llm_fallback_remote_base_url_unchanged(self):
        with patch("app.agents.validator_agent.resolve_key_for_role", return_value=None), \
             patch("app.agents.validator_agent.get_config",
                   side_effect=lambda k, d=None: "https://api.remote.com/v1" if k == "llm.base_url" else d), \
             patch("app.agents.validator_agent.ChatOpenAI") as mock_chat, \
             patch("app.clients.http_client.new_client"), \
             patch("app.clients.http_client.new_sync_client"):
            ValidatorAgent._build_llm()
        assert mock_chat.call_args.kwargs["base_url"] == "https://api.remote.com/v1"


class TestValidatorReadEntityState:
    @pytest.mark.asyncio
    async def test_no_ha_service_returns_none(self):
        validator = ValidatorAgent()
        assert await validator._read_entity_state("light.a") is None

    @pytest.mark.asyncio
    async def test_snapshot_failure_returns_none(self):
        validator = ValidatorAgent(ha_service=_StubHA(exc=RuntimeError("ha down")))
        assert await validator._read_entity_state("light.a") is None

    @pytest.mark.asyncio
    async def test_entity_missing_returns_none(self):
        validator = ValidatorAgent(ha_service=_StubHA([{"entity_id": "other.l", "state": "on"}]))
        assert await validator._read_entity_state("light.a") is None

    @pytest.mark.asyncio
    async def test_state_none_value_returns_none(self):
        validator = ValidatorAgent(ha_service=_StubHA([{"entity_id": "light.a", "state": None}]))
        assert await validator._read_entity_state("light.a") is None

    @pytest.mark.asyncio
    async def test_state_found_returns_str(self):
        validator = ValidatorAgent(ha_service=_StubHA([{"entity_id": "light.a", "state": "on"}]))
        assert await validator._read_entity_state("light.a") == "on"


class TestValidatorToolCallCountBranch:
    @pytest.mark.asyncio
    async def test_tools_called_and_intent_goes_to_llm_check(self):
        """tool_call_count>0 + query 带意图 → 保留 LLM 语义兜底。"""
        validator = ValidatorAgent(max_retries=1)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content='{"need_retry": true}'))
        validator._llm = mock_llm
        result = await validator.should_retry("好的呢", 2, query="把灯打开")
        assert result is True
        mock_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tools_called_without_intent_skips_llm(self):
        validator = ValidatorAgent(max_retries=1)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock()
        validator._llm = mock_llm
        assert await validator.should_retry("好的呢", 2, query="你好") is False
        mock_llm.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claim_entity_missing_from_snapshot_generic_retry(self):
        """实体匹配到名字但 HA 快照里查不到 → actual None → 通用重试。"""
        validator = ValidatorAgent(max_retries=1, ha_service=_StubHA([]))
        result = await validator.should_retry(
            "已经打开客厅灯了", 0, query="把客厅灯打开",
            entity_name_map={"light.kt": "客厅灯"})
        assert result is True
        assert validator._pending_retry_message is None  # 通用重试，无定向消息

    @pytest.mark.asyncio
    async def test_entity_name_too_short_is_skipped_in_match(self):
        """友好名清洗后不足 2 字的实体被跳过（单字"灯"会误碰任何灯）。"""
        validator = ValidatorAgent(max_retries=1, ha_service=_StubHA(
            [{"entity_id": "light.kt", "state": "off"}]))
        result = await validator.should_retry(
            "已经打开客厅灯了", 0, query="把客厅灯打开",
            entity_name_map={"light.a": "灯", "light.kt": "客厅灯"})
        assert result is True  # 状态不符 → 定向重试（"灯"实体被跳过，未误匹配）
        msg = validator.build_retry_message()
        assert "light.kt" in msg.content  # 匹配的是清洗后合法的实体

    @pytest.mark.asyncio
    async def test_close_all_swallows_aclose_failure(self):
        validator = ValidatorAgent()
        sync_c = MagicMock()
        async_c = MagicMock()
        async_c.aclose = AsyncMock(side_effect=RuntimeError("aclose boom"))
        validator._user_llms = {"u1": MagicMock()}
        validator._user_clients = {"u1": (sync_c, async_c)}
        await validator.close_all()  # 不抛
        assert validator._user_clients == {}


# ---------------------------------------------------------------------------
# app/services/automation_service.py
# ---------------------------------------------------------------------------

def _registry(rules):
    reg = MagicMock()
    reg.list_rules.return_value = rules
    return reg


def _chat_svc(registry, reply="1"):
    svc = AutomationService(registry, vision_service=MagicMock())
    chat = MagicMock()
    chat.chat = AsyncMock(return_value=reply)
    svc._chat_client = chat
    svc._build_condition_context = AsyncMock(return_value="ctx")
    return svc, chat


class TestEvaluateFilterBranches:
    @pytest.mark.asyncio
    async def test_cooldown_skips_rule(self):
        rule = {"id": "r1", "name": "t", "type": "time", "condition": "晚上",
                "enabled": True, "cooldown_seconds": 100, "last_triggered_at": time.time(),
                "actions": []}
        svc, chat = _chat_svc(_registry([rule]))
        applied = await svc.evaluate(frames=None, rule_types=("time",))
        assert applied == []
        chat.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_condition_skips_rule(self):
        rule = {"id": "r1", "name": "t", "type": "time", "condition": "  ",
                "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0.0,
                "actions": []}
        svc, chat = _chat_svc(_registry([rule]))
        applied = await svc.evaluate(frames=None, rule_types=("time",))
        assert applied == []
        chat.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ha_snapshot_failure_disables_gate_not_evaluation(self):
        ha = MagicMock()
        ha.get_states_snapshot = AsyncMock(side_effect=RuntimeError("ha down"))
        rule = {"id": "r1", "name": "t", "type": "time", "condition": "晚上",
                "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0.0,
                "actions": []}
        svc, chat = _chat_svc(_registry([rule]))
        svc._ha_service = ha
        applied = await svc.evaluate(frames=None, rule_types=("time",))
        assert applied == []  # result 0 → 无动作，但规则确实被评估了
        chat.chat.assert_awaited_once()


class TestEvaluateTimeouts:
    @pytest.mark.asyncio
    async def test_chat_group_timeout_skips(self, monkeypatch):
        monkeypatch.setattr("app.services.automation_service._EVAL_TIMEOUT_SECONDS", 0.05)
        rule = {"id": "r1", "name": "t", "type": "time", "condition": "晚上",
                "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0.0,
                "actions": []}
        reg = _registry([rule])
        svc = AutomationService(reg, vision_service=MagicMock())
        svc._build_condition_context = AsyncMock(return_value="ctx")

        async def slow_ctx(*a, **kw):
            await asyncio.sleep(0.5)
            return 1

        with patch.object(svc, "_evaluate_context_only", side_effect=slow_ctx):
            applied = await svc.evaluate(frames=None, rule_types=("time",))
        assert applied == []

    @pytest.mark.asyncio
    async def test_vision_group_timeout_skips(self, monkeypatch):
        monkeypatch.setattr("app.services.automation_service._EVAL_TIMEOUT_SECONDS", 0.05)
        rule = {"id": "r1", "name": "t", "type": "vision", "condition": "有人",
                "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0.0,
                "actions": []}
        vision = MagicMock()
        vision.encode_frames_b64 = AsyncMock(return_value="b64")

        async def slow_vl(*a, **kw):
            await asyncio.sleep(0.5)
            return 1

        vision.evaluate_condition = slow_vl
        svc = AutomationService(_registry([rule]), vision_service=vision)
        svc._build_condition_context = AsyncMock(return_value="ctx")
        applied = await svc.evaluate(frames=[[1]], rule_types=("vision",))
        assert applied == []


class TestApplyResultsSideEffects:
    @pytest.mark.asyncio
    async def test_alert_record_failure_swallowed(self):
        """alert_service.record 抛错不影响动作执行结果。"""
        rule = {"id": "r1", "name": "灯", "type": "vision", "condition": "有人",
                "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0.0,
                "actions": [{"mcp_tool_name": "t", "mcp_tool_input": {}}]}
        vision = MagicMock()
        vision.evaluate_condition = AsyncMock(return_value=1)
        vision.encode_frames_b64 = AsyncMock(return_value="b64")
        executor = MagicMock()
        executor.resolve_tool_name = lambda n: n
        executor.execute_tool_by_name = AsyncMock(return_value={"success": True})
        svc = AutomationService(_registry([rule]), tool_executor=executor, vision_service=vision)
        svc._build_condition_context = AsyncMock(return_value="ctx")
        with patch("app.services.alert_service.alert_service.record",
                   new=AsyncMock(side_effect=RuntimeError("alert db down"))):
            applied = await svc.evaluate(frames=[[1]], rule_types=("vision",))
        assert len(applied) == 1  # 动作已执行，告警失败仅吞掉
        assert applied[0]["result"]["tool"] == "t"


class TestDeriveTargetState:
    def setup_method(self):
        self.svc = AutomationService.__new__(AutomationService)

    def test_turn_on(self):
        assert self.svc._derive_target_state("light", "turn_on", {}) == {"state": "on"}

    def test_open_cover(self):
        assert self.svc._derive_target_state("cover", "open_cover", {}) == {"state": "open"}

    def test_set_temperature(self):
        assert self.svc._derive_target_state("climate", "set_temperature", {"temperature": 26}) == \
            {"attributes": {"temperature": 26}}

    def test_set_temperature_without_value_none(self):
        assert self.svc._derive_target_state("climate", "set_temperature", {}) is None

    def test_set_humidity(self):
        assert self.svc._derive_target_state("humidifier", "set_humidity", {"humidity": 55}) == \
            {"attributes": {"humidity": 55}}

    def test_set_humidity_without_value_none(self):
        assert self.svc._derive_target_state("humidifier", "set_humidity", {}) is None

    def test_set_cover_position(self):
        assert self.svc._derive_target_state("cover", "set_cover_position", {"position": 50}) == \
            {"attributes": {"current_position": 50}}
        assert self.svc._derive_target_state("cover", "set_position", {"position": 50}) == \
            {"attributes": {"current_position": 50}}

    def test_set_brightness(self):
        assert self.svc._derive_target_state("light", "set_brightness", {"brightness": 128}) == \
            {"attributes": {"brightness": 128}}


class TestMatchesTargetState:
    def setup_method(self):
        self.svc = AutomationService.__new__(AutomationService)

    def test_current_attr_fallback(self):
        """attributes 里查不到时回退 current_<attr>。"""
        current = {"state": "open", "attributes": {"current_position": 50}}
        assert self.svc._matches_target_state(current, {"attributes": {"position": 50}}) is True

    def test_missing_attr_not_match(self):
        current = {"state": "open", "attributes": {}}
        assert self.svc._matches_target_state(current, {"attributes": {"position": 50}}) is False

    def test_non_numeric_attr_equal(self):
        current = {"state": "eco", "attributes": {"mode": "eco"}}
        assert self.svc._matches_target_state(current, {"attributes": {"mode": "eco"}}) is True

    def test_non_numeric_attr_different(self):
        current = {"state": "eco", "attributes": {"mode": "night"}}
        assert self.svc._matches_target_state(current, {"attributes": {"mode": "eco"}}) is False

    def test_target_without_state_or_attributes(self):
        assert self.svc._matches_target_state({"state": "on"}, {}) is False


class TestExecuteActionFailures:
    def _svc(self, executor=..., **kw):
        reg = _registry([])
        executor_arg = MagicMock() if executor is ... else executor
        return AutomationService(reg, tool_executor=executor_arg, **kw)

    @pytest.mark.asyncio
    async def test_action_without_tool_name_skipped(self):
        svc = self._svc()
        result = await svc._execute_action({"parameters": {}})
        assert result is None

    @pytest.mark.asyncio
    async def test_no_executor_returns_none(self):
        svc = self._svc(executor=None)
        result = await svc._execute_action({"tool_name": "t", "parameters": {}})
        assert result is None

    @pytest.mark.asyncio
    async def test_executor_exception_records_error_log(self):
        executor = MagicMock()
        executor.resolve_tool_name = lambda n: n
        executor.execute_tool_by_name = AsyncMock(side_effect=RuntimeError("mcp down"))
        svc = self._svc(executor=executor)
        result = await svc._execute_action({"tool_name": "t", "parameters": {"a": 1}},
                                           camera_id="cam1")
        assert result is None

    @pytest.mark.asyncio
    async def test_tool_failure_result_records_error_log(self):
        executor = MagicMock()
        executor.resolve_tool_name = lambda n: n
        executor.execute_tool_by_name = AsyncMock(return_value={"success": False, "error": "设备离线"})
        svc = self._svc(executor=executor)
        result = await svc._execute_action({"tool_name": "t", "parameters": {}})
        assert result is None

    @pytest.mark.asyncio
    async def test_virtual_dry_run_exception_returns_false(self):
        svc = self._svc()
        cm = MagicMock()
        cm.is_virtual_camera = MagicMock(side_effect=RuntimeError("cm boom"))
        svc._camera_manager = cm
        assert svc._virtual_dry_run("cam1") is False

    @pytest.mark.asyncio
    async def test_record_action_log_includes_error_and_result(self):
        svc = self._svc()
        db = MagicMock()
        db.vision_log_insert = AsyncMock()
        with patch("app.core.database.Database.get", return_value=db):
            await svc._record_action_log("cam1", "tool_t", {"a": 1},
                                         attempted=True, error="bad", result_summary={"r": 2})
        assert db.vision_log_insert.await_count == 1
        content = db.vision_log_insert.await_args.args[2]
        assert content["error"] == "bad"
        assert content["result"] == {"r": 2}
        assert content["attempted"] is True


class TestResolveChatClientCaching:
    @pytest.mark.asyncio
    async def test_cached_client_reused_when_signature_unchanged(self):
        svc = AutomationService(_registry([]))
        cached = MagicMock()
        svc._per_user_clients["u1"] = (("k1", "https://b", "m1"), cached)
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value={"api_key": "k1", "base_url": "https://b", "model": "m1"})):
            client = await svc._resolve_chat_client("u1")
        assert client is cached

    @pytest.mark.asyncio
    async def test_removed_per_user_config_clears_cache_and_falls_back(self):
        svc = AutomationService(_registry([]))
        cached = MagicMock()
        svc._per_user_clients["u1"] = (("k1", "b", "m"), cached)
        global_chat = MagicMock()
        svc._chat_client = global_chat
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)):
            client = await svc._resolve_chat_client("u1")
        assert client is global_chat
        assert "u1" not in svc._per_user_clients

    @pytest.mark.asyncio
    async def test_resolve_failure_falls_back_to_global(self):
        """key 解析抛错 → key_info=None → 回退全局 client，不抛。"""
        svc = AutomationService(_registry([]))
        global_chat = MagicMock()
        svc._chat_client = global_chat
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            client = await svc._resolve_chat_client("u1")
        assert client is global_chat
        assert svc._per_user_clients == {}

    @pytest.mark.asyncio
    async def test_global_client_lazy_init(self):
        svc = AutomationService(_registry([]))
        mock_client_cls = MagicMock(return_value=MagicMock())
        with patch("app.clients.llm_chat_client.LlmChatClient", mock_client_cls):
            client = await svc._resolve_chat_client("")
        mock_client_cls.assert_called_once_with(role="chat")
        assert client is mock_client_cls.return_value


class TestEvaluateContextOnlyParsing:
    def _svc_with_chat(self, reply=None, exc=None):
        svc = AutomationService(_registry([]))
        chat = MagicMock()
        if exc is not None:
            chat.chat = AsyncMock(side_effect=exc)
        else:
            chat.chat = AsyncMock(return_value=reply)
        svc._chat_client = chat
        return svc, chat

    @pytest.mark.asyncio
    async def test_non_numeric_reply_returns_zero(self):
        svc, chat = self._svc_with_chat("条件无法判断")
        assert await svc._evaluate_context_only("晚上10点", "ctx") == 0

    @pytest.mark.asyncio
    async def test_zero_reply_returns_zero(self):
        svc, _ = self._svc_with_chat("0")
        assert await svc._evaluate_context_only("晚上10点", "ctx") == 0

    @pytest.mark.asyncio
    async def test_chat_exception_returns_zero(self):
        svc, _ = self._svc_with_chat(exc=RuntimeError("llm down"))
        assert await svc._evaluate_context_only("晚上10点", "ctx") == 0


# ---------------------------------------------------------------------------
# app/agents/model_family_adapters.py
# ---------------------------------------------------------------------------

@pytest.fixture
def _reset_registry():
    yield
    reset_adapters()


def _write_plugin(root, plugin_id, adapters_source=None):
    pdir = root / plugin_id
    pdir.mkdir(parents=True)
    manifest = {
        "id": plugin_id, "name": plugin_id, "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "model_adapter", "id": f"{plugin_id}_cap"}],
    }
    (pdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if adapters_source is not None:
        (pdir / "adapters.py").write_text(adapters_source, encoding="utf-8")
    return pdir


_GOOD_ADAPTER = (
    "import re\n"
    "from app.agents.model_family_adapters import ModelFamilyAdapter\n"
    "class A(ModelFamilyAdapter):\n"
    "    family = 'q'\n"
    "    _match_re = re.compile('qwen', re.I)\n"
    "ADAPTERS = [A()]\n"
)


class TestAdapterModuleLoading:
    def test_default_plugin_dir_from_config(self):
        d = _default_plugin_dir()
        assert d.name == "integrations"

    def test_spec_none_returns_empty(self, tmp_path):
        pdir = _write_plugin(tmp_path, "p1", _GOOD_ADAPTER)
        with patch("importlib.util.spec_from_file_location", return_value=None):
            assert _load_adapters_module(pdir, "p1") == []

    def test_exec_failure_returns_empty(self, tmp_path):
        pdir = _write_plugin(tmp_path, "p1", "raise RuntimeError('adapter boom')\n")
        assert _load_adapters_module(pdir, "p1") == []

    def test_missing_adapters_export_returns_empty(self, tmp_path):
        pdir = _write_plugin(tmp_path, "p1", "X = 1\n")
        assert _load_adapters_module(pdir, "p1") == []

    def test_empty_adapters_list_returns_empty(self, tmp_path):
        pdir = _write_plugin(tmp_path, "p1", "ADAPTERS = []\n")
        assert _load_adapters_module(pdir, "p1") == []

    def test_non_adapter_items_filtered(self, tmp_path):
        pdir = _write_plugin(tmp_path, "p1", "ADAPTERS = [object(), 42]\n")
        assert _load_adapters_module(pdir, "p1") == []

    def test_class_item_instantiated(self, tmp_path):
        """ADAPTERS 导出类（而非实例）时自动实例化。"""
        pdir = _write_plugin(tmp_path, "p1", _GOOD_ADAPTER.replace("ADAPTERS = [A()]", "ADAPTERS = [A]"))
        result = _load_adapters_module(pdir, "p1")
        assert len(result) == 1
        assert isinstance(result[0], ModelFamilyAdapter)


class TestRefreshAndGet:
    def test_disabled_config_failure_defaults_to_empty_list(self, tmp_path):
        _write_plugin(tmp_path, "p1", _GOOD_ADAPTER)
        with patch("app.integration.config_helper.get_disabled_plugins",
                   side_effect=RuntimeError("config broken")):
            n = refresh_plugin_adapters(plugin_dir=tmp_path, disabled=None)
        assert n == 1
        assert get_adapter("qwen-3") is not None

    def test_get_adapter_fallback_when_lazy_load_leaves_registry_none(self, monkeypatch):
        import app.agents.model_family_adapters as mod
        monkeypatch.setattr(mod, "_adapters", None)
        monkeypatch.setattr(mod, "refresh_plugin_adapters", lambda *a, **k: 0)
        assert get_adapter("any-model") is None
        assert mod._adapters == []  # 兜底置空列表，避免反复重扫

    def test_refresh_then_get_no_match(self, tmp_path):
        _write_plugin(tmp_path, "p1", _GOOD_ADAPTER)
        assert refresh_plugin_adapters(plugin_dir=tmp_path, disabled=[]) == 1
        assert get_adapter("glm-4") is None
        assert get_adapter("") is None

    def test_matches_classmethod(self):
        class _M(ModelFamilyAdapter):
            family = "m"
            import re as _re
            _match_re = _re.compile("abc", _re.IGNORECASE)

        assert _M.matches("XABCY") is True
        assert _M.matches("xyz") is False
