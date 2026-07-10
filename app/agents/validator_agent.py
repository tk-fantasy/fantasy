"""校验 Agent — 用 LLM 语义判断模型是否表达了执行意图但没有确认动作已完成。"""
from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..core.config import get_config
from ..core.key_resolver import resolve_key_for_role

logger = logging.getLogger(__name__)

_VALIDATOR_SYSTEM_PROMPT = (
    "你是一个校验助手。你的唯一任务是判断一段对话回复是否只表达了要做某事的意图，"
    "但没有确认动作已经完成。\n\n"
    "判断规则：\n"
    "- 如果回复中明确表达了即将执行某个操作的意图（如'我将'、'我会'、'请稍等'等），"
    "且没有同时确认该操作已经完成，返回 true。\n"
    "- 如果回复中已经确认动作完成（如'已经打开'、'已完成'、'搞定了'等），返回 false。\n"
    "- 如果回复与执行操作无关（纯闲聊），返回 false。\n"
    "- 如果回复既表达了意图又确认了完成（如'我将帮你打开，已经打开了'），返回 false。\n\n"
    "只返回 JSON：{\"need_retry\": true} 或 {\"need_retry\": false}，不要返回其他内容。"
)


class ValidatorAgent:
    """用 LLM 语义校验 agent 的返回是否真的执行了动作。

    当模型说"我将关闭床头灯"但没有说"已经关闭"时，
    自动追加提示消息让模型继续执行。
    """

    def __init__(self, max_retries: int = 1, llm: ChatOpenAI | None = None):
        self._max_retries = max_retries
        self._llm = llm

    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = self._build_llm()
        return self._llm

    @staticmethod
    def _build_llm() -> ChatOpenAI:
        """复用 chat 角色的模型配置构建轻量 LLM 实例。"""
        from ..clients.http_client import new_client, new_sync_client
        http_client = new_sync_client(timeout=30.0)
        http_async_client = new_client(timeout=30.0)

        key_entry = resolve_key_for_role("chat")

        if key_entry:
            base_url = key_entry.get("base_url", "").rstrip("/")
            model = key_entry.get("model", "glm-4-flash")
            api_key = key_entry.get("api_key", "")
            return ChatOpenAI(
                model=model,
                base_url=base_url,
                api_key=api_key or "not-needed",
                temperature=0.0,
                max_tokens=50,
                http_client=http_client,
                http_async_client=http_async_client,
            )

        base_url = str(get_config("llm.base_url", "http://127.0.0.1:11434")).rstrip("/")
        model = str(get_config("llm.chat_model", "qwen3.5:9b"))
        if "127.0.0.1" in base_url or "localhost" in base_url:
            if not base_url.endswith("/v1"):
                base_url = base_url + "/v1"
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key="not-needed",
            temperature=0.0,
            max_tokens=50,
            http_client=http_client,
            http_async_client=http_async_client,
        )

    async def should_retry(self, final_content: str, tool_call_count: int) -> bool:
        """用 LLM 语义判断模型是否只表达了执行意图但没有确认动作已完成。

        Args:
            final_content: 模型最终输出的文本内容
            tool_call_count: 工具调用次数（保留参数但不参与判断）

        Returns:
            True 表示需要重试
        """
        if not final_content.strip():
            logger.debug("Validator: empty content, skip retry")
            return False

        llm = self._get_llm()
        messages = [
            SystemMessage(content=_VALIDATOR_SYSTEM_PROMPT),
            HumanMessage(content=final_content[:500]),  # 截断防止超长
        ]

        try:
            # 记录 LLM 调用
            try:
                from ..container import get_container
                get_container().metrics_service.record_llm_call()
            except Exception:
                pass

            response = await llm.ainvoke(messages)
            text = response.content.strip() if response.content else ""
            logger.info("Validator: content=%r..., tool_calls=%d, validator_response=%r",
                        final_content[:80], tool_call_count, text[:80])
            # 解析 JSON 响应
            if "true" in text.lower():
                logger.info("Validator: retry needed")
                return True
            logger.info("Validator: no retry needed")
            return False
        except Exception:
            logger.exception("Validator: LLM call failed, fallback to no retry")
            # 记录 LLM 调用错误
            try:
                from ..container import get_container
                get_container().metrics_service.record_llm_call(error=True)
            except Exception:
                pass
            return False

    def build_retry_message(self) -> HumanMessage:
        """构建重试提示消息。"""
        return HumanMessage(
            content="你刚才只输出了文字回复，没有通过 tool_call 调用任何工具。"
                    "你必须立即通过 tool_call 机制调用必要的工具来执行操作。"
                    "绝对不要在回复文本中写 JSON 代码块来模拟工具调用。"
                    "现在请立即调用工具。"
        )
