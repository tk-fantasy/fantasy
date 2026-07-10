"""Tests for app/sg/pipeline/relation_analyzer.py — 邻居关系分析。

重点回归 PAIR_PROMPT 的 str.format() 兼容性：
此前模板末尾有一个多余的 '}'，导致 format() 抛
"Single '}' encountered in format string"，整个构建在第 4 步失败。
"""
from __future__ import annotations

from app.sg.pipeline.parser import Document
from app.sg.pipeline.relation_analyzer import PAIR_PROMPT, _call_llm


class TestPairPromptFormat:
    def test_prompt_formats_without_error(self):
        """PAIR_PROMPT 必须能被 str.format() 正确渲染。"""
        out = PAIR_PROMPT.format(
            title_a="文档A",
            title_b="文档B",
            excerpt_a="A 的摘要",
            excerpt_b="B 的摘要",
        )
        assert "文档A" in out
        assert "文档B" in out
        assert "A 的摘要" in out

    def test_prompt_contains_relation_types(self):
        """模板应列出全部关系类型供 LLM 参考。"""
        out = PAIR_PROMPT.format(
            title_a="A", title_b="B", excerpt_a="x", excerpt_b="y"
        )
        for rtype in ["前置依赖", "功能关联", "配置关联", "同主题", "问题排查", "无明显关系"]:
            assert rtype in out


class TestCallLlm:
    def _make_doc(self, doc_id, text):
        return Document(
            id=doc_id, title=doc_id, category="安装", subcategory=None,
            filepath=f"docs/{doc_id}.md", raw_text=text,
        )

    def test_returns_relation_type(self):
        """LLM 返回有效 JSON 时，_call_llm 返回对应关系类型。"""
        def fake_chat_fn(messages, max_tokens=1024):
            return '{"relation_type": "前置依赖", "description": "A是B的前提"}'

        doc_a = self._make_doc("A", "A 的内容" * 50)
        doc_b = self._make_doc("B", "B 的内容" * 50)
        assert _call_llm(doc_a, doc_b, fake_chat_fn) == "前置依赖"

    def test_no_relation_returns_none(self):
        """LLM 判定「无明显关系」时返回 None（不连边）。"""
        def fake_chat_fn(messages, max_tokens=1024):
            return '{"relation_type": "无明显关系", "description": "不相关"}'

        doc_a = self._make_doc("A", "内容A")
        doc_b = self._make_doc("B", "内容B")
        assert _call_llm(doc_a, doc_b, fake_chat_fn) is None

    def test_invalid_json_falls_back_to_same_topic(self):
        """LLM 输出非 JSON 时回退为「同主题」。"""
        def fake_chat_fn(messages, max_tokens=1024):
            return "这不是 JSON"

        doc_a = self._make_doc("A", "内容A")
        doc_b = self._make_doc("B", "内容B")
        assert _call_llm(doc_a, doc_b, fake_chat_fn) == "同主题"

    def test_chat_exception_falls_back_to_same_topic(self):
        """LLM 调用抛异常时回退为「同主题」（容错，不中断构建）。"""
        def fake_chat_fn(messages, max_tokens=1024):
            raise RuntimeError("LLM 服务不可用")

        doc_a = self._make_doc("A", "内容A")
        doc_b = self._make_doc("B", "内容B")
        assert _call_llm(doc_a, doc_b, fake_chat_fn) == "同主题"
