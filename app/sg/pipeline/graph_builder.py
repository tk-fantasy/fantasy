"""图谱构建与导出 — 从 kg-pipeline 移植，去掉 Neo4j 依赖，默认含 rule 边。

修复 numpy float32 不可 JSON 序列化的问题。
visual_sources 默认含 ("llm_neighbor", "embedding", "rule")，让结构化边也进图。
"""
import json
import pathlib
from collections import defaultdict

from .parser import Document


def _to_native(v):
    """递归把 numpy 类型转成 Python 原生类型，确保 JSON 可序列化。"""
    import numpy as np
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, dict):
        return {k: _to_native(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_native(x) for x in v]
    return v


class GraphBuilder:
    """构建图谱数据并导出为 graph.json。

    Args:
        graph_export_path: graph.json 输出路径
    """

    def __init__(self, graph_export_path: str) -> None:
        self.graph_export_path = graph_export_path

    def build(self, docs: list[Document],
              edges: list, coords_3d: dict | None = None):
        print("  Exporting JSON...")
        self._export_json(docs, edges, coords_3d=coords_3d)

    def _export_json(self, docs, edges,
                     coords_3d: dict | None = None,
                     visual_sources: tuple = ("llm_neighbor", "embedding", "rule")):
        graph_data = self._build_graph_data(
            docs, edges,
            coords_3d=coords_3d,
            visual_sources=visual_sources,
        )
        out = pathlib.Path(self.graph_export_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # 序列化前统一转原生类型，避免 numpy float32 报错
        graph_data = _to_native(graph_data)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, ensure_ascii=False, indent=2)
        print(f"  Exported graph to {out} "
              f"({len(graph_data['nodes'])} nodes, {len(graph_data['links'])} edges)")

    def _build_graph_data(self, docs, edges,
                          coords_3d: dict | None = None,
                          visual_sources: tuple = ("llm_neighbor", "embedding", "rule")):
        nodes = []
        seen_ids = set()
        for doc in docs:
            x, y, z = (0.0, 0.0, 0.0)
            if coords_3d and doc.id in coords_3d:
                x, y, z = coords_3d[doc.id]
            node = {
                "id": doc.id,
                "name": doc.title or doc.id,
                "type": "Document",
                "category": doc.category,
                "subcategory": doc.subcategory or "",
                "val": 3,
                "x": x, "y": y, "z": z,
            }
            nodes.append(node)
            seen_ids.add(doc.id)

        merged = defaultdict(list)
        for src, tgt, rtype, weight, source in edges:
            # 只保留两端都是文档节点的边（过滤掉 category:xxx 虚拟节点）
            if src not in seen_ids or tgt not in seen_ids:
                continue
            if source not in visual_sources:
                continue
            key = (src, tgt) if src < tgt else (tgt, src)
            merged[key].append((src, tgt, rtype, weight, source))

        links = []
        for (src, tgt), items in merged.items():
            rtypes = list(dict.fromkeys(it[2] for it in items))
            first = items[0]
            links.append({
                "source": first[0],
                "target": first[1],
                "relation": " | ".join(rtypes),
                "weight": first[3],
                "source_type": first[4],
            })

        return {"nodes": nodes, "links": links}
