"""Dispatcher — 使用 LangGraph ReAct Agent 处理聊天请求。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from .langgraph_agent import session_to_langchain_messages, run_agent_streaming, tool_call_signature, load_model_config_for_user, build_chat_agent
from .model_family_adapters import get_adapter
from .validator_agent import ValidatorAgent
from ..schema.chat_schema import Dialog, Event, Instruction, Template, UI
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
        ha_catalog_provider: Any = None,
        ha_controls_provider: Any = None,
        catalog_refresh_fn: Any = None,  # controls 缓存空时同步刷新（确保备注不缺位）
        clients: tuple[Any, Any] | None = None,  # 全局 agent 的 (sync, async) httpx 客户端
        vision_service: Any = None,
        ha_service: Any = None,
        validator: ValidatorAgent | None = None,
        summarization_service: Any = None,
        camera_manager: Any = None,
        sink_manager: Any = None,
    ) -> None:
        self._session_store = session_store
        self._agent = agent
        self._tools: list = []           # 工具列表（构建 per-user agent 用）
        self._user_agents: dict[str, Any] = {}  # user_id → agent 缓存
        self._user_agent_lock = asyncio.Lock()
        # 后台小任务引用（如 broadcasting 状态清除），防事件循环只持弱引用被 GC
        self._bg_tasks: set[asyncio.Task] = set()
        # 多路。取 focus/state 时按主摄像头(第一个 enabled)。
        self._camera_manager = camera_manager
        self._ha_catalog_provider = ha_catalog_provider
        self._ha_controls_provider = ha_controls_provider
        self._catalog_refresh_fn = catalog_refresh_fn
        self._vision_service = vision_service
        self._ha_service = ha_service
        self._validator = validator or ValidatorAgent()
        self._summarization_service = summarization_service
        # 集成广播钩子：assistant final_content 产出后广播到 output_sink（如小爱）
        self._sink_manager = sink_manager
        # 失败重试上限：与 validator 的 _max_retries 对齐，避免死循环
        self._max_failure_retries = 1
        # agent → 它的 (sync, async) httpx 客户端 映射。
        # langgraph agent 不透明，无法反查内部客户端，故由 dispatcher 显式登记。
        # agent 失效（重建/换 key/退出）时只关它自己的客户端，不再全局清空——
        # 避免 per-user agent 构建误关全局 agent 连接。
        self._agent_clients: dict[int, tuple[Any, Any]] = {}
        if agent is not None and clients is not None:
            self._agent_clients[id(agent)] = clients

    def _primary_camera_id(self) -> str:
        """Task 9:取主摄像头 id。

        与 CameraManager.primary_camera_id 对齐(预览路优先,否则第一个
        enabled)——旧实现永远取第一个 enabled,用户把 AI 预览切到别路后,
        聊天侧摄像头状态/关注项注入仍指旧路,与 vision_chat 的取帧口径不一致。
        """
        if self._camera_manager is None:
            return ""
        getter = getattr(self._camera_manager, "primary_camera_id", None)
        if callable(getter):
            return getter() or ""
        # duck-type 兜底:测试桩/旧 mock 没有 primary_camera_id 时保持旧行为
        cams = self._camera_manager.list_cameras()
        return cams[0]["id"] if cams else ""

    # 空摄像头状态（manager 未注入/无路时兜底）。与 CameraStateModel 字段对齐，
    # main._primary_camera_state 用同一份，加字段改一处即可。
    EMPTY_CAMERA_STATE: dict = {
        "camera_id": "", "camera_opened": False, "backend_name": "unknown",
        "frame_width": 0, "frame_height": 0, "fps": 0.0, "last_frame_at": 0.0,
        "last_error": None, "action": "idle", "feedback": "", "details": None,
    }

    def set_ha_service(self, svc) -> None:
        """HA 配置热替换后重绑（main.sync_ha_runtime_refs 调用）。"""
        self._ha_service = svc

    def _get_camera_state(self) -> dict:
        """取主摄像头状态。camera_manager 为空或无路时返回完整空状态字典。"""
        if self._camera_manager is None:
            return dict(self.EMPTY_CAMERA_STATE)
        cid = self._primary_camera_id()
        if cid:
            return self._camera_manager.get_state(cid)
        return dict(self.EMPTY_CAMERA_STATE)

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

    async def set_agent(self, agent: Any, tools: list | None = None,
                        clients: tuple[Any, Any] | None = None) -> None:
        """运行时替换全局 Agent 实例（MCP 工具变更后调用）。

        同时更新工具列表并清空 per-user agent 缓存，下次各用户聊天时按新工具重建。
        旧全局 agent 和所有 per-user agent 的 httpx 客户端在此一并关闭回收。
        """
        # 先关闭被取代的旧客户端（全局 + 所有 per-user），再清缓存换新。
        await self.close_all_agent_clients()
        self._agent = agent
        if tools is not None:
            self._tools = tools
        self._user_agents.clear()
        if agent is not None and clients is not None:
            self._agent_clients[id(agent)] = clients

    async def invalidate_user_agent(self, user_id: str) -> None:
        """用户修改 chat key 绑定后清除其 agent 缓存，下次聊天用新 key 重建。

        清缓存前先关闭该 agent 的 httpx 客户端，回收连接池。
        同时清 validator 的 per-user LLM 缓存，避免 validator 用旧 key 请求。
        """
        old = self._user_agents.pop(user_id, None)
        if old is not None:
            await self._close_agent_clients(old)
        self._validator.invalidate_user(user_id)

    async def _close_agent_clients(self, agent: Any) -> None:
        """关闭单个 agent 绑定的 httpx 客户端，从映射移除。agent 未登记则 no-op。"""
        clients = self._agent_clients.pop(id(agent), None)
        if clients is None:
            return
        sync_client, async_client = clients
        try:
            sync_client.close()
        except Exception:  # noqa: BLE001
            logger.debug("close sync httpx client failed", exc_info=True)
        try:
            await async_client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("close async httpx client failed", exc_info=True)

    async def close_all_agent_clients(self) -> None:
        """关闭所有已登记的 agent 客户端（全局 + per-user），清空映射。

        供 set_agent（换全局 agent 前清理）和 lifespan 关闭收尾调用。
        """
        agents = list(self._user_agents.values()) + [self._agent]
        for agent in agents:
            if agent is not None:
                await self._close_agent_clients(agent)

    async def _get_agent(self, user_id: str) -> Any:
        """按 user_id 获取 agent。用户有独立 key 配置时返回 per-user agent，
        无配置或无 user_id 时回退全局 self._agent。"""
        if not user_id:
            return self._agent
        if user_id in self._user_agents:
            return self._user_agents[user_id]
        model_config = await load_model_config_for_user(user_id)
        if not model_config:
            return self._agent  # 用户无独立配置，回退全局
        async with self._user_agent_lock:
            # double-check：持锁后可能已被其他协程构建
            if user_id in self._user_agents:
                return self._user_agents[user_id]
            try:
                agent, clients = build_chat_agent(self._tools, model_config=model_config)
                self._user_agents[user_id] = agent
                self._agent_clients[id(agent)] = clients
                logger.info("Built per-user agent for user_id=%s, model=%s",
                            user_id, model_config.get("model"))
                return agent
            except Exception:
                logger.exception("Failed to build per-user agent for %s, falling back to global", user_id)
                return self._agent

    async def _emit_turn_error(
        self, exc: Exception, state: _StreamRunState,
        emit: Callable[[Instruction], Awaitable[None]],
        request_id: str, session_id: str, path: str,
    ) -> None:
        """agent 执行异常的统一兜底：置 has_error 并尽力发 Dialog.Exception。

        主轮与两个重试轮共用。WS 下 emit 可能因连接断开再抛错（内层 try 兜住）；
        REST 下 emit=append 不会抛。置 has_error=True 后各 while 循环条件
        (not state.has_error) 失效，自然退出并走到收尾发 Finish(success=False)，
        避免异常逃出 _run_turn 导致客户端收不到 Finish 永久挂起。
        """
        logger.exception("_run_turn: agent error [%s]", path)
        state.has_error = True
        from .langgraph_agent import _friendly_api_error
        error_msg = _friendly_api_error(exc)
        try:
            await emit(
                Instruction.build_instruction(
                    Dialog.Exception(message=error_msg),
                    request_id, session_id,
                )
            )
        except Exception:
            logger.exception("_run_turn: failed to send error [%s]", path)

    async def _handle_cancelled(self, emit, request_id: str, session_id: str) -> None:
        """处理被打断：emit Finish(success=False) + 停所有 sink。吞掉 CancelledError。

        task.cancel() 触发的 CancelledError 是 BaseException，不会被 except Exception
        捕获，会逃逸出 _run_turn 导致客户端收不到 Finish 永久挂起。此处统一兜底。
        """
        await emit(
            Instruction.build_instruction(
                Dialog.Finish(success=False), request_id, session_id,
            )
        )
        if self._sink_manager is not None:
            try:
                await self._sink_manager.interrupt_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("打断时停 sink 失败（不影响）: %s", exc)
        logger.info("Turn %s 被用户打断", request_id)

    async def _clear_broadcasting_after(self, emit, request_id: str,
                                        session_id: str, delay: float) -> None:
        """延迟清除 broadcasting status（估算播报完毕后发送按钮恢复发送态）。

        HA 不暴露小爱播报状态，用超时估算。不精确但够用——用户也可在超时前点 ■
        打断（interrupt 会清前端 statusPhase）。被 cancel / 连接断开时静默退出。
        """
        try:
            await asyncio.sleep(delay)
            await emit(
                Instruction.build_instruction(
                    UI.Status(phase=""), request_id, session_id,
                )
            )
        except Exception:  # noqa: BLE001
            pass  # 连接已断 / task 被 cancel，忽略

    async def _prepare_context(self, session, query: str, user_id: str = "") -> dict[str, Any]:
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
                # controls 为空（后台刷新循环启动时序失败/未就绪）时，
                # 退回 catalog 会丢失备注→LLM 按直觉调错 service。
                # 同步触发一次刷新，确保备注/controls 不缺位。
                if not device_controls and self._catalog_refresh_fn is not None:
                    try:
                        await self._catalog_refresh_fn()
                        device_controls = self._ha_controls_provider() or ""
                    except Exception:
                        logger.warning("On-demand catalog refresh failed", exc_info=True)
            except Exception:
                logger.exception("Failed to build HA device controls")

        # 获取视觉关注重点 (focus) —— Task 9:按主摄像头取 per-camera focus
        vision_focuses = None
        if self._vision_service is not None:
            try:
                vision_focuses = self._vision_service.get_vision_focuses(self._primary_camera_id())
            except Exception:
                logger.exception("Failed to get vision focuses")

        # 自动压缩过期对话，生成摘要
        if self._summarization_service is not None:
            try:
                await self._summarization_service.refresh_summaries(session, user_id=user_id)
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

        # 模型家族适配节点（通用）：家族行为由 model_adapter 能力插件提供
        # （integrations/<id>/adapters.py），宿主只查注册表，零家族特判。
        # 命中当前聊天模型时让适配器改写本轮消息（如混合思考模型
        # 注入思考软开关降首字延迟）。
        chat_model = await self._current_chat_model(user_id)
        self._inject_family_switch(lc_messages, chat_model)

        return {
            "session": session,
            "query": query,
            "device_catalog": device_catalog,
            "device_controls": device_controls,
            "vision_focuses": vision_focuses,
            "system_prompt": system_prompt,
            "lc_messages": lc_messages,
            "chat_model": chat_model,
        }

    async def _current_chat_model(self, user_id: str) -> str:
        """解析本轮实际使用的聊天模型名（per-user 配置优先，回退全局）。

        模型家族适配节点查询用；失败返回空串（等价无适配器，不影响主流程）。
        """
        if user_id:
            try:
                cfg = await load_model_config_for_user(user_id)
                if cfg:
                    return str(cfg.get("model", ""))
            except Exception:
                logger.warning("per-user chat 模型解析失败，回退全局", exc_info=True)
        from .langgraph_agent import _load_model_config_from_config
        try:
            return str(_load_model_config_from_config().get("model", ""))
        except Exception:
            logger.debug("全局 chat 模型解析失败（可能未配置 chat key）")
            return ""

    def _inject_family_switch(self, lc_messages: list, model_name: str,
                              include_system: bool = True) -> None:
        """模型家族适配节点：让命中当前模型的适配器改写本轮消息。

        适配器来自 model_adapter 能力插件（integrations/ 下有示例），
        此处只做通用查表调用，不含任何模型家族特判——具体注入什么
        开关标记、匹配哪些模型名，全部由插件自己决定。典型用途：
        混合思考模型在闲聊/控制场景关闭思考降首字延迟（聊天模板以
        最后一条消息的开关为准，故主轮注入 system+user，重试轮只补
        最后一条（include_system=False，避免 system 上开关标记叠加））。

        只改发送给模型的副本，不回写 session 历史，避免开关标记
        污染对话记录。无适配器/异常静默跳过，不影响主流程。
        """
        if not model_name or not lc_messages:
            return
        try:
            adapter = get_adapter(model_name)
        except Exception:
            logger.warning("模型家族适配器查询失败，跳过本轮注入", exc_info=True)
            return
        if adapter is None:
            return
        last = lc_messages[-1]
        if not isinstance(last, HumanMessage):
            return
        has_system = isinstance(lc_messages[0], SystemMessage)
        system_text = str(lc_messages[0].content) if has_system else ""
        new_system, new_user = adapter.no_think(
            system_text if include_system else "", str(last.content),
        )
        if include_system and has_system:
            lc_messages[0] = SystemMessage(content=new_system)
        lc_messages[-1] = HumanMessage(content=new_user)

    # ------------------------------------------------------------------
    # 入口：REST（非流式）/ WS（流式）
    # ------------------------------------------------------------------

    async def dispatch(self, event: Event, user_id: str = "") -> list[Instruction]:
        """处理聊天事件，返回 instruction 列表（非流式，兼容 REST 回退）。"""
        session = await self._session_store.get_or_create(event.header.session_id, event.header.request_id, user_id=user_id)
        session.latest_visual_state = self._get_camera_state()
        session.history_events.append(event)
        query = event.payload.get("query", "")
        session.current_query = query

        try:
            ctx = await self._prepare_context(session, query, user_id=user_id)
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

        agent = await self._get_agent(user_id)
        await self._run_turn(event, session, query, ctx, emit, stream_tokens=False, agent=agent, user_id=user_id)

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
        session.latest_visual_state = self._get_camera_state()
        session.history_events.append(event)
        query = event.payload.get("query", "")
        session.current_query = query

        try:
            ctx = await self._prepare_context(session, query, user_id=user_id)
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

        ws_broken = False

        async def emit(instruction: Instruction) -> None:
            """WS 发送，断连后自动静音。

            客户端中途断开（手机锁屏/切页）时 ws_send 会抛异常；若让它逃逸，
            _run_turn 的收尾（model_messages 落库、Finish）全部跳过，本轮问答
            永久丢失且日志无痕。这里失败一次即标记断连，后续 emit 静默 no-op，
            让本轮继续跑完并持久化——用户重连后能看到完整历史。
            """
            nonlocal ws_broken
            if ws_broken:
                return
            try:
                await ws_send(instruction.model_dump())
            except Exception:  # noqa: BLE001
                ws_broken = True
                logger.info("dispatch_stream: ws send failed, muting further emits "
                            "(turn continues for persistence)")

        agent = await self._get_agent(user_id)
        try:
            await self._run_turn(event, session, query, ctx, emit, stream_tokens=True, agent=agent, user_id=user_id)
        finally:
            # 无论本轮如何收场（含意外异常），都把会话状态落库
            session.history_instructions = []  # 流式模式不存 history_instructions
            try:
                await self._session_store.store_session(session)
            except Exception:  # noqa: BLE001
                logger.exception("dispatch_stream: store_session failed after turn")

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
        agent: Any = None,
        user_id: str = "",
    ) -> None:
        """单轮 agent 执行的共享编排骨架，REST/WS 共用。

        agent 参数由调用方按 user_id 解析后传入；未传时回退 self._agent。
        user_id 透传给 validator，使其按 per-user chat key 校验（与主对话同模型）。

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

        if agent is None:
            agent = self._agent

        logger.info("System prompt length: %d chars, device_catalog: %s",
                    len(ctx["system_prompt"]),
                    "present" if ctx["device_catalog"] else "empty")

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
                async for stream_event in run_agent_streaming(agent, lc_messages, session):
                    await handler(stream_event)
            except asyncio.CancelledError:
                await self._handle_cancelled(emit, request_id, session_id)
                return
            except Exception as e:
                # WS：emit 可能因连接断开抛错，兜底设 has_error 并尝试发 Dialog.Exception。
                # REST：emit=append 不会抛，run_agent_streaming 内部已吞异常 yield error，
                # 此分支对 REST 是死代码，不改变其行为。
                await self._emit_turn_error(e, state, emit, request_id, session_id, path)

        # 失败重试：调过工具但有失败项时，追加精准提示再跑一轮，只补失败的。
        # succeeded_tool_calls 传给 post_model_hook，代码层面剔除已成功的 tool_call。
        # has_error 对 REST 恒为 False，条件与原 REST 实现等价。
        failure_retry_count = 0
        while state.failed_tools and not state.has_error and failure_retry_count < self._max_failure_retries:
            failure_retry_count += 1
            logger.info("Failure retry (%d/%d) [%s]: %d tools failed",
                        failure_retry_count, self._max_failure_retries, path, len(state.failed_tools))
            # 告知用户部分操作失败正在重试（REST 也发：test_dispatcher 锁定此行为；
            # 与 validator 重试轮"仅 WS"不同，属历史决定，见讨论项）
            await emit(
                Instruction.build_instruction(
                    UI.Status(phase="retrying", detail="部分操作失败，正在重试"),
                    request_id, session_id,
                )
            )
            lc_messages.append(self._build_failure_retry_message(state.failed_tools))
            # 重试轮补注入家族开关（只补最后一条，system 首轮已注入）
            self._inject_family_switch(lc_messages, ctx.get("chat_model", ""),
                                       include_system=False)
            state.reset_for_retry()
            try:
                async for stream_event in run_agent_streaming(
                    agent, lc_messages, session,
                    succeeded_tool_calls=state.succeeded_tool_calls,
                ):
                    await handler(stream_event)
            except asyncio.CancelledError:
                await self._handle_cancelled(emit, request_id, session_id)
                return
            except Exception as e:
                # 重试轮异常兜底：置 has_error 让 while 退出，收尾发 Finish(success=False)，
                # 避免异常逃出 _run_turn 导致客户端收不到 Finish。
                await self._emit_turn_error(e, state, emit, request_id, session_id, path)

        # Validator 校验：仅当模型完全没调工具时才触发重试（兜底安全网）
        # has_error 对 REST 恒为 False，tool_call_count==0 短路与原 REST 的 if 守卫等价。
        retry_count = 0
        while (state.tool_call_count == 0 and not state.has_error
               and await self._validator.should_retry(state.final_content, state.tool_call_count, user_id=user_id)
               and retry_count < self._validator.max_retries):
            retry_count += 1
            logger.info("Validator: auto-retry (%d/%d) [%s]", retry_count, self._validator.max_retries, path)
            # retrying 状态：与失败重试轮一致，REST 也发（统一策略）
            await emit(
                Instruction.build_instruction(
                    UI.Status(phase="retrying"), request_id, session_id,
                )
            )
            lc_messages.append(self._validator.build_retry_message())
            # Validator 重试轮同样补注入家族开关
            self._inject_family_switch(lc_messages, ctx.get("chat_model", ""),
                                       include_system=False)
            state.final_content = ""
            try:
                async for stream_event in run_agent_streaming(agent, lc_messages, session):
                    await handler(stream_event)
            except asyncio.CancelledError:
                await self._handle_cancelled(emit, request_id, session_id)
                return
            except Exception as e:
                # Validator 重试轮异常兜底：同上，置 has_error 退出 while，收尾发 Finish(success=False)
                await self._emit_turn_error(e, state, emit, request_id, session_id, path)

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

        # ── 集成广播钩子：把最终回复同步到 output_sink（如小爱）──
        # 失败不阻塞主流程，仅记录警告（用户已通过 WS 收到文字回复）。
        if state.final_content and self._sink_manager is not None:
            try:
                # emit broadcasting status：让前端发送按钮保持停止态（可打断小爱播报）
                await emit(
                    Instruction.build_instruction(
                        UI.Status(phase="broadcasting"), request_id, session_id,
                    )
                )
                await self._sink_manager.broadcast(state.final_content, request_id)
                # 估算超时后清除 broadcasting（中文约 4 字/秒 + 5 秒缓冲）
                est_seconds = max(len(state.final_content) / 4, 3) + 5
                _t = asyncio.create_task(self._clear_broadcasting_after(
                    emit, request_id, session_id, est_seconds))
                self._bg_tasks.add(_t)
                _t.add_done_callback(self._bg_tasks.discard)
            except Exception as exc:
                logger.warning("集成广播失败（不影响主流程）: %s", exc)

        # Finish 反映真实状态：仍有未解决失败或执行出错时标记失败
        finish_success = not state.unresolved_failed and not state.has_error
        await emit(
            Instruction.build_instruction(
                Dialog.Finish(success=finish_success),
                request_id, session_id,
            )
        )
