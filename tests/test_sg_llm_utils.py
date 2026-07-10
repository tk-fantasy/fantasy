"""Tests for app/sg/pipeline/llm_utils.py — LLM 输出解析与调用辅助。"""
from __future__ import annotations

from app.sg.pipeline.llm_utils import call_chat, parse_json_from_llm


class TestParseJsonFromLlm:
    def test_pure_json(self):
        content = '{"relation_type": "前置依赖", "description": "A是B的前提"}'
        result = parse_json_from_llm(content)
        assert result == {"relation_type": "前置依赖", "description": "A是B的前提"}

    def test_json_embedded_in_text(self):
        """LLM 常在 JSON 前后加自然语言，应能提取。"""
        content = '好的，分析结果如下：\n{"relation_type": "同主题", "description": "弱关联"}\n以上。'
        result = parse_json_from_llm(content)
        assert result["relation_type"] == "同主题"

    def test_json_in_code_block(self):
        content = '```json\n{"relation_type": "无明显关系", "description": "不相关"}\n```'
        result = parse_json_from_llm(content)
        assert result["relation_type"] == "无明显关系"

    def test_no_json_returns_none(self):
        assert parse_json_from_llm("纯文本，没有 JSON") is None

    def test_invalid_json_returns_none(self):
        assert parse_json_from_llm("{broken json}") is None

    def test_empty_string(self):
        assert parse_json_from_llm("") is None


class TestCallChat:
    def test_calls_fn_with_messages(self):
        captured = {}

        def fake_chat_fn(messages, max_tokens=1024):
            captured["messages"] = messages
            captured["max_tokens"] = max_tokens
            return "LLM 响应"

        result = call_chat(fake_chat_fn, [{"role": "user", "content": "hi"}], max_tokens=256)
        assert result == "LLM 响应"
        assert captured["messages"] == [{"role": "user", "content": "hi"}]
        assert captured["max_tokens"] == 256

    def test_default_max_tokens(self):
        captured = {}

        def fake_chat_fn(messages, max_tokens=1024):
            captured["max_tokens"] = max_tokens
            return "ok"

        call_chat(fake_chat_fn, [{"role": "user", "content": "hi"}])
        assert captured["max_tokens"] == 1024
