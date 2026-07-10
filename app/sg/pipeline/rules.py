"""规则连边 — 从 kg-pipeline 移植，零改造（纯算法，无外部依赖）。

基于文档元数据（分类、子分类、共享实体）连边，不依赖向量或 LLM。
"""
from itertools import combinations
from typing import Iterator

from .parser import Document


Relation = tuple[str, str, str, float, str]


def apply_rules(docs: list[Document]) -> list[Relation]:
    """根据文档元数据生成规则边。"""
    edges: list[Relation] = []
    edges.extend(_category_edges(docs))
    edges.extend(_same_group_edges(docs))
    edges.extend(_entity_cooccurrence_edges(docs))
    return edges


def _category_edges(docs: list[Document]) -> Iterator[Relation]:
    """文档 → 分类虚拟节点（3D 球不显示，仅作结构记录）。"""
    for doc in docs:
        yield (doc.id, f"category:{doc.category}", "属于", 1.0, "rule")
        if doc.subcategory:
            yield (doc.id, f"subcategory:{doc.subcategory}", "属于", 1.0, "rule")


def _same_group_edges(docs: list[Document]) -> Iterator[Relation]:
    """同一分类+子分类下的文档两两连边。"""
    groups: dict[str, list[Document]] = {}
    for doc in docs:
        key = f"{doc.category}/{doc.subcategory or ''}"
        groups.setdefault(key, []).append(doc)

    for group in groups.values():
        if len(group) < 2:
            continue
        for a, b in combinations(group, 2):
            yield (a.id, b.id, "同组", 0.8, "rule")


def _entity_cooccurrence_edges(docs: list[Document]) -> Iterator[Relation]:
    """共享 ≥2 个实体的文档对连边。"""
    doc_entities: dict[str, set[str]] = {}
    for doc in docs:
        entities = set()
        for sec in doc.sections:
            entities.update(sec.entities)
        doc_entities[doc.id] = entities

    for a_id, b_id in combinations(doc_entities.keys(), 2):
        shared = doc_entities[a_id] & doc_entities[b_id]
        if len(shared) >= 2:
            weight = min(1.0, len(shared) / 5.0)
            yield (a_id, b_id, "共享实体", weight, "rule")
