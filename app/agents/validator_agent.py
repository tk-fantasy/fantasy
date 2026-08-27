"""校验 Agent — 用 LLM 语义判断模型是否表达了执行意图但没有确认动作已完成。"""
from __future__ import annotations

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..core.config import get_config
from ..core.key_resolver import resolve_key_for_role

logger = logging.getLogger(__name__)

# 匹配"声称已完成设备控制操作"的措辞，用于硬规则短路：
# tool_call_count==0 但说了这类话 → 模型在撒谎，强制重试。
#
# 设计原则：只匹配"已+控制动词"这种强完成态结构，不匹配通用完成词。
# - "已打开/已关闭/已调节/已切换" 基本只出现在设备控制语境，闲聊不会这么说；
# - 刻意不收 "完成/搞定/好了" 等通用词——它们在闲聊里太常见
#   （"计划好了""方案完成了"），会误判正常对话为撒谎。
# - 动作动词是通用控制动作（开/关/调/设置/切换），不硬编码设备名，
#   用户加新设备类型无需改正则。
_ACTION_DONE_RE = re.compile(
    r"已(经)?(打开|关闭|开启|关掉|调节|调整|设置|切换)|"
    r"帮\s*你.*(打开|关闭|开启|关掉|调[节整])",
    re.IGNORECASE,
)

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
        # per-user LLM 缓存（user_id → ChatOpenAI），仿 dispatcher._user_agents 模式。
        # 主聊天重试时 validator 与主对话用同一模型，避免全局/用户模型不一致误判。
        # user_id 为空（APP_TOKEN 鉴权等）走全局 self._llm。
        # 注意：key 解析是 async（resolve_key_for_role_user），在 should_retry 内完成；
        # 这里只缓存已构建的 ChatOpenAI 实例。
        self._user_llms: dict[str, ChatOpenAI] = {}

    @property
    def max_retries(self) -> int:
        """校验重试上限（dispatcher 的失败重试上限以此对齐，避免跨类读私有属性）。"""
        return self._max_retries

    def invalidate_user(self, user_id: str) -> None:
        """用户修改 chat key 后清除其缓存的 per-user LLM，下次 should_retry 重建。

        与 dispatcher.invalidate_user_agent 对齐——key 变更后旧 LLM 实例
        （带旧 api_key）必须清除，否则 validator 用旧 key 请求会误判或报错。
        user_id 为空或未缓存则 no-op。
        """
        old = self._user_llms.pop(user_id, None)
        if old is not None:
            logger.info("Validator: invalidated cached LLM for user_id=%s", user_id)

    def _get_llm(self, user_id: str = "") -> ChatOpenAI:
        """按 user_id 取已缓存的 per-user LLM；未缓存或无 user_id 回退全局。

        per-user LLM 的构建（含 async key 解析）由 _resolve_user_llm 完成，
        should_retry 调用它后本方法从缓存取。
        """
        if user_id and user_id in self._user_llms:
            return self._user_llms[user_id]
        # 无 per-user 缓存：走全局
        if self._llm is None:
            self._llm = self._build_llm()
        return self._llm

    async def _resolve_user_llm(self, user_id: str) -> ChatOpenAI | None:
        """按 user_id 解析 per-user chat key 并构建 LLM，缓存后返回。

        在 async 上下文（should_retry）内调用。用户无 per-user 配置返回 None，
        调用方回退全局 _get_llm(user_id)（命中全局分支）。
        """
        if not user_id or user_id in self._user_llms:
            return self._user_llms.get(user_id)
        try:
            from ..core.key_resolver import resolve_key_for_role_user
            key_info = await resolve_key_for_role_user("chat", user_id)
            if not key_info or not key_info.get("api_key"):
                return None
            from ..clients.http_client import new_client, new_sync_client
            llm = ChatOpenAI(
                model=key_info.get("model", "glm-4-flash"),
                base_url=key_info.get("base_url", "").rstrip("/"),
                api_key=key_info["api_key"],
                temperature=0.0,
                max_tokens=50,
                http_client=new_sync_client(timeout=30.0),
                http_async_client=new_client(timeout=30.0),
            )
            self._user_llms[user_id] = llm
            logger.info("Validator: built per-user LLM for user_id=%s, model=%s",
                        user_id, key_info.get("model"))
            return llm
        except Exception:
            logger.debug("Validator: failed to build per-user LLM, will fallback to global", exc_info=True)
            return None

    @staticmethod
    def _build_llm() -> ChatOpenAI:
        """复用 chat 角色的【全局】模型配置构建轻量 LLM 实例。"""
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

    async def should_retry(self, final_content: str, tool_call_count: int,
                           user_id: str = "") -> bool:
        """判断模型是否需要重试。

        硬性规则优先：如果回复声称完成了设备控制操作（"已打开""已关闭"等），
        但 tool_call_count == 0，说明模型在撒谎（嘴上说了但没真调工具），
        直接返回 True 强制重试，不浪费一次 LLM 语义判断调用。

        _ACTION_DONE_RE 只匹配"已+控制动词"强完成态，不收"完成/搞定/好了"等
        通用词——避免把闲聊（"计划好了"）误判为撒谎。这样无需 user_query 闸门，
        单看回复即可区分设备控制的漏调与正常闲聊。

        硬性规则不命中时，回退到 LLM 语义判断。

        Args:
            final_content: 模型最终输出的文本内容
            tool_call_count: 工具调用次数
            user_id: 当前用户 ID，用于解析 per-user chat key。为空或用户无 per-user 配置时走全局。

        Returns:
            True 表示需要重试
        """
        if not final_content.strip():
            logger.debug("Validator: empty content, skip retry")
            return False

        # 硬性规则：声称已完成设备控制操作但没调任何工具 → 撒谎，强制重试。
        # _ACTION_DONE_RE 只匹配"已打开/已关闭"等控制动作强完成态，
        # 不匹配"完成/搞定/好了"等通用词，闲聊不会误触发。
        if tool_call_count == 0 and _ACTION_DONE_RE.search(final_content):
            logger.info("Validator: 检测到声称已完成控制操作但 tool_calls=0，强制重试: %r",
                        final_content[:80])
            return True

        # per-user 优先：解析用户 chat key 构建专用 LLM（缓存），失败/无配置回退全局
        if user_id:
            await self._resolve_user_llm(user_id)
        llm = self._get_llm(user_id)
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
            # 解析 JSON 响应：优先 json.loads 精确解析 need_retry 字段，
            # LLM 偶尔返回非 JSON 时降级到词边界匹配 "true"（避免 "true story" 误判）。
            return self._parse_need_retry(text)
        except Exception:
            logger.exception("Validator: LLM call failed, fallback to no retry")
            # 记录 LLM 调用错误
            try:
                from ..container import get_container
                get_container().metrics_service.record_llm_call(error=True)
            except Exception:
                pass
            return False

    @staticmethod
    def _parse_need_retry(text: str) -> bool:
        """解析 validator LLM 的返回，判断是否 need_retry。

        优先 json.loads 精确解析 {"need_retry": true/false}；
        LLM 偶尔返回纯 "true"/"false" 时按词边界判定。
        其它非 JSON 解释性文本（如 "true story"）一律不重试——
        原代码 "true" in text 会把这类子串误判为需重试。
        """
        text = text.strip()
        # 尝试精确 JSON 解析
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return bool(parsed.get("need_retry", False))
            if isinstance(parsed, bool):
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        # 降级：只有整段文本就是独立的 true（去掉空格后）才算，
        # 避免把 "true story" / "not true" 这类解释性文本误判为需重试。
        return text.lower() in ("true", "yes", "1")

    def build_retry_message(self) -> HumanMessage:
        """构建重试提示消息。"""
        return HumanMessage(
            content="你刚才只输出了文字回复，没有通过 tool_call 调用任何工具。"
                    "你必须立即通过 tool_call 机制调用必要的工具来执行操作。"
                    "绝对不要在回复文本中写 JSON 代码块来模拟工具调用。"
                    "现在请立即调用工具。"
        )
