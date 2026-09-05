"""sg/rag 模块覆盖补齐 — 针对 pipeline、服务层与路由的未覆盖分支。

边界 mock 原则：LLM/embed 调用在客户端边界用假客户端（async 协程）mock，
HTTP 完全不外发；PCA/UMAP/FAISS 用真实本地实现（小数据量、少迭代轮数）。
每个测试都断言真实行为（返回结构、索引规模、边列表、落盘产物、路由 JSON）。
"""
from __future__ import annotations

import asyncio
import json
import os
import pickle
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.core.loop_utils import LoopUnavailableError
from app.sg.pipeline.entity_extractor import EntityExtractor
from app.sg.pipeline.graph_builder import GraphBuilder
from app.sg.pipeline.parser import Document, Section, load_index, parse_all
from app.sg.pipeline.relation_analyzer import analyze_neighbor_pairs
from app.sg.pipeline.vectorizer import Vectorizer
from app.services.rag_service import RagService
from app.services.sg_service import SemanticGraphService


# ══════════════════════════════ vectorizer ══════════════════════════════

def _fake_embed_fn(dim: int = 8):
    """确定性假 embedding：文本字节决定向量内容（全正值 → 余弦 > 0）。"""

    def embed_fn(texts):
        out = []
        for t in texts:
            v = np.zeros(dim, dtype=np.float32)
            for i, b in enumerate(t.encode("utf-8")):
                v[i % dim] += float((b % 13) + 1)
            out.append(v.tolist())
        return out

    return embed_fn


def _mk_doc(doc_id: str, sections=None) -> Document:
    return Document(
        id=doc_id, title=f"标题{doc_id}", category="分类", subcategory=None,
        filepath=f"{doc_id}.md", sections=sections or [], raw_text=f"{doc_id} 正文",
    )


class TestVectorizerComputeDocVectors:
    def test_weighted_average_over_sections(self):
        """多段文档向量 = 段向量按段落长度加权平均；单段文档 = 段向量本身。"""
        docs = [
            _mk_doc("d1", [Section("A", "aa"), Section("B", "bbb")]),
            _mk_doc("d2", [Section("C", "cc")]),
        ]
        v = Vectorizer(max_paragraph_chars=100)
        result = v.compute_doc_vectors(docs, _fake_embed_fn())

        assert set(result) == {"d1", "d2"}
        assert v.doc_ids == ["d1", "d2"]
        emb = _fake_embed_fn()
        expected_d2 = np.array(emb(["C: cc"])[0], dtype=np.float32)
        np.testing.assert_allclose(result["d2"], expected_d2)

        v1 = np.array(emb(["A: aa"])[0], dtype=np.float32)
        v2 = np.array(emb(["B: bbb"])[0], dtype=np.float32)
        w1, w2 = len("aa"), len("bbb")
        expected_d1 = (v1 * w1 + v2 * w2) / (w1 + w2)
        np.testing.assert_allclose(result["d1"], expected_d1, rtol=1e-5)

    def test_empty_docs_returns_empty_dict(self):
        """无段落（docs 无 sections / 空列表）→ 返回 {}。"""
        v = Vectorizer()
        assert v.compute_doc_vectors([], _fake_embed_fn()) == {}
        result = v.compute_doc_vectors([_mk_doc("d")], _fake_embed_fn())
        assert result == {}

    def test_paragraph_truncation_and_progress(self):
        """段落文本按 max_paragraph_chars 截断；on_progress 收到 (done, total)。"""
        seen_texts = []

        def embed_fn(texts):
            seen_texts.extend(texts)
            return [np.ones(4, dtype=np.float32).tolist() for _ in texts]

        calls = []
        v = Vectorizer(max_paragraph_chars=5)
        docs = [_mk_doc("d", [Section("H", "0123456789")])]
        result = v.compute_doc_vectors(docs, embed_fn, on_progress=lambda d, t: calls.append((d, t)))
        assert set(result) == {"d"}
        # 截断：heading + ": " + 前 5 个字符
        assert seen_texts == ["H: 01234"]
        assert calls == [(1, 1)]


class TestVectorizerFitTransform:
    def _vectors(self, n=6, dim=8):
        emb = _fake_embed_fn(dim)
        return {f"d{i}": np.array(emb([f"第{i}篇文档内容{i * 7}"])[0], dtype=np.float32)
                for i in range(n)}

    def test_full_umap_path_builds_index(self):
        """多文档 → PCA + 真实 UMAP + FAISS 索引；近邻检索排除自身。"""
        v = Vectorizer(pca_dim=3, umap_n_components=3, umap_n_neighbors=2,
                       umap_min_dist=0.1, umap_n_epochs=20)
        v.fit_transform(self._vectors(6))

        assert v.pca is not None and v.umap_model is not None
        assert set(v.umap_vectors) == {f"d{i}" for i in range(6)}
        for vec in v.umap_vectors.values():
            assert vec.shape == (3,)
        assert v.faiss_index is not None and v.faiss_index.ntotal == 6

        # 近邻：低阈值返回全部其他文档，高阈值返回空
        neighbors = v.get_neighbors_above_threshold("d0", -1.0)
        assert len(neighbors) == 5
        assert all(did != "d0" for did, _ in neighbors)
        assert all(isinstance(s, float) for _, s in neighbors)
        assert v.get_neighbors_above_threshold("d0", 10.0) == []
        # 未知文档 id → 空
        assert v.get_neighbors_above_threshold("ghost", -1.0) == []

        # _faiss_search：top_k 超过 ntotal 时截断；分数降序
        scores, idxs = v._faiss_search(v.raw_vectors["d0"], 100)
        assert len(scores) == 6 and len(idxs) == 6
        assert scores[0] >= scores[-1]
        scores2, _ = v._faiss_search(v.raw_vectors["d0"], 2)
        assert len(scores2) == 2

        # 无索引 / 空索引 → []
        v2 = Vectorizer()
        assert v2.get_neighbors_above_threshold("d0", 0.0) == []

    def test_too_few_docs_falls_back_to_pca(self):
        """2 篇文档（umap_n < 2）→ 跳过 UMAP，3D 坐标取 PCA 前几维。"""
        v = Vectorizer(pca_dim=2, umap_n_components=3, umap_n_neighbors=15, umap_n_epochs=5)
        vecs = self._vectors(2, dim=4)
        v.fit_transform(vecs)

        assert v.umap_model is None
        for did in ("d0", "d1"):
            coord = v.umap_vectors[did]
            assert coord.shape == (3,)
            # 前两维来自 PCA，第三维补零
            np.testing.assert_allclose(coord[:2], v.pca_vectors[did][:2], atol=1e-6)
            assert coord[2] == 0.0
        # 仍然构建了 FAISS 索引
        assert v.faiss_index.ntotal == 2

    def test_no_docs_returns_without_index(self):
        """空输入 → 直接返回，不建索引。"""
        v = Vectorizer()
        v.fit_transform({})
        assert v.pca is None and v.faiss_index is None

    def test_get_3d_coords_known_and_unknown(self):
        v = Vectorizer(pca_dim=3, umap_n_components=3, umap_n_neighbors=2, umap_n_epochs=20)
        v.fit_transform(self._vectors(6))
        c = v.get_3d_coords("d0")
        assert len(c) == 3 and all(isinstance(x, float) for x in c)
        assert v.get_3d_coords("nope") == (0.0, 0.0, 0.0)

    def test_save_persists_models_and_faiss(self, tmp_path):
        """fit 后 save：PCA/UMAP 模型与 FAISS 索引全部落盘且可回读。"""
        import faiss
        import joblib

        v = Vectorizer(pca_dim=3, umap_n_components=3, umap_n_neighbors=2, umap_n_epochs=20)
        v.fit_transform(self._vectors(6))
        vectors_path = str(tmp_path / "vectors.pkl")
        model_dir = str(tmp_path / "models")
        faiss_path = str(tmp_path / "models" / "faiss.index")
        v.save(vectors_path, model_dir, faiss_path)

        pca = joblib.load(os.path.join(model_dir, "pca_model.joblib"))
        assert pca.n_components == 3
        umap_model = joblib.load(os.path.join(model_dir, "umap_model.joblib"))
        assert umap_model is not None
        idx = faiss.read_index(faiss_path)
        assert idx.ntotal == 6 and idx.d == 8


