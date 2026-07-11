"""Tests for app/sg/pipeline/entity_extractor.py。

回归保护：SINGLE_PROMPT 曾因多余 } 导致 .format() 输出脏数据，
此测试确保 prompt 格式化不抛异常、不产生多余字面量。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.sg.pipeline.entity_extractor import EntityExtractor, SINGLE_PROMPT


class TestSinglePromptFormat:
    """SINGLE_PROMPT 必须能安全 .format()。"""

    def test_format_does_not_raise(self):
        """格式化不抛 ValueError（多余的 } 会触发 Single '}' encountered）。"""
        result = SINGLE_PROMPT.format(text="测试文档")
        assert "测试文档" in result

    def test_format_no_extra_literal_brace(self):
        """格式化后不应残留多余的字面 }。原始 bug：{text}}} 多输出一个 }。"""
        result = SINGLE_PROMPT.format(text="X")
        # JSON 模板里的 }} 是转义出的 {，属于预期；末尾不应有连续多余 }
        assert "}}}" not in result

    def test_format_preserves_json_template(self):
        """格式化后 JSON 结构占位符仍在（LLM 按此输出）。"""
        result = SINGLE_PROMPT.format(text="文档")
        assert "entities" in result
        assert "relations" in result

    def test_format_handles_special_chars_in_text(self):
        """text 含 { } 也不应破坏格式化（格式化已一次性替换）。"""
        result = SINGLE_PROMPT.format(text="带{大括号}的文本")
        assert "带{大括号}的文本" in result


def _make_doc(text: str) -> SimpleNamespace:
    return SimpleNamespace(raw_text=text)


class TestEntityExtractorExtract:
    """EntityExtractor.extract_batch / _extract_one。"""

    def test_extract_one_parses_valid_json(self):
        """chat_fn 返回合法 JSON → 解析出 entities/relations。"""
        def chat_fn(messages, max_tokens=1024):
            return json.dumps({
                "entities": [{"name": "GLM", "type": "AI模型"}],
                "relations": [{"source": "GLM", "target": "API", "type": "调用"}],
            })

        ext = EntityExtractor(chat_fn)
        result = ext._extract_one("GLM 调用 API")
        assert len(result["entities"]) == 1
        assert result["entities"][0]["name"] == "GLM"
        assert len(result["relations"]) == 1

    def test_extract_one_returns_empty_on_invalid_json(self):
        """chat_fn 返回非 JSON → 兜底空结构。"""
        ext = EntityExtractor(lambda msg, max_tokens=1024: "这不是JSON")
        result = ext._extract_one("文本")
        assert result == {"entities": [], "relations": []}

    def test_extract_one_swallows_chat_exception(self):
        """chat_fn 抛异常 → 兜底空结构，不向上抛。"""
        def boom(msg, max_tokens=1024):
            raise RuntimeError("LLM 挂了")
        ext = EntityExtractor(boom)
        result = ext._extract_one("文本")
        assert result == {"entities": [], "relations": []}

    def test_extract_batch_returns_aligned_results(self):
        """批量抽取结果与输入 docs 等长且顺序对齐。"""
        def chat_fn(msg, max_tokens=1024):
            return json.dumps({"entities": [], "relations": []})

        ext = EntityExtractor(chat_fn, max_workers=2)
        docs = [_make_doc(f"文档{i}") for i in range(5)]
        results = ext.extract_batch(docs)
        assert len(results) == 5
        for r in results:
            assert "entities" in r
            assert "relations" in r

    def test_extract_batch_empty_list(self):
        ext = EntityExtractor(lambda msg, max_tokens=1024: "{}")
        assert ext.extract_batch([]) == []

    def test_extract_batch_calls_progress_callback(self):
        """on_progress 回调应被调用，参数为 (done, total)。"""
        def chat_fn(msg, max_tokens=1024):
            return json.dumps({"entities": [], "relations": []})

        progress = []
        ext = EntityExtractor(chat_fn, max_workers=2)
        docs = [_make_doc(f"d{i}") for i in range(3)]
        ext.extract_batch(docs, on_progress=lambda done, total: progress.append((done, total)))
        # 3 个文档，回调至少触发 3 次（每完成一个）
        assert len(progress) == 3
        assert progress[-1] == (3, 3)

    def test_extract_batch_partial_failure_does_not_crash(self):
        """部分文档抽取失败 → 该项空结构，其余正常。"""
        call_count = [0]

        def chat_fn(msg, max_tokens=1024):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("第二个炸了")
            return json.dumps({"entities": [{"name": "ok", "type": "概念"}], "relations": []})

        ext = EntityExtractor(chat_fn, max_workers=1)
        docs = [_make_doc(f"d{i}") for i in range(3)]
        results = ext.extract_batch(docs)
        assert len(results) == 3
        # 失败的那项是空结构，不是 None
        for r in results:
            assert r is not None
            assert "entities" in r

    def test_extract_truncates_long_text(self):
        """raw_text 超过 1500 字符应被截断（防止 prompt 过长）。"""
        captured = {}

        def chat_fn(msg, max_tokens=1024):
            # user message 是 SINGLE_PROMPT.format(text=...)
            captured["user_content"] = msg[1]["content"]
            return json.dumps({"entities": [], "relations": []})

        ext = EntityExtractor(chat_fn)
        marker = "X" * 3000  # 用 X 标记，避免与 prompt 模板里的字面量字母冲突
        ext.extract_batch([_make_doc(marker)])
        # 截断后 prompt 里 X 的数量应 <= 1500（模板正文不含 X）
        assert captured["user_content"].count("X") <= 1500
        assert captured["user_content"].count("X") > 0  # 确实有内容
