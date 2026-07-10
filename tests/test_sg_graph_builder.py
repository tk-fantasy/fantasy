"""Tests for app/sg/pipeline/graph_builder.py — 图谱构建与导出。"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.sg.pipeline.graph_builder import GraphBuilder, _to_native
from app.sg.pipeline.parser import Document


def _make_doc(doc_id, title=None, category="安装", subcategory=None):
    return Document(
        id=doc_id,
        title=title or doc_id,
        category=category,
        subcategory=subcategory,
        filepath=f"docs/{doc_id}.md",
    )


class TestToNative:
    def test_numpy_float(self):
        assert _to_native(np.float32(0.5)) == 0.5
        assert isinstance(_to_native(np.float32(0.5)), float)

    def test_numpy_int(self):
        assert _to_native(np.int32(3)) == 3
        assert isinstance(_to_native(np.int32(3)), int)

    def test_numpy_array(self):
        assert _to_native(np.array([1.0, 2.0])) == [1.0, 2.0]

    def test_nested_dict(self):
        data = {"a": np.float32(0.1), "b": [np.int32(1), np.int32(2)]}
        result = _to_native(data)
        assert result["a"] == pytest.approx(0.1)
        assert result["b"] == [1, 2]

    def test_passthrough_native(self):
        assert _to_native("str") == "str"
        assert _to_native(42) == 42
        assert _to_native(None) is None


class TestGraphBuilder:
    def test_build_writes_valid_json(self, tmp_path):
        """build() 应写出可解析的 graph.json，含 nodes 和 links 两个键。"""
        out = tmp_path / "graph.json"
        docs = [_make_doc("A", "文档A"), _make_doc("B", "文档B")]
        edges = [("A", "B", "语义相似", 0.9, "embedding")]
        builder = GraphBuilder(str(out))
        builder.build(docs, edges=edges)

        data = json.loads(out.read_text(encoding="utf-8"))
        assert "nodes" in data
        assert "links" in data
        assert len(data["nodes"]) == 2

    def test_node_structure(self, tmp_path):
        out = tmp_path / "graph.json"
        docs = [_make_doc("A", "文档A", category="安装", subcategory="Docker")]
        builder = GraphBuilder(str(out))
        builder.build(docs, edges=[])

        data = json.loads(out.read_text(encoding="utf-8"))
        node = data["nodes"][0]
        assert node["id"] == "A"
        assert node["name"] == "文档A"
        assert node["type"] == "Document"
        assert node["category"] == "安装"
        assert node["subcategory"] == "Docker"
        assert "x" in node and "y" in node and "z" in node

    def test_coords_3d_applied(self, tmp_path):
        """coords_3d 坐标应写入节点的 x/y/z。"""
        out = tmp_path / "graph.json"
        docs = [_make_doc("A"), _make_doc("B")]
        coords = {"A": (1.5, 2.5, 3.5), "B": (3.0, 4.0, 5.0)}
        builder = GraphBuilder(str(out))
        builder.build(docs, edges=[], coords_3d=coords)

        data = json.loads(out.read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in data["nodes"]}
        assert nodes["A"]["x"] == 1.5
        assert nodes["A"]["y"] == 2.5
        assert nodes["A"]["z"] == 3.5

    def test_numpy_coords_serialized(self, tmp_path):
        """numpy 坐标应被转成原生类型，不报 JSON 序列化错误。"""
        out = tmp_path / "graph.json"
        docs = [_make_doc("A")]
        coords = {"A": (np.float32(1.1), np.float32(2.2), np.float32(3.3))}
        builder = GraphBuilder(str(out))
        builder.build(docs, edges=[], coords_3d=coords)

        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["nodes"][0]["x"] == pytest.approx(1.1)
        assert isinstance(data["nodes"][0]["x"], float)
        assert data["nodes"][0]["z"] == pytest.approx(3.3)

    def test_edges_filtered_by_visual_sources(self, tmp_path):
        """不在 visual_sources 里的边不进图。"""
        out = tmp_path / "graph.json"
        docs = [_make_doc("A"), _make_doc("B")]
        # rule 边默认在 visual_sources 里
        edges = [("A", "B", "同组", 0.8, "rule")]
        builder = GraphBuilder(str(out))
        builder.build(docs, edges=edges)

        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data["links"]) == 1
        assert data["links"][0]["source_type"] == "rule"

    def test_virtual_node_edges_filtered(self, tmp_path):
        """指向 category:xxx 虚拟节点的边被过滤（端点不在 docs 中）。"""
        out = tmp_path / "graph.json"
        docs = [_make_doc("A"), _make_doc("B")]
        edges = [
            ("A", "category:安装", "属于", 1.0, "rule"),  # 虚拟节点，应过滤
            ("A", "B", "同组", 0.8, "rule"),
        ]
        builder = GraphBuilder(str(out))
        builder.build(docs, edges=edges)

        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data["links"]) == 1
        assert data["links"][0]["source"] == "A"
        assert data["links"][0]["target"] == "B"

    def test_merged_parallel_edges(self, tmp_path):
        """同一对节点的多条边合并为一条，relation 用 | 连接。"""
        out = tmp_path / "graph.json"
        docs = [_make_doc("A"), _make_doc("B")]
        edges = [
            ("A", "B", "语义相似", 0.9, "embedding"),
            ("A", "B", "前置依赖", 0.7, "llm_neighbor"),
        ]
        builder = GraphBuilder(str(out))
        builder.build(docs, edges=edges)

        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data["links"]) == 1
        link = data["links"][0]
        assert "语义相似" in link["relation"]
        assert "前置依赖" in link["relation"]