# ══════════════════════════════ relation_analyzer ══════════════════════════════

class _FakeNeighborVectorizer:
    def __init__(self, neighbors):
        self._neighbors = neighbors

    def get_neighbors_above_threshold(self, doc_id, threshold):
        return list(self._neighbors.get(doc_id, []))


def _ral_doc(doc_id: str) -> Document:
    return Document(
        id=doc_id, title=f"标题{doc_id}", category="分类", subcategory=None,
        filepath=f"{doc_id}.md", raw_text=f"{doc_id} 的正文内容用于摘要截断 " * 20,
    )


_OK = '{"relation_type": "前置依赖", "description": "x"}'
_NO_REL = '{"relation_type": "无明显关系", "description": "y"}'


class TestAnalyzeNeighborPairs:
    def test_fewer_than_two_docs_returns_empty(self):
        assert analyze_neighbor_pairs([_ral_doc("a")], _FakeNeighborVectorizer({}), 0.5,
                                      lambda m, max_tokens=1: _OK) == []

    def test_no_pairs_above_threshold_returns_empty(self):
        edges = analyze_neighbor_pairs(
            [_ral_doc("a"), _ral_doc("b")], _FakeNeighborVectorizer({}), 0.5,
            lambda m, max_tokens=1: _OK,
        )
        assert edges == []

    def test_edges_dedup_progress_and_persistence(self, tmp_path):
        """双向邻居去重成一对；边带 llm_neighbor 标记；进度文件与回调均产出。"""
        neighbors = {"a": [("b", 0.9), ("c", 0.8)], "b": [("a", 0.9)], "c": [("a", 0.8)]}
        docs = [_ral_doc("a"), _ral_doc("b"), _ral_doc("c")]
        calls = []
        progress = []

        def chat(messages, max_tokens=256):
            calls.append(1)
            return _OK

        edges = analyze_neighbor_pairs(
            docs, _FakeNeighborVectorizer(neighbors), 0.5, chat,
            max_workers=2, task_dir=tmp_path,
            on_progress=lambda done, total: progress.append((done, total)),
        )

        assert len(edges) == 2
        assert all(e[2] == "前置依赖" and e[4] == "llm_neighbor" for e in edges)
        assert {(e[0], e[1]) for e in edges} == {("a", "b"), ("a", "c")}
        assert {round(e[3], 4) for e in edges} == {0.9, 0.8}
        # 去重后只分析 2 对
        assert len(calls) == 2
        # 进度回调与断点文件
        assert progress[-1] == (2, 2)
        prog = json.loads((tmp_path / "llm_progress.json").read_text(encoding="utf-8"))
        assert sorted(prog) == ["a|b", "a|c"]
        assert all(v["rtype"] == "前置依赖" for v in prog.values())

    def test_no_relation_pairs_are_filtered(self, tmp_path):
        """LLM 判定无明显关系 → 不产出边，但断点仍记录 rtype=null。"""
        neighbors = {"a": [("b", 0.9)]}
        docs = [_ral_doc("a"), _ral_doc("b")]

        def chat(messages, max_tokens=256):
            return _NO_REL

        edges = analyze_neighbor_pairs(docs, _FakeNeighborVectorizer(neighbors), 0.5,
                                       chat, task_dir=tmp_path)
        assert edges == []
        prog = json.loads((tmp_path / "llm_progress.json").read_text(encoding="utf-8"))
        assert prog["a|b"] == {"rtype": None, "score": 0.9}

    def test_resume_from_progress_file_skips_llm(self, tmp_path):
        """断点续传：进度文件里已完成的对不再调 LLM，直接产出边。"""
        (tmp_path / "llm_progress.json").write_text(
            json.dumps({"a|b": {"rtype": "功能关联", "score": 0.1}}), encoding="utf-8")
        docs = [_ral_doc("a"), _ral_doc("b")]

        def chat(messages, max_tokens=256):
            raise AssertionError("缓存命中时不应再调 LLM")

        edges = analyze_neighbor_pairs(docs, _FakeNeighborVectorizer({"a": [("b", 0.9123)]}),
                                       0.5, chat, task_dir=tmp_path)
        assert edges == [("a", "b", "功能关联", 0.9123, "llm_neighbor")]

    def test_cached_none_rtype_yields_no_edge(self, tmp_path):
        """缓存里 rtype=null（此前判定无关联）→ 不产出边、不再调 LLM。"""
        (tmp_path / "llm_progress.json").write_text(
            json.dumps({"a|b": {"rtype": None, "score": 0.9}}), encoding="utf-8")
        docs = [_ral_doc("a"), _ral_doc("b")]
        edges = analyze_neighbor_pairs(docs, _FakeNeighborVectorizer({"a": [("b", 0.9)]}),
                                       0.5, lambda m, max_tokens=1: _OK, task_dir=tmp_path)
        assert edges == []

    def test_corrupt_progress_file_is_ignored(self, tmp_path):
        """断点文件损坏 → 忽略缓存，重新全量分析。"""
        (tmp_path / "llm_progress.json").write_text("not json {{{", encoding="utf-8")
        docs = [_ral_doc("a"), _ral_doc("b")]
        calls = []

        def chat(messages, max_tokens=256):
            calls.append(1)
            return _OK

        edges = analyze_neighbor_pairs(docs, _FakeNeighborVectorizer({"a": [("b", 0.9)]}),
                                       0.5, chat, task_dir=tmp_path)
        assert len(edges) == 1 and len(calls) == 1

    def test_print_path_without_progress_callback(self):
        """不传 on_progress 时走 done%20==0/done==total 的打印分支。"""
        docs = [_ral_doc("a"), _ral_doc("b")]
        edges = analyze_neighbor_pairs(docs, _FakeNeighborVectorizer({"a": [("b", 0.9)]}),
                                       0.5, lambda m, max_tokens=1: _OK)
        assert edges == [("a", "b", "前置依赖", 0.9, "llm_neighbor")]

    def test_batch_save_triggers_midway(self, tmp_path):
        """≥10 对未完成结果触发中途保存（batch_save=10）。"""
        ids = [f"d{i}" for i in range(6)]
        neighbors = {d: [(o, 0.9) for o in ids if o != d] for d in ids}
        docs = [_ral_doc(d) for d in ids]
        edges = analyze_neighbor_pairs(docs, _FakeNeighborVectorizer(neighbors), 0.5,
                                       lambda m, max_tokens=1: _OK,
                                       max_workers=4, task_dir=tmp_path)
        assert len(edges) == 15  # C(6,2)
        prog = json.loads((tmp_path / "llm_progress.json").read_text(encoding="utf-8"))
        assert len(prog) == 15

    def test_no_task_dir_still_works(self):
        """task_dir=None → 不写断点文件，边照常返回。"""
        docs = [_ral_doc("a"), _ral_doc("b")]
        edges = analyze_neighbor_pairs(docs, _FakeNeighborVectorizer({"a": [("b", 0.9)]}),
                                       0.5, lambda m, max_tokens=1: _OK, task_dir=None)
        assert len(edges) == 1


