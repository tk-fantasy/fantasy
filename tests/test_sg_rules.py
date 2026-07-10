"""Tests for app/sg/pipeline/rules.py — 规则连边（纯算法，无外部依赖）。"""
from __future__ import annotations

from app.sg.pipeline.parser import Document, Section
from app.sg.pipeline.rules import apply_rules


def _make_doc(doc_id, category, subcategory=None, entities=None):
    """构造测试用 Document。"""
    sections = []
    if entities:
        sections.append(Section(heading="实体", content="", entities=entities))
    return Document(
        id=doc_id,
        title=doc_id,
        category=category,
        subcategory=subcategory,
        filepath=f"docs/{category}/{doc_id}.md",
        sections=sections,
    )


class TestApplyRules:
    def test_category_edges(self):
        """每个文档应连一条「属于」边到分类虚拟节点。"""
        docs = [_make_doc("A", "安装")]
        edges = apply_rules(docs)
        cat_edges = [e for e in edges if e[2] == "属于" and e[1] == "category:安装"]
        assert len(cat_edges) == 1
        assert cat_edges[0][4] == "rule"

    def test_subcategory_edges(self):
        """有子分类的文档应连到 subcategory 虚拟节点。"""
        docs = [_make_doc("A", "安装", subcategory="Docker")]
        edges = apply_rules(docs)
        sub_edges = [e for e in edges if e[1] == "subcategory:Docker"]
        assert len(sub_edges) == 1

    def test_same_group_edges(self):
        """同分类+子分类的文档两两连「同组」边。"""
        docs = [
            _make_doc("A", "安装", subcategory="Docker"),
            _make_doc("B", "安装", subcategory="Docker"),
        ]
        edges = apply_rules(docs)
        group_edges = [e for e in edges if e[2] == "同组"]
        assert len(group_edges) == 1
        assert {group_edges[0][0], group_edges[0][1]} == {"A", "B"}
        assert group_edges[0][3] == 0.8

    def test_different_groups_no_same_group_edge(self):
        """不同子分类的文档不连「同组」边。"""
        docs = [
            _make_doc("A", "安装", subcategory="Docker"),
            _make_doc("B", "安装", subcategory="源码"),
        ]
        edges = apply_rules(docs)
        group_edges = [e for e in edges if e[2] == "同组"]
        assert len(group_edges) == 0

    def test_entity_cooccurrence_edges(self):
        """共享 ≥2 个实体的文档对连「共享实体」边。"""
        docs = [
            _make_doc("A", "安装", entities=["Docker", "MySQL", "Redis"]),
            _make_doc("B", "安装", entities=["Docker", "MySQL", "Nginx"]),
        ]
        edges = apply_rules(docs)
        cooccur = [e for e in edges if e[2] == "共享实体"]
        assert len(cooccur) == 1
        # 共享 2 个实体 → weight = 2/5 = 0.4
        assert cooccur[0][3] == 0.4

    def test_no_cooccurrence_below_threshold(self):
        """仅共享 1 个实体的文档对不连边。"""
        docs = [
            _make_doc("A", "安装", entities=["Docker"]),
            _make_doc("B", "安装", entities=["Docker"]),
        ]
        edges = apply_rules(docs)
        cooccur = [e for e in edges if e[2] == "共享实体"]
        assert len(cooccur) == 0

    def test_empty_docs(self):
        assert apply_rules([]) == []

    def test_all_edges_are_rule_source(self):
        """所有规则边的 source 标记为 'rule'。"""
        docs = [
            _make_doc("A", "安装", subcategory="Docker", entities=["X", "Y"]),
            _make_doc("B", "安装", subcategory="Docker", entities=["X", "Y"]),
        ]
        edges = apply_rules(docs)
        assert all(e[4] == "rule" for e in edges)
