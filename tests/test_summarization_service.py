"""Tests for SummarizationService pure methods."""
from __future__ import annotations

from app.services.summarization_service import SummarizationService
from app.services.session_store import SessionState


class TestEstimateTokens:
    def setup_method(self):
        self.svc = SummarizationService()

    def test_empty(self):
        assert self.svc.estimate_tokens([]) == 1  # min 1

    def test_chinese_text(self):
        messages = [{"role": "user", "content": "你好世界"}]
        tokens = self.svc.estimate_tokens(messages)
        assert tokens > 0

    def test_longer_text_more_tokens(self):
        short = [{"role": "user", "content": "hi"}]
        long = [{"role": "user", "content": "a" * 1000}]
        assert self.svc.estimate_tokens(long) > self.svc.estimate_tokens(short)


class TestShouldCompress:
    def setup_method(self):
        self.svc = SummarizationService()

    def test_short_conversation_no_compress(self):
        s = SessionState(session_id="t", request_id="r")
        s.model_messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        should, level = self.svc.should_compress(s)
        assert should is False
        assert level is None

    def test_hard_max_turns(self):
        s = SessionState(session_id="t", request_id="r")
        s.model_messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"} for i in range(40)]
        should, level = self.svc.should_compress(s)
        assert should is True
        assert level == "hard"

    def test_soft_max_turns(self):
        s = SessionState(session_id="t", request_id="r")
        s.model_messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"} for i in range(26)]
        should, level = self.svc.should_compress(s)
        assert should is True
        assert level == "soft"


class TestTruncateSummary:
    def test_single_message(self):
        result = SummarizationService._truncate_summary(["hello world"])
        assert result == "hello world"

    def test_single_long_message_truncated(self):
        result = SummarizationService._truncate_summary(["a" * 500])
        assert len(result) == 240

    def test_multiple_messages(self):
        result = SummarizationService._truncate_summary(["first message", "last message"])
        assert "2条" in result
        assert "first" in result
        assert "last" in result


class _FakeClient:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []

    async def chat(self, messages, timeout):
        self.calls.append(messages)
        return "摘要结果"


class TestResolveSummaryClient:
    """摘要无独立模型角色：一律复用对话（chat）模型。"""

    async def test_uses_per_user_chat_role(self):
        import asyncio
        from unittest.mock import AsyncMock

        import app.services.summarization_service as ss

        per_user = _FakeClient()
        factory = AsyncMock(return_value=per_user)
        original = ss.build_per_user_chat_client
        ss.build_per_user_chat_client = factory
        try:
            svc = SummarizationService(chat_client=_FakeClient())
            got = await svc._resolve_summary_client("u1")
        finally:
            ss.build_per_user_chat_client = original
        # 角色必须是 chat（summary 角色已删除）
        factory.assert_awaited_once_with("chat", "u1", force_enabled=False)
        assert got is per_user

    async def test_falls_back_to_global_chat_client(self):
        import asyncio
        from unittest.mock import AsyncMock

        import app.services.summarization_service as ss

        factory = AsyncMock(return_value=None)
        original = ss.build_per_user_chat_client
        ss.build_per_user_chat_client = factory
        try:
            global_client = _FakeClient()
            svc = SummarizationService(chat_client=global_client)
            got = await svc._resolve_summary_client("u1")
        finally:
            ss.build_per_user_chat_client = original
        assert got is global_client


class TestSummarizeChunkPrompt:
    """摘要 prompt 规范：结构化保留要求 + 输入截断上限。"""

    async def test_prompt_contains_structured_requirements(self):
        import asyncio
        from unittest.mock import patch

        import app.services.summarization_service as ss

        client = _FakeClient()
        with patch.object(ss, "get_config", side_effect=lambda k, d=None: 30 if k == "llm.summary_timeout_seconds" else d):
            text = await SummarizationService()._summarize_chunk(["用户: 开灯"], chat_client=client)
        assert text == "摘要结果"
        system = client.calls[0][0]["content"]
        # 结构化规范：设备/房间、指令与执行结果、未完成意图、时间与数量
        assert "设备名" in system
        assert "执行结果" in system
        assert "未完成" in system
        assert "时间" in system
        assert "300字" in system

    async def test_input_truncated_to_12000_chars(self):
        import asyncio
        from unittest.mock import patch

        import app.services.summarization_service as ss

        client = _FakeClient()
        long_text = "x" * 20000
        with patch.object(ss, "get_config", side_effect=lambda k, d=None: 30 if k == "llm.summary_timeout_seconds" else d):
            await SummarizationService()._summarize_chunk([long_text], chat_client=client)
        user_msg = client.calls[0][1]["content"]
        # "请摘要以下对话片段:\n" 前缀 + 12000 字符
        assert len(user_msg) == len("请摘要以下对话片段:\n") + 12000

    async def test_disabled_client_falls_back_to_truncation(self):
        import asyncio
        from unittest.mock import patch

        import app.services.summarization_service as ss

        client = _FakeClient(enabled=False)
        with patch.object(ss, "get_config", side_effect=lambda k, d=None: d if k == "llm.summary_enabled" else d):
            text = await SummarizationService()._summarize_chunk(["a" * 500], chat_client=client)
        assert len(text) == 240  # 截断式回退
        assert client.calls == []  # 未调 LLM