# ══════════════════════════════ sg_service ══════════════════════════════

class FakeEmbedClient:
    model = "test-embed"

    def __init__(self, dim=8, delay=0.0):
        self.dim = dim
        self.delay = delay
        self.calls = []

    async def post_embedding(self, payload):
        self.calls.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        v = np.zeros(self.dim, dtype=np.float32)
        for i, b in enumerate(payload["prompt"].encode("utf-8")):
            v[i % self.dim] += float((b % 13) + 1)
        return {"embedding": v.tolist()}


class FakeChatClient:
    def __init__(self, delay=0.0, pair_delay=0.0):
        self.delay = delay            # 实体抽取类调用的延迟
        self.pair_delay = pair_delay  # 邻居关系类调用的延迟
        self.calls = []

    async def chat(self, messages, timeout=120):
        self.calls.append(messages)
        content = messages[-1]["content"]
        wait = self.pair_delay if "实质关联" in content else self.delay
        if wait:
            await asyncio.sleep(wait)
        if "实质关联" in content:  # relation_analyzer.PAIR_PROMPT
            return '{"relation_type": "功能关联", "description": "配合使用"}'
        if "提取实体" in content:  # entity_extractor.SINGLE_PROMPT
            return json.dumps({"entities": [{"name": "测试实体", "type": "概念"}],
                               "relations": []})
        return "{}"


def _sg_docs(tmp_path, n=6):
    """在子目录下生成 n 篇文档（parser 不会解析 docs 根级 .md，见报告中的源码 bug）。"""
    docs = tmp_path / "docs"
    cat = docs / "分类"
    cat.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (cat / f"doc{i}.md").write_text(
            f"# 文档{i}\n## 第一节\n文档{i}的内容 共同关键词 设备\n## 第二节\n更多内容 {i}",
            encoding="utf-8",
        )
    return docs


def _sg_ready_config(_patch_config):
    _patch_config["llm_keys"] = [
        {"type": "embed", "model": "test-embed", "api_key_env": "SG_EMBED_KEY"},
        {"type": "chat", "model": "test-chat"},
    ]
    _patch_config["sg"] = {
        "threshold": 0.0, "pca_dim": 4, "umap_n_components": 3,
        "umap_n_neighbors": 2, "umap_min_dist": 0.1, "umap_n_epochs": 20,
        "max_workers": 2, "max_paragraph_chars": 200,
    }


async def _wait_terminal(svc, timeout=60.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if svc.status in ("done", "error", "idle"):
            await asyncio.sleep(0.05)  # 等 _on_done 回调与状态写入稳定
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"构建未在 {timeout}s 内结束: {svc.snapshot()}")


