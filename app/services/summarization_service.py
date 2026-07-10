from __future__ import annotations

import logging

from ..core.config import get_config
from .session_store import SessionState

logger = logging.getLogger(__name__)


class SummarizationService:
    def __init__(self, chat_client=None) -> None:
        # 注入 LLM 聊天客户端用于真正的 LLM 摘要;为空时退回截断
        self._chat_client = chat_client
        # 从配置文件加载，保留硬编码默认值作为 fallback
        self._soft_max_turns = int(get_config("rag.soft_max_turns", 12))
        self._hard_max_turns = int(get_config("rag.hard_max_turns", 16))
        self._soft_max_tokens = int(get_config("rag.soft_max_tokens", 12000))
        self._hard_max_tokens = int(get_config("rag.hard_max_tokens", 16000))
        self._soft_max_chars = int(get_config("rag.soft_max_chars", 24000))
        self._hard_max_chars = int(get_config("rag.hard_max_chars", 32000))
        self._recent_turns_to_keep = int(get_config("rag.recent_turns", 5))
        self._summary_blocks = int(get_config("rag.summary_blocks", 2))
        # 摘要缓存：按 session_id 记录上次处理时的消息数，消息数未变则跳过。
        # 必须按会话隔离，否则多会话共享单例会导致会话间误跳过压缩。
        self._last_message_count: dict[str, int] = {}

    def estimate_tokens(self, messages: list[dict]) -> int:
        """估算 token 数。

        中文每字符约 1-2 token，英文每单词约 1-1.5 token。
        使用字符数 * 0.75 作为折中估算（对中英文都较接近）。
        """
        text = "\n".join(str(message.get("content", "")) for message in messages)
        return max(1, int(len(text) * 0.75))

    def should_compress(self, session: SessionState) -> tuple[bool, str | None]:
        # turn 计数只算 user 消息数（ReAct 模式下 ToolMessage 会膨胀消息数）
        turn_count = sum(1 for m in session.model_messages if m.get("role") == "user")
        total_chars = sum(len(str(message.get("content", ""))) for message in session.model_messages)
        estimated_tokens = self.estimate_tokens(session.model_messages)
        if turn_count >= self._hard_max_turns or estimated_tokens >= self._hard_max_tokens or total_chars >= self._hard_max_chars:
            return True, "hard"
        if turn_count >= self._soft_max_turns or estimated_tokens >= self._soft_max_tokens or total_chars >= self._soft_max_chars:
            return True, "soft"
        return False, None

    async def refresh_summaries(self, session: SessionState) -> list[dict]:
        # 摘要缓存：该 session 消息数未变则跳过
        current_count = len(session.model_messages)
        if self._last_message_count.get(session.session_id) == current_count:
            return session.summaries
        should, _ = self.should_compress(session)
        if not should:
            self._last_message_count[session.session_id] = current_count
            return session.summaries
        older_messages = session.model_messages[:-self._recent_turns_to_keep]
        if not older_messages:
            return session.summaries
        text_blocks = [str(message.get("content", "")).strip() for message in older_messages if str(message.get("content", "")).strip()]
        if not text_blocks:
            return session.summaries
        chunk_size = max(1, len(text_blocks) // self._summary_blocks)
        chunks: list[list[str]] = []
        for index in range(0, len(text_blocks), chunk_size):
            chunk = text_blocks[index:index + chunk_size]
            if chunk:
                chunks.append(chunk)
            if len(chunks) >= self._summary_blocks:
                break

        # 并发处理所有 chunk
        import asyncio
        tasks = [self._summarize_chunk(chunk) for chunk in chunks]
        results = await asyncio.gather(*tasks)

        summaries: list[dict] = []
        for chunk, summary_text in zip(chunks, results):
            summaries.append(
                {
                    "id": f"summary-{len(summaries)}",
                    "text": summary_text,
                    "source_count": len(chunk),
                }
            )
        session.summaries = summaries
        self._last_message_count[session.session_id] = current_count
        return summaries

    async def _summarize_chunk(self, chunk: list[str]) -> str:
        joined = "\n".join(chunk)
        # 配置开启且有可用客户端时,调 LLM 压缩;否则/失败回退到截断式
        use_llm = bool(get_config("llm.summary_enabled", True))
        if use_llm and self._chat_client is not None and getattr(self._chat_client, "enabled", False):
            try:
                timeout = int(get_config("llm.summary_timeout_seconds", 30))
                messages = [
                    {
                        "role": "system",
                        "content": "你是对话摘要器。把多轮对话压缩成一段简洁中文摘要,保留关键事实、用户意图和已执行的操作,不要寒暄,不超过150字。",
                    },
                    {"role": "user", "content": f"请摘要以下对话片段:\n{joined[:4000]}"},
                ]
                summary = await self._chat_client.chat(messages, timeout)
                summary = str(summary).strip()
                if summary:
                    return summary
            except Exception:  # noqa: BLE001
                logger.warning("LLM summary failed, fall back to truncation", exc_info=True)
        return self._truncate_summary(chunk)

    @staticmethod
    def _truncate_summary(chunk: list[str]) -> str:
        first = chunk[0]
        last = chunk[-1]
        if len(chunk) == 1:
            return first[:240]
        return f"历史摘要({len(chunk)}条): {first[:120]} ... {last[:120]}"
