"""Dispatcher — 使用 LangGraph ReAct Agent 处理聊天请求。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from langchain_core.messages import HumanMessage

from .langgraph_agent import session_to_langchain_messages, run_agent_streaming, tool_call_signature
from .validator_agent import ValidatorAgent
from ..schema.chat_schema import Dialog, Event, Instruction, Internal, Template, UI
from ..services.priority_service import interactive_priority
from ..services.prompt_service import build_system_prompt
from ..services.session_store import SessionStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 单轮 agent 流式执行的共享状态与事件处理器
# ---------------------------------------------------------------------------

@dataclass
class _StreamRunState:
    """单轮 agent 流式执行的可变状态，REST/WS 共用。

    failed_tools / succeeded_tool_calls / unresolved_failed 配合失败重试回路：
    - failed_tools：本轮执行失败的工具，重试时只补这些；
    - succeeded_tool_calls：本轮已成功的工具调用签名，传给 post_model_hook 在重试轮剔除；
    - unresolved_failed：按工具名记录的、跨重试轮仍存在、最终未成功的工具。
      重试轮若模型空转（hook 剔光、未产出新结果），failed_tools 会被清空，
      此时靠 unresolved_failed 在收尾兜底生成失败说明、并把 Finish 标记为失败。
    """

    final_content: str = ""
    tool_call_count: int = 0
    has_error: bool = False
    has_streamed_tokens: bool = False  # WS 路径：是否已推送过 token
    # run_id -> 该次调用的 args。用 run_id 而非 tool_name 做 key，
    # 同名工具并行调用时不会互相覆盖（on_tool_start/on_tool_end 的 run_id 相同）。
    pending_tool_args: dict[str, dict] = field(default_factory=dict)
    # run_id -> 工具名，tool_end 时据 run_id 取回正确工具名
    pending_tool_names: dict[str, str] = field(default_factory=dict)
    # run_id -> tool_id，保证 tool_start/tool_end 的 id 配对正确（并行工具不串号）
    pending_tool_ids: dict[str, str] = field(default_factory=dict)
    failed_tools: list[dict] = field(default_factory=list)
    succeeded_tool_calls: set[str] = field(default_factory=set)  # 供失败重试轮 post_model_hook 剔除
    # 跨重试轮保留：记录最终仍未成功的工具名，用于收尾兜底
    unresolved_failed: list[str] = field(default_factory=list)

    def reset_for_retry(self) -> None:
        """重试轮开始前清空本轮临时状态，保留 succeeded_tool_calls 与 unresolved_failed。

        unresolved_failed 不在此清空：它追踪的是「这次用户请求中最终没成的工具」，
        重试轮空转（hook 剔光、无新结果）时 failed_tools 会变空，此时收尾要靠它兜底。
        """
        self.final_content = ""
        self.failed_tools = []
        self.pending_tool_args = {}
        self.pending_tool_names = {}
        self.pending_tool_ids = {}


def _make_event_handler(
    state: _StreamRunState,
    emit: Callable[[Instruction], Awaitable[None]],
    request_id: str,
    session_id: str,
    *,
    stream_tokens: bool,
    entity_name_map: dict[str, str] | None = None,
) -> Callable[[dict], Awaitable[None]]:
    """构造统一的流式事件处理器，消除 REST/WS 两路重复的事件分支。

    Args:
        state: 本轮可变状态。
        emit: 把 Instruction 推给调用方（REST 追加列表 / WS 经 ws_send 推送）。
        request_id / session_id: 构造 Instruction 头部用。
        stream_tokens: WS 路径为 True，额外推送 TokenStream 与 executing 状态；
            REST 路径为 False，仅累积 final_content。
        entity_name_map: {entity_id: friendly_name}，供 CallTool 填充设备友好名。
    """

    async def handler(se: dict) -> None:
        event_type = se.get("type")

        if event_type == "token":
            content = se.get("content", "")
            state.final_content += content
            if stream_tokens:
                state.has_streamed_tokens = True
                await emit(
                    Instruction.build_instruction(
                        Template.TokenStream(token=content, is_final=False),
                        request_id, session_id,
                    )
                )

        elif event_type == "tool_start":
            state.tool_call_count += 1
            tool_name = se.get("tool_name", "unknown")
            tool_args = se.get("tool_args", {})
            run_id = se.get("run_id", "") or f"tool-{state.tool_call_count}"
            tool_id = f"tool-{state.tool_call_count}"
            # 用 run_id 关联 start↔end，同名工具并行时不互相覆盖
            state.pending_tool_args[run_id] = tool_args
            state.pending_tool_names[run_id] = tool_name
            state.pending_tool_ids[run_id] = tool_id
            service_name = tool_name.split("___")[0] if "___" in tool_name else "local"
            # call_service 时把 entity_id 翻译成友好名（如 light.bed → 床头灯）
            friendly_name = None
            if entity_name_map and "call_service" in tool_name:
                eid = str(tool_args.get("entity_id", ""))
                if eid:
                    friendly_name = entity_name_map.get(eid)
            if stream_tokens:
                # WS：先推 executing 状态，再推 CallTool
                await emit(
                    Instruction.build_instruction(
                        UI.Status(phase="executing", detail=tool_name),
                        request_id, session_id,
                    )
                )
            await emit(
                Instruction.build_instruction(
                    Template.CallTool(
                        id=tool_id,
                        service_name=service_name,
                        tool_name=tool_name,
                        tool_params=tool_args,
                        friendly_name=friendly_name,
                    ),
                    request_id, session_id,
                )
            )

        elif event_type == "tool_end":
            tool_name = se.get("tool_name", "unknown")
            result = se.get("result", "")
            is_error = se.get("error", False)
            run_id = se.get("run_id", "") or f"tool-{state.tool_call_count}"
            tool_args = state.pending_tool_args.pop(run_id, {})
            state.pending_tool_names.pop(run_id, None)
            tool_id = state.pending_tool_ids.pop(run_id, f"tool-{state.tool_call_count}")
            # 失败入列供失败重试；成功记签名供 post_model_hook 在重试轮剔除
            if is_error:
                state.failed_tools.append({
                    "name": tool_name,
                    "args": tool_args,
                    "result": result,
                })
                # 记录跨轮未解决：重试成功后会在下方移除
                if tool_name not in state.unresolved_failed:
                    state.unresolved_failed.append(tool_name)
            else:
                state.succeeded_tool_calls.add(tool_call_signature(tool_name, tool_args))
                # 本轮成功：若它之前在 unresolved 列表里，移除（重试成功兑现）
                if tool_name in state.unresolved_failed:
                    state.unresolved_failed.remove(tool_name)
            await emit(
                Instruction.build_instruction(
                    Template.CallToolResult(
                        id=tool_id,
                        success=not is_error,
                        tool_name=tool_name,
                        tool_response={"result": result} if not is_error else None,
                        error_message=result if is_error else None,
                    ),
                    request_id, session_id,
                )
            )

        elif event_type == "error":
            error_msg = se.get("message", "Unknown error")
            await emit(
                Instruction.build_instruction(
                    Dialog.Exception(message=error_msg),
                    request_id, session_id,
                )
            )
            if stream_tokens:
                # WS：标记错误，最终 ToastStream 跳过（Dialog.Exception 已展示）
                state.has_error = True
            else:
                # REST：写入 final_content，最终 ToastStream 展示错误
                state.final_content = f"抱歉，处理出错：{error_msg}"

    return handler


class Dispatcher:
    def __init__(
        self,
        session_store: SessionStore,
        agent: Any,  # LangGraph CompiledStateGraph
        camera_stream: Any,
        ha_catalog_provider: Any = None,
        ha_controls_provider: Any = None,
        vision_service: Any = None,
        ha_service: Any = None,
        validator: ValidatorAgent | None = None,
        summarization_service: Any = None,
    ) -> None:
        self._session_store = session_store
        self._agent = agent
        self._camera_stream = camera_stream
        self._ha_catalog_provider = ha_catalog_provider
        self._ha_controls_provider = ha_controls_provider
        self._vision_service = vision_service
        self._ha_service = ha_service
        self._validator = validator or ValidatorAgent()
        self._summarization_service = summarization_service
        # 失败重试上限：与 validator 的 _max_retries 对齐，避免死循环
        self._max_failure_retries = 1

    @staticmethod
    def _build_failure_retry_message(failed_tools: list[dict]) -> HumanMessage:
        """构建"只重试失败工具"的提示消息。

        语气说明：只陈述事实（哪些工具失败、错误是什么），不强调"如实告知失败"，
        避免 LLM 把"报告失败"当成主线任务而放弃总结已成功的结果。
        Args:
            failed_tools: [{"name": str, "args": dict, "result": str}, ...]
        """
        import json
        lines = ["刚才部分工具调用失败，以下是失败信息："]
        for ft in failed_tools:
            args_str = json.dumps(ft.get("args", {}), ensure_ascii=False, default=str)
            lines.append(f"- 工具 {ft['name']}，参数 {args_str}，错误：{ft.get('result', '')}")
        lines.append(
            "请检查失败原因后重试对应的工具。如果该工具名不存在或设备确实不可用，"
            "换用正确的工具（如查状态用 get_entities）。"
            "同时请正常总结本次对话中已完成的操作，给用户一个完整回复。"
        )
        return HumanMessage(content="\n".join(lines))

    def set_agent(self, agent: Any) -> None:
        """运行时替换 Agent 实例（MCP 工具变更后调用）。"""
        self._agent = agent

    async def _prepare_context(self, session, query: str) -> dict[str, Any]:
        """共享的准备逻辑：构建 agent 运行所需的全部上下文。

        Returns:
            dict with keys: session, query, device_catalog, device_controls,
            vision_focuses, system_prompt, lc_messages
        """
        # 获取 HA 设备目录
        device_catalog = None
        device_controls = None
        if self._ha_catalog_provider is not None:
            try:
                device_catalog = self._ha_catalog_provider()
            except Exception:
                logger.exception("Failed to build HA device catalog")
        if self._ha_controls_provider is not None:
            try:
                device_controls = self._ha_controls_provider()
            except Exception:
                logger.exception("Failed to build HA device controls")

        # 获取视觉关注重点 (focus)
        vision_focuses = None
        if self._vision_service is not None:
            try:
                vision_focuses = self._vision_service.get_vision_focuses()
            except Exception:
                logger.exception("Failed to get vision focuses")

        # 自动压缩过期对话，生成摘要
        if self._summarization_service is not None:
            try:
                await self._summarization_service.refresh_summaries(session)
            except Exception:
                logger.exception("Failed to refresh summaries")

        # 构建 system prompt
        try:
            system_prompt = await build_system_prompt(
                visual_summary=session.latest_visual_state,
                device_catalog=device_catalog,
                device_controls=device_controls,
                vision_focuses=vision_focuses,
                query=query,
                summaries=session.summaries,
            )
        except Exception:
            logger.exception("Failed to build system prompt, using minimal fallback")
            # 降级：使用最小化的 system prompt，保证聊天仍可用
            system_prompt = "你是 Aether 家庭智能助手。请尽力回答用户问题。"

        # 构建 LangChain 消息列表
        lc_messages = session_to_langchain_messages(session, system_prompt=system_prompt)
        lc_messages.append(HumanMessage(content=query))

        return {
            "session": session,
            "query": query,
            "device_catalog": device_catalog,
            "device_controls": device_controls,
            "vision_focuses": vision_focuses,
            "system_prompt": system_prompt,
            "lc_messages": lc_messages,
        }

    # ------------------------------------------------------------------
    # 入口：REST（非流式）/ WS（流式）
    # ------------------------------------------------------------------

    async def dispatch(self, event: Event, user_id: str = "") -> list[Instruction]:
        """处理聊天事件，返回 instruction 列表（非流式，兼容 REST 回退）。"""
        session = await self._session_store.get_or_create(event.header.session_id, event.header.request_id, user_id=user_id)
        session.latest_visual_state = self._camera_stream.get_state()
        session.history_events.append(event)
        query = event.payload.get("query", "")
        session.current_query = query

        try:
            ctx = await self._prepare_context(session, query)
        except Exception as e:
            logger.exception("dispatch: _prepare_context failed")
            return [
                Instruction.build_instruction(
                    Dialog.Exception(message=f"准备上下文失败: {e}"),
                    event.header.request_id, event.header.session_id,
                ),
                Instruction.build_instruction(
                    Dialog.Finish(success=False),
                    event.header.request_id, event.header.session_id,
                ),
            ]

        instructions: list[Instruction] = []

        async def emit(instruction: Instruction) -> None:
            instructions.append(instruction)

        await self._run_turn(event, session, query, ctx, emit, stream_tokens=False)

        session.history_instructions.extend(instructions)
        await self._session_store.store_session(session)
        return instructions

    async def dispatch_stream(self, event: Event, ws_send, user_id: str = ""):
        """处理聊天事件，通过 WebSocket 流式推送 instruction。

        Args:
            event: 聊天事件
            ws_send: WebSocket 发送函数 (async def send(data))
            user_id: 当前用户 ID，用于会话隔离
        """
        session = await self._session_store.get_or_create(
            event.header.session_id, event.header.request_id, user_id=user_id,
        )
        session.latest_visual_state = self._camera_stream.get_state()
        session.history_events.append(event)
        query = event.payload.get("query", "")
        session.current_query = query

        try:
            ctx = await self._prepare_context(session, query)
        except Exception as e:
            logger.exception("dispatch_stream: _prepare_context failed")
            try:
                await ws_send(
                    Instruction.build_instruction(
                        Dialog.Exception(message=f"准备上下文失败: {e}"),
                        event.header.request_id, event.header.session_id,
                    ).model_dump()
                )
                await ws_send(
                    Instruction.build_instruction(
                        Dialog.Finish(success=False),
                        event.header.request_id, event.header.session_id,
                    ).model_dump()
                )
            except Exception:
                logger.exception("dispatch_stream: failed to send error to ws")
            return

        async def emit(instruction: Instruction) -> None:
            await ws_send(instruction.model_dump())

        await self._run_turn(event, session, query, ctx, emit, stream_tokens=True)

        session.history_instructions = []  # 流式模式不存 history_instructions
        await self._session_store.store_session(session)

    # ------------------------------------------------------------------
    # 共享编排骨架
    # ------------------------------------------------------------------

    async def _run_turn(
        self,
        event: Event,
        session,
        query: str,
        ctx: dict[str, Any],
        emit: Callable[[Instruction], Awaitable[None]],
        *,
        stream_tokens: bool,
    ) -> None:
        """单轮 agent 执行的共享编排骨架，REST/WS 共用。

        覆盖：Dispatcher 信号 → thinking 状态 → agent 流式 → 失败重试 →
        Validator 兜底 → 静默收尾兜底 → 最终回复 → session 更新 → Finish。

        两路差异通过 stream_tokens 分支保留，不改语义：
        - WS(stream_tokens=True)：逐 token 推送、发 thinking/executing/finalizing
          状态、收尾发 TokenStream(is_final) 复位前端流式索引、出错设 has_error。
        - REST(stream_tokens=False)：只累积 final_content、不发阶段状态、出错写入
          final_content 由 ToastStream 展示、has_error 恒为 False。
        """
        request_id = event.header.request_id
        session_id = event.header.session_id
        lc_messages = ctx["lc_messages"]
        path = "WS" if stream_tokens else "REST"

        logger.info("System prompt length: %d chars, device_catalog: %s",
                    len(ctx["system_prompt"]),
                    "present" if ctx["device_catalog"] else "empty")

        # Dispatcher 信号
        await emit(
            Instruction.build_instruction(
                Internal.Dispatcher(current_query=query, need_storage_history=True),
                request_id, session_id,
            )
        )
        # thinking 状态：仅 WS
        if stream_tokens:
            await emit(
                Instruction.build_instruction(
                    UI.Status(phase="thinking"), request_id, session_id,
                )
            )

        state = _StreamRunState()

        # 设备友好名映射（容错：HA 不可用时用空 dict，不阻塞对话）
        entity_name_map: dict[str, str] = {}
        if self._ha_service:
            try:
                entity_name_map = await self._ha_service.get_entity_name_map()
            except Exception:
                logger.debug("Failed to get entity_name_map [%s]", path, exc_info=True)

        handler = _make_event_handler(
            state, emit, request_id, session_id,
            stream_tokens=stream_tokens,
            entity_name_map=entity_name_map,
        )

        # 主轮：运行 LangGraph agent，收集流式事件
        with interactive_priority.hold():
            try:
                async for stream_event in run_agent_streaming(self._agent, lc_messages, session):
                    await handler(stream_event)
            except Exception as e:
                # WS：emit 可能因连接断开抛错，兜底设 has_error 并尝试发 Dialog.Exception。
                # REST：emit=append 不会抛，run_agent_streaming 内部已吞异常 yield error，
                # 此分支对 REST 是死代码，不改变其行为。
                logger.exception("_run_turn: agent error [%s]", path)
                state.has_error = True
                from .langgraph_agent import _friendly_api_error
                error_msg = _friendly_api_error(e)
                try:
                    await emit(
                        Instruction.build_instruction(
                            Dialog.Exception(message=error_msg),
                            request_id, session_id,
                        )
                    )
                except Exception:
                    logger.exception("_run_turn: failed to send error [%s]", path)

        # 失败重试：调过工具但有失败项时，追加精准提示再跑一轮，只补失败的。
        # succeeded_tool_calls 传给 post_model_hook，代码层面剔除已成功的 tool_call。
        # has_error 对 REST 恒为 False，条件与原 REST 实现等价。
        failure_retry_count = 0
        while state.failed_tools and not state.has_error and failure_retry_count < self._max_failure_retries:
            failure_retry_count += 1
            logger.info("Failure retry (%d/%d) [%s]: %d tools failed",
                        failure_retry_count, self._max_failure_retries, path, len(state.failed_tools))
            # 告知用户部分操作失败正在重试
            await emit(
                Instruction.build_instruction(
                    UI.Status(phase="retrying", detail="部分操作失败，正在重试"),
                    request_id, session_id,
                )
            )
            lc_messages.append(self._build_failure_retry_message(state.failed_tools))
            state.reset_for_retry()
            async for stream_event in run_agent_streaming(
                self._agent, lc_messages, session,
                succeeded_tool_calls=state.succeeded_tool_calls,
            ):
                await handler(stream_event)

        # Validator 校验：仅当模型完全没调工具时才触发重试（兜底安全网）
        # has_error 对 REST 恒为 False，tool_call_count==0 短路与原 REST 的 if 守卫等价。
        retry_count = 0
        while (state.tool_call_count == 0 and not state.has_error
               and await self._validator.should_retry(state.final_content, state.tool_call_count)
               and retry_count < self._validator._max_retries):
            retry_count += 1
            logger.info("Validator: auto-retry (%d/%d) [%s]", retry_count, self._validator._max_retries, path)
            # retrying 状态：仅 WS（REST 原实现不发）
            if stream_tokens:
                await emit(
                    Instruction.build_instruction(
                        UI.Status(phase="retrying"), request_id, session_id,
                    )
                )
            lc_messages.append(self._validator.build_retry_message())
            state.final_content = ""
            async for stream_event in run_agent_streaming(self._agent, lc_messages, session):
                await handler(stream_event)

        # 静默收尾兜底：重试轮空转（hook 剔光、模型无文本产出）时 final_content 为空，
        # 但仍有未解决的工具失败。强制生成失败说明，避免「失败却 Finish(success=True)」。
        # has_error 对 REST 恒为 False，条件与原 REST 实现等价。
        if not state.final_content and state.unresolved_failed and not state.has_error:
            names = "、".join(state.unresolved_failed)
            state.final_content = f"部分操作未能完成（{names}），请稍后重试或检查设备状态。"
            logger.info("Silent-failure fallback [%s]: %s", path, state.unresolved_failed)
            if stream_tokens:
                # WS：此时通常无流式 token，需直接发 ToastStream + is_final 复位前端索引
                await emit(
                    Instruction.build_instruction(
                        Template.ToastStream(stream=state.final_content),
                        request_id, session_id,
                    )
                )
                await emit(
                    Instruction.build_instruction(
                        Template.TokenStream(token="", is_final=True),
                        request_id, session_id,
                    )
                )
            # REST：只设 final_content，由下方收尾统一发 ToastStream

        # 最终回复
        if stream_tokens:
            # WS：只有当流式 token 已输出时才发 ToastStream+is_final，
            # 否则 Dialog.Exception 已经处理了错误显示，避免重复。
            if state.final_content and state.has_streamed_tokens:
                await emit(
                    Instruction.build_instruction(
                        UI.Status(phase="finalizing"), request_id, session_id,
                    )
                )
                # ToastStream 到达时 streamingMessageIndex >= 0，前端会跳过它避免重复
                await emit(
                    Instruction.build_instruction(
                        Template.ToastStream(stream=state.final_content),
                        request_id, session_id,
                    )
                )
                # is_final 重置前端流式索引
                await emit(
                    Instruction.build_instruction(
                        Template.TokenStream(token="", is_final=True),
                        request_id, session_id,
                    )
                )
        else:
            # REST：有 final_content 就发 ToastStream（含静默兜底设入的失败说明）
            if state.final_content:
                await emit(
                    Instruction.build_instruction(
                        Template.ToastStream(stream=state.final_content),
                        request_id, session_id,
                    )
                )

        # 更新 session
        session.model_messages.append({"role": "user", "content": query})
        if state.final_content:
            session.model_messages.append({"role": "assistant", "content": state.final_content})

        # Finish 反映真实状态：仍有未解决失败或执行出错时标记失败
        finish_success = not state.unresolved_failed and not state.has_error
        await emit(
            Instruction.build_instruction(
                Dialog.Finish(success=finish_success),
                request_id, session_id,
            )
        )