async def _wait_until(cond, timeout=30.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


class TestSemanticGraphServiceBuild:
    def _patch_output_root(self, monkeypatch, tmp_path):
        """构建产物重定向到 tmp_path，避免污染真实 app/sg/output。"""
        out = tmp_path / "sg_output"
        monkeypatch.setattr("app.services.sg_service.OUTPUT_ROOT", out)
        return out

    async def test_build_success_end_to_end(self, tmp_path, monkeypatch, _patch_config):
        """完整 5 步流水线：解析→向量化→实体→邻居分析→导出，产物全部落盘。"""
        self._patch_output_root(monkeypatch, tmp_path)
        monkeypatch.setenv("DOCS_ROOT", str(_sg_docs(tmp_path, n=6)))
        _sg_ready_config(_patch_config)
        embed, chat = FakeEmbedClient(), FakeChatClient()
        svc = SemanticGraphService(embed, chat)
        svc.bind_loop(asyncio.get_running_loop())

        snap = await svc.build_async()
        assert snap["status"] == "running"
        assert snap["task_dir"] and os.path.isdir(snap["task_dir"])

        await _wait_terminal(svc)
        assert svc.status == "done"
        assert svc.progress == 100
        assert svc.message == "构建完成"

        # graph.json：6 节点；每对文档同时有 rule 边与 llm 邻居边，合并后 relation 含两类
        graph = json.loads((svc.task_dir / "graph.json").read_text(encoding="utf-8"))
        assert len(graph["nodes"]) == 6
        assert graph["links"], "threshold=0 时应产出邻居边"
        assert any("功能关联" in link["relation"] for link in graph["links"])

        # 其余产物
        assert (svc.task_dir / "vectors.pkl").exists()
        assert (svc.task_dir / "models" / "faiss.index").exists()
        prog = json.loads((svc.task_dir / "llm_progress.json").read_text(encoding="utf-8"))
        assert len(prog) == 15  # C(6,2) 对全部分析过

        # 回调桥确实被使用（embed 逐条、chat 实体+关系两类 prompt）
        assert len(embed.calls) >= 12  # 每段一次
        assert len(chat.calls) == 6 + 15
        assert embed.calls[0]["model"] == "test-embed"

    async def test_build_not_configured_error(self, _patch_config):
        """未配置 embed/chat key → 立即 error，不启动构建线程。"""
        _patch_config["llm_keys"] = []
        svc = SemanticGraphService(MagicMock(), MagicMock())
        snap = await svc.build_async()
        assert snap["status"] == "error"
        assert "未配置" in snap["message"]

    async def test_build_docs_root_missing(self, tmp_path, monkeypatch, _patch_config):
        """docs 目录不存在 → error。"""
        _sg_ready_config(_patch_config)
        monkeypatch.setenv("DOCS_ROOT", str(tmp_path / "no_docs"))
        svc = SemanticGraphService(FakeEmbedClient(), FakeChatClient())
        snap = await svc.build_async()
        assert snap["status"] == "error"
        assert "docs 目录不存在" in snap["message"]

    async def test_build_failure_marks_error(self, tmp_path, monkeypatch, _patch_config):
        """构建线程抛异常（空 docs）→ _on_done 捕获并置 error。"""
        self._patch_output_root(monkeypatch, tmp_path)
        _sg_ready_config(_patch_config)
        empty = tmp_path / "empty_docs"
        empty.mkdir()
        monkeypatch.setenv("DOCS_ROOT", str(empty))
        svc = SemanticGraphService(FakeEmbedClient(), FakeChatClient())
        svc.bind_loop(asyncio.get_running_loop())

        snap = await svc.build_async()
        assert snap["status"] == "running"
        await _wait_terminal(svc)
        assert svc.status == "error"
        assert svc.message.startswith("构建失败")

    async def test_busy_when_already_running(self):
        """已有任务运行时再次触发 → 返回 error 而不启动新任务。"""
        svc = SemanticGraphService(MagicMock(), MagicMock())
        svc.status = "running"
        snap = await svc.build_async()
        assert "error" in snap
        assert "已有构建任务在运行" in snap["error"]

    async def test_cancel_midway_returns_idle(self, tmp_path, monkeypatch, _patch_config):
        """构建中途取消：不产出 graph.json，最终状态 idle/已取消。"""
        self._patch_output_root(monkeypatch, tmp_path)
        monkeypatch.setenv("DOCS_ROOT", str(_sg_docs(tmp_path, n=6)))
        _sg_ready_config(_patch_config)
        svc = SemanticGraphService(FakeEmbedClient(delay=0.05), FakeChatClient())
        svc.bind_loop(asyncio.get_running_loop())

        await svc.build_async()
        svc.cancel()
        await _wait_terminal(svc)
        assert svc.status == "idle"
        assert svc.message == "已取消"
        assert svc.progress < 100
        assert svc.task_dir is not None
        # 向量化在取消检查点之前完成，但流程在 Step 3 前中止 → 无 graph.json
        assert (svc.task_dir / "vectors.pkl").exists()
        assert not (svc.task_dir / "graph.json").exists()

    async def test_cancel_during_entity_stage(self, tmp_path, monkeypatch, _patch_config):
        """Step 3（实体抽取）进行中取消 → 在 Step 3 检查点中止，不进入邻居分析。"""
        self._patch_output_root(monkeypatch, tmp_path)
        monkeypatch.setenv("DOCS_ROOT", str(_sg_docs(tmp_path, n=6)))
        _sg_ready_config(_patch_config)
        chat = FakeChatClient(delay=0.05)  # 放慢实体抽取
        svc = SemanticGraphService(FakeEmbedClient(), chat)
        svc.bind_loop(asyncio.get_running_loop())

        await svc.build_async()
        await _wait_until(lambda: len(chat.calls) >= 1)  # 实体抽取已开始
        svc.cancel()
        await _wait_terminal(svc)
        assert svc.status == "idle" and svc.message == "已取消"
        assert not (svc.task_dir / "graph.json").exists()
        assert not (svc.task_dir / "llm_progress.json").exists()  # 未进入 Step 4

    async def test_cancel_during_relation_stage(self, tmp_path, monkeypatch, _patch_config):
        """Step 4（邻居分析）进行中取消 → 在 Step 4 检查点中止，不导出 graph。"""
        self._patch_output_root(monkeypatch, tmp_path)
        monkeypatch.setenv("DOCS_ROOT", str(_sg_docs(tmp_path, n=6)))
        _sg_ready_config(_patch_config)
        chat = FakeChatClient(pair_delay=0.05)  # 放慢邻居分析
        svc = SemanticGraphService(FakeEmbedClient(), chat)
        svc.bind_loop(asyncio.get_running_loop())

        await svc.build_async()
        # 等到至少一个 pair 分析请求发出后取消
        await _wait_until(lambda: any("实质关联" in c[-1]["content"] for c in chat.calls))
        svc.cancel()
        await _wait_terminal(svc)
        assert svc.status == "idle" and svc.message == "已取消"
        assert (svc.task_dir / "llm_progress.json").exists()  # Step 4 已运行
        assert not (svc.task_dir / "graph.json").exists()  # Step 5 被跳过

    async def test_binds_running_loop_implicitly(self, tmp_path, monkeypatch, _patch_config):
        """未显式 bind_loop 时 build_async 自动绑定当前运行中的循环。"""
        self._patch_output_root(monkeypatch, tmp_path)
        monkeypatch.setenv("DOCS_ROOT", str(_sg_docs(tmp_path, n=6)))
        _sg_ready_config(_patch_config)
        svc = SemanticGraphService(FakeEmbedClient(), FakeChatClient())
        assert svc._loop is None
        await svc.build_async()
        assert svc._loop is not None
        await _wait_terminal(svc)
        assert svc.status == "done"


class TestSemanticGraphServiceBridges:
    def test_latest_graph_skips_corrupt_newer_dir(self, tmp_path, monkeypatch):
        """损坏的 graph.json 若 mtime 更新：读取失败 → continue 到下一个产物目录。"""
        monkeypatch.setattr("app.services.sg_service.OUTPUT_ROOT", tmp_path)

        corrupt = tmp_path / "20260101_000000"
        corrupt.mkdir()
        (corrupt / "graph.json").write_text("not json {{{", encoding="utf-8")
        good = tmp_path / "20260102_000000"
        good.mkdir()
        (good / "graph.json").write_text(
            json.dumps({"nodes": [{"id": "ok"}], "links": []}), encoding="utf-8")
        # 显式 mtime 让 corrupt 排最前（默认排序 reverse=True），强制走 except→continue
        now = time.time()
        os.utime(corrupt, (now + 1000, now + 1000))
        os.utime(good, (now - 1000, now - 1000))

        graph, task_dir = SemanticGraphService.latest_graph()
        assert task_dir.name == "20260102_000000"
        assert graph["nodes"][0]["id"] == "ok"

    async def test_embed_fn_bridge(self):
        """_make_embed_fn：线程内逐条投递主循环，返回 embedding 列表。"""
        embed = FakeEmbedClient()
        svc = SemanticGraphService(embed, FakeChatClient())
        loop = asyncio.get_running_loop()
        svc.bind_loop(loop)

        embed_fn = svc._make_embed_fn()
        result = await loop.run_in_executor(None, embed_fn, ["甲文本", "乙文本"])
        assert len(result) == 2 and len(result[0]) == 8
        assert embed.calls[0]["model"] == "test-embed"
        assert embed.calls[0]["prompt"] == "甲文本"
        assert embed.calls[1]["prompt"] == "乙文本"

    async def test_chat_fn_bridge(self):
        """_make_chat_fn：messages 原样透传 chat 客户端（timeout=120）。"""
        chat = FakeChatClient()
        svc = SemanticGraphService(FakeEmbedClient(), chat)
        loop = asyncio.get_running_loop()
        svc.bind_loop(loop)

        chat_fn = svc._make_chat_fn()
        messages = [{"role": "user", "content": "你好"}]
        out = await loop.run_in_executor(None, chat_fn, messages)
        assert isinstance(out, str)
        assert chat.calls[0] is messages  # messages 原样透传

    def test_set_progress_cap_and_cancel_guard(self):
        """_set：进度封顶 100；取消后忽略更新。"""
        svc = SemanticGraphService(MagicMock(), MagicMock())
        svc._set(150, "x")
        assert svc.progress == 100
        assert svc.message == "x"
        svc._cancel = True
        svc._set(10, "y")
        assert svc.progress == 100
        assert svc.message == "x"


# ══════════════════════════════ rag_service ══════════════════════════════

class FakeEmbed:
    """可注入失败的假 embed 客户端（批量 + 单条）。"""

    def __init__(self, model="test-embed", dim=8, fail_if=None, exc=None,
                 fail_embedding=False, always_fail=False):
        self.model = model
        self.enabled = True
        self.dim = dim
        self._fail_if = fail_if
        self._exc = exc or RuntimeError("embed down")
        self._fail_embedding = fail_embedding
        self._always_fail = always_fail
        self.batch_calls = []

    async def post_embeddings_batch(self, texts, timeout=60):
        self.batch_calls.append(list(texts))
        if self._always_fail or (self._fail_if is not None and self._fail_if(texts)):
            raise self._exc
        return [[float((len(t) % 7) + 1)] * self.dim for t in texts]

    async def post_embedding(self, payload, timeout=60):
        if self._fail_embedding:
            raise self._exc
        v = [float((len(payload.get("prompt", "")) % 7) + 1)] * self.dim
        return {"embedding": v}


def _make_rag(tmp_path, monkeypatch, embed=None, data_is_file=False, docs="normal"):
    """构造绑定后台事件循环的 RagService；返回 (svc, stop_fn)。"""
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    if data_is_file:
        (app_dir / "data").write_text("I am a file", encoding="utf-8")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    if docs == "normal":
        (docs_dir / "a.md").write_text("# A\n\n## SA\n\n" + "x" * 80, encoding="utf-8")
        (docs_dir / "b.md").write_text("# B\n\n## SB\n\n" + "y" * 80, encoding="utf-8")
    elif docs == "thin":
        (docs_dir / "t.md").write_text("# 标题\n短", encoding="utf-8")
    monkeypatch.setenv("DOCS_ROOT", str(docs_dir))

    svc = RagService(base_dir=app_dir, embed_client=embed or FakeEmbed())
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    svc._loop = loop

    def stop():
        loop.call_soon_threadsafe(loop.stop)
        th.join(timeout=2)

    return svc, stop


@pytest.fixture
def rag(tmp_path, monkeypatch):
    svc, stop = _make_rag(tmp_path, monkeypatch)
    yield svc
    stop()


class TestRagServiceStatusAndGuards:
    def test_rebuild_status_snapshot(self, rag):
        st = rag.rebuild_status
        assert st == {"rebuilding": False, "total": 0, "done": 0, "errors": 0,
                      "message": "", "model": "", "chunk_count": 0}
        rag.safe_build()
        st2 = rag.rebuild_status
        assert st2["rebuilding"] is False
        assert st2["chunk_count"] == rag.chunk_count == 2
        assert st2["model"] == "test-embed"
        assert "重建完成" in st2["message"]
        assert st2["total"] == 2 and st2["done"] == 2

    def test_bind_loop(self, rag):
        loop = asyncio.new_event_loop()
        try:
            rag._loop = None
            rag.bind_loop(loop)
            assert rag._loop is loop
        finally:
            loop.close()

    def test_safe_build_retries_then_gives_up(self, rag, monkeypatch):
        """build_index 连续失败 → 重试 3 次（sleep 被 patch 以加速）后放弃。"""
        monkeypatch.setattr("time.sleep", lambda s: None)
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("boom")

        monkeypatch.setattr(rag, "build_index", boom)
        rag.safe_build()
        assert len(calls) == 3
        assert rag._rebuild_message == "重建失败，已重试 3 次"
        assert rag._rebuilding is False

    def test_try_load_corrupt_meta_falls_back(self, rag):
        """meta.json 损坏 → 加载失败回退 False（不抛异常）。"""
        rag.safe_build()
        assert rag.try_load() is True
        (rag._index_dir / "meta.json").write_text("not json {{{", encoding="utf-8")
        assert rag.try_load() is False

    def test_try_load_without_embed_client(self, rag):
        """产物文件齐全但 embed_client 为 None → False。"""
        rag.safe_build()
        svc2 = RagService(base_dir=rag._base_dir, embed_client=None)
        assert svc2._index_dir.exists()  # 文件确实齐全
        assert svc2.try_load() is False

    def test_fingerprint_empty_when_docs_root_missing(self, rag, tmp_path, monkeypatch):
        """docs 目录不存在 → 指纹为空 dict。"""
        monkeypatch.setenv("DOCS_ROOT", str(tmp_path / "no_docs"))
        assert rag._compute_docs_fingerprint() == {}

    def test_fingerprint_tracks_md_files(self, rag):
        """docs 下的 .md 文件按相对路径记录 [size, mtime_ns]。"""
        fp = rag._compute_docs_fingerprint()
        assert set(fp) == {"a.md", "b.md"}
        for size, mtime in fp.values():
            assert size > 0 and mtime > 0

    def test_maybe_rebuild_noop_cases(self, rag):
        rag.maybe_rebuild_if_model_changed()  # 无 _embed_model → no-op
        assert rag._rebuilding is False
        rag._embed_model = "test-embed"
        rag.maybe_rebuild_if_model_changed()  # 模型一致 → no-op
        assert rag._rebuilding is False

    def test_maybe_rebuild_skips_when_rebuilding(self, rag):
        rag._rebuilding = True
        rag._embed_model = "old-model"
        rag.maybe_rebuild_if_model_changed()
        assert rag._rebuilding is True  # 并发闸生效，未重置

    def test_maybe_rebuild_loop_unbound_resets_flag(self, rag):
        rag._embed_model = "old-model"
        rag._loop = None
        rag.maybe_rebuild_if_model_changed()
        assert rag._rebuilding is False

    def test_maybe_rebuild_schedules_safe_build(self, rag, monkeypatch):
        rag._embed_model = "old-model"
        mock_sb = MagicMock()
        monkeypatch.setattr(rag, "safe_build", mock_sb)
        rag.maybe_rebuild_if_model_changed()
        for _ in range(300):
            if mock_sb.called:
                break
            time.sleep(0.01)
        assert mock_sb.called


class TestRagServiceBuildIndexGuards:
    def test_no_embed_client(self, rag):
        rag._embed_client = None
        rag.build_index()
        assert rag.is_ready is False

    def test_disabled_embed_client(self, rag):
        rag._embed_client.enabled = False
        rag.build_index()
        assert rag.is_ready is False

    def test_docs_missing(self, rag, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCS_ROOT", str(tmp_path / "nope"))
        rag.build_index()
        assert rag.is_ready is False

    def test_no_chunks_in_docs(self, tmp_path, monkeypatch):
        svc, stop = _make_rag(tmp_path, monkeypatch, docs="thin")
        try:
            svc.build_index()
            assert svc.is_ready is False
            assert svc._rebuild_message == "未找到文档内容"
        finally:
            stop()


class TestRagServiceEmbedFailures:
    def test_all_batches_fail(self, rag):
        """每批 embed 都失败 → 跳过全部 chunk，索引不构建。"""
        rag._embed_client = FakeEmbed(fail_if=lambda texts: True)
        rag.build_index()
        assert rag.is_ready is False
        assert rag._rebuild_message == "向量化全部失败，请检查 embed 配置"
        assert rag._rebuild_errors == rag._rebuild_total

    def test_loop_unavailable_skips_retry_and_fails_fast(self, rag):
        """循环不可用 → 不重试（仅 1 次调用），异常转为 RuntimeError 后按批跳过。"""
        rag._embed_client = FakeEmbed(exc=LoopUnavailableError("dead"), always_fail=True)
        rag.build_index()
        assert rag.is_ready is False
        assert len(rag._embed_client.batch_calls) == 1  # LoopUnavailableError 不重试
        assert rag._rebuild_message == "向量化全部失败，请检查 embed 配置"

    def test_timeout_retries_three_times_then_skips_batch(self, rag):
        """TimeoutError → 重试 3 次后放弃该批（1 批 → 共 3 次调用）。"""
        rag._embed_client = FakeEmbed(exc=TimeoutError("slow"), always_fail=True)
        rag.build_index()
        assert rag.is_ready is False
        assert len(rag._embed_client.batch_calls) == 3
        assert rag._rebuild_total == 2

    def test_partial_batch_failure_keeps_good_chunks(self, tmp_path, monkeypatch):
        """部分批次失败：好 chunk 保留进索引，坏 chunk 计入 errors。"""
        svc, stop = _make_rag(tmp_path, monkeypatch, embed=FakeEmbed(), docs="none")
        try:
            docs_dir = tmp_path / "docs"
            good = "\n".join(f"## 第{i}节\n\n" + "g" * 60 for i in range(20))
            bad = "\n".join(f"## 失败{i}节\n\n" + "FAILMARK" + "b" * 60 for i in range(20))
            (docs_dir / "good.md").write_text("# 好\n" + good, encoding="utf-8")
            (docs_dir / "bad.md").write_text("# 坏\n" + bad, encoding="utf-8")
            svc._embed_client = FakeEmbed(
                fail_if=lambda texts: any("FAILMARK" in t for t in texts))
            svc.build_index()

            assert svc.is_ready
            assert len(svc.chunks) + svc._rebuild_errors == 40
            assert all("FAILMARK" not in c for c in svc.chunks)
            assert svc.faiss_index.ntotal == len(svc.chunks)
            assert svc._rebuild_errors > 0
        finally:
            stop()

    def test_persist_failure_does_not_break_build(self, tmp_path, monkeypatch):
        """落盘失败（data 是文件）→ 只告警，内存索引仍可用。"""
        svc, stop = _make_rag(tmp_path, monkeypatch, data_is_file=True)
        try:
            svc.build_index()
            assert svc.is_ready
            assert not (svc._base_dir / "data" / "rag_index").exists()
        finally:
            stop()


class TestRagServiceSearch:
    async def test_not_ready_returns_empty(self, rag):
        assert await rag.search("问题") == ""

    async def test_returns_joined_chunks(self, rag):
        rag.safe_build()
        assert rag.is_ready and len(rag.chunks) == 2
        ctx = await rag.search("x" * 80)
        # 只有 2 个 chunk，top5 全部返回（按相似度排序，比较集合）
        assert ctx.split("\n\n---\n\n") and sorted(ctx.split("\n\n---\n\n")) == sorted(rag.chunks)

    async def test_dim_mismatch_returns_empty(self, rag):
        """query 维度与索引不一致（模型已变更）→ 返回空串而非崩溃。"""
        rag.safe_build()
        rag._embed_client = FakeEmbed(dim=4)
        assert await rag.search("x" * 80) == ""

    async def test_embed_failure_returns_empty(self, rag):
        rag.safe_build()
        rag._embed_client = FakeEmbed(fail_embedding=True)
        assert await rag.search("x" * 80) == ""

    async def test_loop_unavailable_returns_empty(self, rag):
        """主循环已解绑 → submit_and_wait 失败 → 返回空串。"""
        rag.safe_build()
        rag._loop = None
        assert await rag.search("x" * 80) == ""


class TestRagServiceBuildLlmClient:
    async def test_global_fallback_from_llm_keys(self, rag, monkeypatch):
        monkeypatch.setenv("RAG_TEST_CHAT_KEY", "sk-rag-global")
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [{"type": "chat", "api_key_env": "RAG_TEST_CHAT_KEY",
                          "base_url": "http://rag-test:8080/v1", "model": "glm-4-flash"}],
        })
        client, model = await rag.build_llm_client()
        assert model == "glm-4-flash"
        assert client.api_key == "sk-rag-global"
        assert str(client.base_url).rstrip("/") == "http://rag-test:8080/v1"

    async def test_no_keys_openai_rejects_empty_key(self, rag):
        """已知问题：完全无 chat 配置时 chat_key 为空串，openai 客户端构造抛 OpenAIError。"""
        import openai
        with pytest.raises(openai.OpenAIError):
            await rag.build_llm_client()

    async def test_per_user_config_caching_and_rebuild(self, rag, monkeypatch):
        """per-user key 优先；按签名缓存；签名变化时关旧建新；loader 异常回退全局。"""
        import app.core.config as cfg
        import app.agents.langgraph_agent as lga

        # 全局回退配置（loader 失败/返回 None 时使用）
        monkeypatch.setenv("RAG_TEST_CHAT_KEY", "sk-rag-global")
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [{"type": "chat", "api_key_env": "RAG_TEST_CHAT_KEY",
                          "base_url": "http://global:8080/v1", "model": "glm-4-flash"}],
        })

        holder = {"cfg": {"api_key": "sk-u1", "base_url": "http://u1/v1",
                          "model": "user-model"}}

        async def fake_loader(user_id):
            if holder["cfg"] is None:
                raise RuntimeError("no key for user")
            return holder["cfg"]

        monkeypatch.setattr(lga, "load_model_config_for_user", fake_loader)

        c1, m1 = await rag.build_llm_client("u1")
        assert m1 == "user-model" and c1.api_key == "sk-u1"

        # 相同签名 → 缓存复用同一客户端
        c1b, m1b = await rag.build_llm_client("u1")
        assert c1b is c1 and m1b == "user-model"

        # key 变化 → 重建并关闭旧客户端
        with patch.object(c1, "close") as spy_close:
            holder["cfg"] = {"api_key": "sk-u2", "base_url": "http://u1/v1",
                             "model": "user-model"}
            c2, _ = await rag.build_llm_client("u1")
            spy_close.assert_called_once()
        assert c2 is not c1 and c2.api_key == "sk-u2"

        # loader 抛异常 → 回退全局配置
        async def bad_loader(user_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(lga, "load_model_config_for_user", bad_loader)
        c3, m3 = await rag.build_llm_client("u1")
        assert m3 == "glm-4-flash" and c3 is not c2
        assert c3.api_key == "sk-rag-global"

        # loader 返回 None → 同样回退全局
        async def none_loader(user_id):
            return None

        monkeypatch.setattr(lga, "load_model_config_for_user", none_loader)
        _, m4 = await rag.build_llm_client("u9")
        assert m4 == "glm-4-flash"

    async def test_stale_client_close_failure_is_swallowed(self, rag, monkeypatch):
        """签名变化时旧客户端 close 抛异常 → 吞掉并照常换新。"""
        import app.core.config as cfg
        import app.agents.langgraph_agent as lga
        monkeypatch.setenv("RAG_TEST_CHAT_KEY", "sk-rag-global")
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [{"type": "chat", "api_key_env": "RAG_TEST_CHAT_KEY",
                          "base_url": "http://global:8080/v1", "model": "glm-4-flash"}],
        })

        holder = {"cfg": {"api_key": "sk-a", "base_url": "http://u1/v1", "model": "m"}}

        async def loader(user_id):
            return holder["cfg"]

        monkeypatch.setattr(lga, "load_model_config_for_user", loader)
        c1, _ = await rag.build_llm_client("u1")

        holder["cfg"] = {"api_key": "sk-b", "base_url": "http://u1/v1", "model": "m"}
        with patch.object(c1, "close", side_effect=RuntimeError("close fail")):
            c2, m2 = await rag.build_llm_client("u1")
        assert c2 is not c1 and m2 == "m" and c2.api_key == "sk-b"


