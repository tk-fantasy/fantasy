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
