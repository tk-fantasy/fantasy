"""LangGraph ReAct Agent — 替代原 IntentService→ExecuteAgent→ChatService 三步流水线。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from ..core.config import get_config
from ..core.key_resolver import resolve_key_for_role
from ..services.prompt_service import build_system_prompt
from ..services.session_store import SessionState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 消息格式转换
# ---------------------------------------------------------------------------

def session_to_langchain_messages(
    session: SessionState,
    system_prompt: str | None = None,
) -> list[BaseMessage]:
    """将 session.model_messages (OpenAI dict 格式) 转为 LangChain Message 列表。

    如果提供 system_prompt，会在列表开头插入 SystemMessage。
    """
    messages: list[BaseMessage] = []

    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))

    for msg in session.model_messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
        # 注意：session 中不存储 tool messages（工具结果已内联到 assistant 回复中）

    return messages


# ---------------------------------------------------------------------------
# Agent 构建
# ---------------------------------------------------------------------------

def tool_call_signature(name: str, args: dict) -> str:
    """生成工具调用签名（name + 规范化 args），用于跨轮匹配已成功的调用。

    用 (name, args) 而非仅 name，避免同工具不同参数（如开灯 vs 设空调温度）
    在失败重试轮被一并剔除。
    """
    return f"{name}::{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


def make_post_model_hook():
    """创建 post_model_hook：失败重试轮强制只调失败的工具，剔除已成功的。

    机制：hook 在 LLM 产出 AIMessage 后、ToolNode 执行前运行。
    从 config["configurable"]["succeeded_tool_calls"] 读上一轮已成功的工具调用签名集合，
    把本轮 AIMessage 中签名命中该集合的 tool_call 剔除。
    返回复用原 AIMessage id 的新 AIMessage（add_messages reducer 据此替换原消息），
    使内置 post_model_hook_router 只把保留的（失败的）tool_call 发给 ToolNode。

    正常轮（succeeded 为空）直接返回 {}，不做任何过滤，行为不变。
    """

    async def hook(state, config: RunnableConfig) -> dict:
        # 从 config 读已成功工具调用签名集合（由 run_agent_streaming 注入）
        configurable = (config or {}).get("configurable") or {}
        succeeded: set[str] = set(configurable.get("succeeded_tool_calls") or ())
        if not succeeded:
            # 正常轮或未注入：不过滤
            return {}

        messages = state.get("messages") or []
        if not messages:
            return {}
        last_ai = messages[-1]
        if not isinstance(last_ai, AIMessage) or not last_ai.tool_calls:
            return {}

        # 按 (name, args) 签名匹配：命中 succeeded 集合的视为已成功，剔除。
        # 用签名而非仅工具名，避免同工具不同参数（开灯 vs 设空调）被误剔除。
        annotated = [(c, tool_call_signature(c.get("name"), c.get("args") or {})) for c in last_ai.tool_calls]
        kept_calls = [c for c, sig in annotated if sig not in succeeded]
        removed_names = [c.get("name") for c, sig in annotated if sig in succeeded]
        if not removed_names:
            return {}

        logger.info("post_model_hook: 剔除已成功的工具调用 %s，保留 %s", removed_names, [c.get("name") for c in kept_calls])

        # 复用原 id 替换原 AIMessage（add_messages reducer 对同 id 做替换）
        new_ai = AIMessage(
            content=last_ai.content,
            tool_calls=kept_calls,
            id=last_ai.id,
        )
        return {"messages": [new_ai]}

    return hook


# build_chat_agent 每次创建并注入 ChatOpenAI 的 httpx 客户端对（sync, async）。
# 重建 agent 前调 close_agent_http_clients() 关闭上一对，防止连接池泄漏。
_agent_http_clients: list[tuple[Any, Any]] = []


async def close_agent_http_clients() -> None:
    """关闭 build_chat_agent 上次创建的 httpx 客户端，释放连接池。

    供 _rebuild_agent 在构建新 agent 前调用（旧 agent 已不再被 dispatcher 引用）。
    首次构建时列表为空，no-op。
    """
    global _agent_http_clients
    clients = _agent_http_clients
    _agent_http_clients = []
    for sync_client, async_client in clients:
        try:
            sync_client.close()
        except Exception:
            logger.debug("close sync httpx client failed", exc_info=True)
        try:
            await async_client.aclose()
        except Exception:
            logger.debug("close async httpx client failed", exc_info=True)


def build_chat_agent(tools: list, model_config: dict | None = None) -> Any:
    """构建 LangGraph ReAct Agent。

    Args:
        tools: LangChain 工具列表（从 MCPTool 转换而来）
        model_config: 模型配置 {base_url, api_key, model}，为 None 时从 config 读取

    Returns:
        LangGraph CompiledStateGraph（可调用 ainvoke / astream_events）

    每次构建都新建一对 httpx 客户端（sync + async）注入 ChatOpenAI。
    重建前必须调 close_agent_http_clients() 关闭上一对，否则 MCP 工具变更
    触发的每次 rebuild 都会泄漏两个连接池，长跑耗尽文件描述符。
    """
    if model_config is None:
        model_config = _load_model_config_from_config()

    # 创建绕过代理的 httpx 客户端（同时传同步和异步，避免 langchain 警告）
    from ..clients.http_client import new_client, new_sync_client
    http_client = new_sync_client(timeout=60.0)
    http_async_client = new_client(timeout=60.0)
    _agent_http_clients.append((http_client, http_async_client))

    llm = ChatOpenAI(
        model=model_config.get("model", "glm-4-flash"),
        base_url=model_config.get("base_url", "http://127.0.0.1:11434/v1"),
        api_key=model_config.get("api_key", "not-needed"),
        streaming=True,
        temperature=0.7,
        http_client=http_client,
        http_async_client=http_async_client,
    )

    # 关键：必须绑定工具，否则 LLM 不会生成 tool_calls
    if tools:
        llm = llm.bind_tools(tools)

    # create_react_agent 内部是 StateGraph + ToolNode
    # ToolNode 默认支持并行执行多个 tool_call（asyncio.gather）
    # post_model_hook：失败重试轮强制只调失败的工具，剔除已成功的（代码约束，非提示词）
    agent = create_react_agent(llm, tools, post_model_hook=make_post_model_hook())
    return agent


def _load_model_config_from_config() -> dict:
    """从 config.json 读取 chat 角色的模型配置。

    优先级：
    1. providers.chat.key_id 指定的 key
    2. 自动选择第一个 type=chat 且 API key 已设置的 key
    """
    key_entry = resolve_key_for_role("chat")

    if not key_entry:
        raise RuntimeError(
            "未找到可用的 chat LLM 配置。请在 模型配置 页面添加至少一个 type=chat 的 API Key，"
            "并在 .env 中设置对应的 Key 值。"
        )

    base_url = key_entry.get("base_url", "").rstrip("/")
    model = key_entry.get("model", "glm-4-flash")
    api_key = key_entry.get("api_key", "")
    logger.info("LangGraph agent using model: %s @ %s", model, base_url)
    return {
        "base_url": base_url,
        "model": model,
        "api_key": api_key or "not-needed",
    }


async def load_model_config_for_user(user_id: str) -> dict | None:
    """按 user_id 从 DB 解析 chat 模型配置。

    用户有独立 key 配置时返回 {base_url, model, api_key}；
    用户无配置时返回 None，调用方回退全局 agent。
    """
    if not user_id:
        return None
    from ..core.key_resolver import resolve_key_for_role_user
    key_entry = await resolve_key_for_role_user("chat", user_id)
    if not key_entry or not key_entry.get("api_key"):
        return None
    return {
        "base_url": key_entry["base_url"].rstrip("/"),
        "model": key_entry["model"],
        "api_key": key_entry["api_key"],
    }


# ---------------------------------------------------------------------------
# Agent 调用（流式事件）
# ---------------------------------------------------------------------------

async def run_agent_streaming(
    agent,
    messages: list[BaseMessage],
    session: SessionState,
    timeout: float = 120.0,
    succeeded_tool_calls: set[str] | None = None,
) -> AsyncIterator[dict]:
    """运行 LangGraph agent 并产出流式事件。

    事件格式：
    - {"type": "token", "content": str}  — LLM 输出的 token
    - {"type": "tool_start", "tool_name": str, "tool_args": dict}  — 工具开始执行
    - {"type": "tool_end", "tool_name": str, "result": str, "error": bool}  — 工具执行完成
    - {"type": "final", "content": str}  — 最终回复
    - {"type": "error", "message": str}  — 错误

    Args:
        agent: LangGraph CompiledStateGraph
        messages: LangChain 消息列表（含 SystemMessage）
        session: 当前会话状态（通过 config 传递给工具）
        timeout: 超时秒数
        succeeded_tool_calls: 失败重试轮传入上一轮已成功的工具调用签名集合，
            post_model_hook 据此剔除已成功的 tool_call，强制只调失败的。
            正常轮传 None（不过滤）。
    """
    config = {"configurable": {"session": session}}
    # 注入已成功工具调用签名，供 post_model_hook 在重试轮剔除已成功的 tool_call
    if succeeded_tool_calls:
        config["configurable"]["succeeded_tool_calls"] = succeeded_tool_calls

    # 获取 metrics service
    metrics = None
    try:
        from ..container import get_container
        metrics = get_container().metrics_service
    except Exception:
        pass

    try:
        async with asyncio.timeout(timeout):
            final_content = ""
            llm_call_recorded = False
            async for event in agent.astream_events(
                {"messages": messages},
                config=config,
                version="v2",
            ):
                kind = event.get("event", "")

                if kind == "on_chat_model_stream":
                    # 记录 LLM 调用（只记录一次）
                    if not llm_call_recorded and metrics:
                        metrics.record_llm_call()
                        llm_call_recorded = True
                    # LLM 流式输出 token
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        content = chunk.content
                        if isinstance(content, str):
                            final_content += content
                            yield {"type": "token", "content": content}

                elif kind == "on_tool_start":
                    # 工具开始执行
                    tool_name = event.get("name", "unknown")
                    tool_args = event.get("data", {}).get("input", {})
                    # run_id 同一次调用的 on_tool_start/on_tool_end 相同，
                    # 用作关联键：同名工具并行时仍能正确配对 start↔end
                    yield {"type": "tool_start", "tool_name": tool_name, "tool_args": tool_args,
                           "run_id": event.get("run_id", "")}

                elif kind == "on_tool_end":
                    # 工具执行完成
                    tool_name = event.get("name", "unknown")
                    output = event.get("data", {}).get("output", "")
                    # langgraph 0.4 中 output 是 ToolMessage 对象，取 .content 拿到实际内容
                    if hasattr(output, "content"):
                        output = output.content
                    if isinstance(output, dict):
                        import json
                        output = json.dumps(output, ensure_ascii=False, default=str)
                    is_error = isinstance(output, str) and output.startswith("Error:")
                    # 记录工具调用
                    if metrics:
                        metrics.record_tool_call(tool_name, error=is_error)
                    yield {"type": "tool_end", "tool_name": tool_name, "result": str(output),
                           "error": is_error, "run_id": event.get("run_id", "")}

            # 流结束，产出最终回复
            yield {"type": "final", "content": final_content}

    except asyncio.TimeoutError:
        yield {"type": "error", "message": f"Agent 执行超时（{timeout}秒）"}
    except Exception as e:
        logger.exception("LangGraph agent error")
        # 区分 API 错误，给出友好提示
        error_msg = _friendly_api_error(e)
        yield {"type": "error", "message": error_msg}


def _friendly_api_error(e: Exception) -> str:
    """将 LLM API 异常转为用户友好的提示。"""
    # openai.APIStatusError 有 status_code 属性
    status_code = getattr(e, "status_code", None)

    if status_code == 502:
        return (
            "抱歉，当前使用的 LLM 模型服务返回 502 错误（服务不可用）。\n\n"
            "可能原因：\n"
            "• 模型服务商临时故障或维护中\n"
            "• 请求频率过高被限流\n"
            "• API 额度已用完\n\n"
            "建议：前往「模型配置」页面切换到其他模型再试。"
        )

    if status_code == 429:
        return (
            "抱歉，当前模型请求频率超限（429 Too Many Requests）。\n\n"
            "建议：\n"
            "• 稍等几秒后再发送\n"
            "• 前往「模型配置」切换到并发更高的模型\n"
            "• 或为当前模型充值升级配额"
        )

    if status_code == 401:
        return (
            "抱歉，当前模型的 API Key 无效或已过期（401 Unauthorized）。\n\n"
            "建议：前往「API Key 管理」页面检查或更换 Key。"
        )

    if status_code == 403:
        return (
            "抱歉，当前模型拒绝访问（403 Forbidden）。\n\n"
            "可能原因：API Key 权限不足、模型未授权、或账户欠费。\n"
            "建议：前往「API Key 管理」或「模型配置」页面检查。"
        )

    if status_code and status_code >= 500:
        return (
            f"抱歉，LLM 模型服务异常（HTTP {status_code}）。\n\n"
            "这是模型服务商的问题，建议稍后重试或切换到其他模型。"
        )

    # 非 API 错误或其他未识别的 API 错误
    # 截断过长的错误信息（比如包含 HTML 的响应体）
    raw_msg = str(e)
    if len(raw_msg) > 200:
        raw_msg = raw_msg[:200] + "..."
    return f"Agent 执行出错：{raw_msg}"