# ══════════════════════════════ sg_routes ══════════════════════════════

def _write_npz(path, doc_ids, raw_mat):
    with open(str(path), "wb") as f:
        np.savez(f, doc_ids=np.array(doc_ids), raw_vectors=raw_mat,
                 pca_vectors=np.zeros((len(doc_ids), 2), dtype=np.float32),
                 umap_vectors=np.zeros((len(doc_ids), 3), dtype=np.float32),
                 pca_dim=np.array(2), umap_n_components=np.array(3))


def _write_faiss(path, mat):
    import faiss
    path.parent.mkdir(parents=True, exist_ok=True)
    m = np.array(mat, dtype=np.float32).copy()
    faiss.normalize_L2(m)
    idx = faiss.IndexFlatIP(m.shape[1])
    idx.add(m)
    faiss.write_index(idx, str(path))


_GRAPH = {"nodes": [{"id": "d1", "name": "Docker部署", "category": "tech"},
                    {"id": "d2", "name": "系统架构", "category": "tech"}]}


class TestSgSearchBranches:
    async def test_legacy_pickle_artifact_fallback(self, tmp_path):
        """老格式纯 pickle 产物：allow_pickle=False 抛 ValueError → 回退后正常检索。"""
        import faiss
        from app.routes.sg_routes import sg_search

        vecs = {"d1": np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
                "d2": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)}
        (tmp_path / "vectors.pkl").write_bytes(
            pickle.dumps({"doc_ids": ["d1", "d2"], "raw_vectors": vecs}))
        _write_faiss(tmp_path / "models" / "faiss.index", list(vecs.values()))

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(_GRAPH, tmp_path)):
            result = await sg_search(q="d1", top_k=10, container=MagicMock())

        ids = [r["id"] for r in result["results"]]
        assert ids and ids[0] == "d1"  # 自相似度最高
        assert set(ids) == {"d1", "d2"}

    async def test_embed_query_path(self, tmp_path):
        """q 不在产物向量里 → 调 embed_client 取向量再检索。"""
        from app.routes.sg_routes import sg_search

        rng = np.random.default_rng(7)
        mat = rng.random((2, 8)).astype(np.float32)
        _write_npz(tmp_path / "vectors.pkl", ["d1", "d2"], mat)
        _write_faiss(tmp_path / "models" / "faiss.index", mat)

        container = MagicMock()
        container.embed_client.model = "m"
        container.embed_client.post_embedding = AsyncMock(
            return_value={"embedding": mat[1].tolist()})  # 与 d2 同向量

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(_GRAPH, tmp_path)):
            result = await sg_search(q="全新查询", top_k=10, container=container)

        assert container.embed_client.post_embedding.await_count == 1
        payload = container.embed_client.post_embedding.await_args.args[0]
        assert payload["model"] == "m" and payload["prompt"] == "全新查询"
        assert result["results"][0]["id"] == "d2"

    async def test_embed_failure_falls_back_to_keyword(self, tmp_path):
        """embed 调用失败 → 退化关键词匹配。"""
        from app.routes.sg_routes import sg_search

        rng = np.random.default_rng(7)
        mat = rng.random((2, 8)).astype(np.float32)
        _write_npz(tmp_path / "vectors.pkl", ["d1", "d2"], mat)
        _write_faiss(tmp_path / "models" / "faiss.index", mat)

        container = MagicMock()
        container.embed_client.model = "m"
        container.embed_client.post_embedding = AsyncMock(
            side_effect=RuntimeError("embed down"))

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(_GRAPH, tmp_path)):
            result = await sg_search(q="Docker", top_k=10, container=container)

        assert result["results"], "关键词回退应有结果"
        assert result["results"][0]["title"] == "Docker部署"

    async def test_query_dim_mismatch_falls_back_to_keyword(self, tmp_path):
        """query 向量维度与索引不一致 → 退化关键词匹配。"""
        from app.routes.sg_routes import sg_search

        rng = np.random.default_rng(7)
        mat = rng.random((2, 8)).astype(np.float32)
        _write_npz(tmp_path / "vectors.pkl", ["d1", "d2"], mat)
        _write_faiss(tmp_path / "models" / "faiss.index", mat)

        container = MagicMock()
        container.embed_client.model = "m"
        container.embed_client.post_embedding = AsyncMock(
            return_value={"embedding": [0.1, 0.2, 0.3, 0.4]})  # 4 维 ≠ 索引 8 维

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(_GRAPH, tmp_path)):
            result = await sg_search(q="Docker", top_k=10, container=container)

        assert result["results"][0]["title"] == "Docker部署"

    async def test_corrupt_faiss_index_falls_back_to_keyword(self, tmp_path):
        """FAISS 索引文件损坏 → 检索异常被吞掉，退化关键词匹配。"""
        from app.routes.sg_routes import sg_search

        rng = np.random.default_rng(7)
        mat = rng.random((2, 8)).astype(np.float32)
        _write_npz(tmp_path / "vectors.pkl", ["d1", "d2"], mat)
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "faiss.index").write_bytes(b"garbage-not-an-index")

        container = MagicMock()
        container.embed_client.model = "m"
        container.embed_client.post_embedding = AsyncMock(
            return_value={"embedding": mat[0].tolist()})

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(_GRAPH, tmp_path)):
            result = await sg_search(q="Docker部署", top_k=10, container=container)

        assert result["results"][0]["title"] == "Docker部署"

    async def test_doc_id_without_node_is_skipped(self, tmp_path):
        """向量产物里有 graph 节点之外的 doc_id → 该结果被跳过。"""
        from app.routes.sg_routes import sg_search

        mat = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0.9, 0.1, 0, 0]],
                       dtype=np.float32)
        _write_npz(tmp_path / "vectors.pkl", ["d1", "d2", "ghost"], mat)
        _write_faiss(tmp_path / "models" / "faiss.index", mat)

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(_GRAPH, tmp_path)):
            result = await sg_search(q="d1", top_k=10, container=MagicMock())

        ids = [r["id"] for r in result["results"]]
        assert "ghost" not in ids
        assert set(ids) == {"d1", "d2"}
        assert result["results"][0]["id"] == "d1"


class TestToVecDict:
    def test_ndim2_matrix_maps_rows_to_doc_ids(self):
        from app.routes.sg_routes import _to_vec_dict
        raw = np.arange(4, dtype=np.float32).reshape(2, 2)
        out = _to_vec_dict(raw, ["a", "b"])
        np.testing.assert_array_equal(out["a"], [0, 1])
        np.testing.assert_array_equal(out["b"], [2, 3])

    def test_zero_dim_object_array_unwrapped(self):
        """老格式：被 np.load 包成 0-d array 的 dict → .item() 还原。"""
        from app.routes.sg_routes import _to_vec_dict
        raw = np.array({"a": [1.0, 2.0]}, dtype=object)
        out = _to_vec_dict(raw, ["a"])
        assert out == {"a": [1.0, 2.0]}

    def test_plain_dict_passthrough(self):
        from app.routes.sg_routes import _to_vec_dict
        assert _to_vec_dict({"a": [9.0]}, ["a"]) == {"a": [9.0]}


# ══════════════════════════════ parser ══════════════════════════════

class TestLoadIndex:
    def test_groups_entries_by_category(self, tmp_path):
        idx = tmp_path / "index.json"
        idx.write_text(json.dumps([
            {"filepath": "a.md", "category": "cat1", "subcategory": "sub1"},
            {"filepath": "b.md", "category": "cat1", "subcategory": ""},
            {"filepath": "c.md", "category": "cat2", "subcategory": "sub2"},
        ]), encoding="utf-8")
        entries, cat_map = load_index(str(idx))
        assert len(entries) == 3
        assert set(cat_map) == {"cat1", "cat2"}
        assert len(cat_map["cat1"]) == 2
        assert cat_map["cat2"][0]["subcategory"] == "sub2"


class TestParseAllWithIndex:
    def test_index_subcategory_and_hidden_dirs(self, tmp_path):
        """index 提供 subcategory；. / _ 目录被跳过；根级 .md 归入「根目录」。"""
        docs = tmp_path / "docs"
        cat = docs / "01-安装"
        cat.mkdir(parents=True)
        (cat / "docker.md").write_text("# Docker\n## 安装\n内容", encoding="utf-8")
        (docs / "README.md").write_text("# README\n根级文档\n## 用法\n用法内容",
                                         encoding="utf-8")
        hidden = docs / ".hidden"
        hidden.mkdir()
        (hidden / "h.md").write_text("# 隐藏", encoding="utf-8")
        drafts = docs / "_drafts"
        drafts.mkdir()
        (drafts / "d.md").write_text("# 草稿", encoding="utf-8")

        idx = tmp_path / "index.json"
        idx.write_text(json.dumps([
            {"filepath": "安装/docker.md", "category": "01-安装", "subcategory": "容器"},
            {"filepath": "README.md", "category": ".", "subcategory": "总览"},
        ]), encoding="utf-8")

        parsed = parse_all(str(docs), str(idx))
        by_id = {d.id: d for d in parsed}
        assert set(by_id) == {"docker", "README"}  # 隐藏/下划线目录跳过，根级 md 解析
        assert by_id["docker"].subcategory == "容器"
        assert by_id["docker"].category == "01-安装"
        assert by_id["docker"].title == "Docker"
        assert by_id["README"].category == "根目录"
        assert by_id["README"].subcategory == "总览"  # index 的 subcategory 对根级文档生效

    def test_missing_category_dir_is_skipped(self, tmp_path, monkeypatch):
        """分类目录在扫描后被删除（listdir 有、磁盘无）→ 跳过不报错。"""
        import app.sg.pipeline.parser as parser_mod

        docs = tmp_path / "docs"
        docs.mkdir()
        real = docs / "real"
        real.mkdir()
        (real / "a.md").write_text("# A\n内容", encoding="utf-8")
        real_listdir = os.listdir

        def fake_listdir(path):
            if str(path) == str(docs):
                return ["ghost", "real"]
            return real_listdir(path)

        monkeypatch.setattr(parser_mod.os, "listdir", fake_listdir)
        parsed = parse_all(str(docs), "")
        assert [d.id for d in parsed] == ["a"]

    def test_listdir_failure_tolerated_root_md_still_parsed(self, tmp_path, monkeypatch):
        """分类扫描 listdir 抛异常 → 忽略分类不崩溃；根级 .md 仍被解析。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("# A", encoding="utf-8")
        calls = {"n": 0}
        real_listdir = os.listdir

        def flaky_listdir(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("denied")
            return real_listdir(path)

        monkeypatch.setattr("app.sg.pipeline.parser.os.listdir", flaky_listdir)
        parsed = parse_all(str(docs), "")
        assert [d.id for d in parsed] == ["a"]
        assert parsed[0].category == "根目录"


# ══════════════════════════════ entity_extractor / graph_builder ══════════════════════════════

class TestEntityExtractorWorkerCrash:
    def test_extract_batch_swallows_worker_crash(self):
        """工作线程函数本身抛异常（而非 chat_fn）→ 该项兜底空结构。"""
        ext = EntityExtractor(lambda m, max_tokens=1024: "{}", max_workers=2)

        def boom(text):
            raise RuntimeError("worker crashed")

        ext._extract_one = boom  # 直接替换线程内入口，命中 fut.result() 异常分支
        docs = [SimpleNamespace(raw_text=f"d{i}") for i in range(3)]
        results = ext.extract_batch(docs)
        assert len(results) == 3
        assert all(r == {"entities": [], "relations": []} for r in results)


class TestGraphBuilderNonVisualSource:
    def test_edge_with_unknown_source_type_filtered(self, tmp_path):
        """source_type 不在默认 visual_sources 里的边不进图。"""
        out = tmp_path / "graph.json"
        docs = [Document(id="A", title="A", category="c", subcategory=None, filepath="a.md"),
                Document(id="B", title="B", category="c", subcategory=None, filepath="b.md")]
        builder = GraphBuilder(str(out))
        builder.build(docs, edges=[("A", "B", "临时", 0.5, "internal_only")])
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["links"] == []
        assert len(data["nodes"]) == 2
