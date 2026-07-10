"""Tests for app/sg/pipeline/vectorizer.py — save/load 往返与 npz 格式。"""
from __future__ import annotations

import numpy as np
import pytest

from app.sg.pipeline.vectorizer import Vectorizer


def _make_vectorizer(doc_ids: list[str], dim: int = 4) -> Vectorizer:
    """构造一个跳过 embed/PCA/UMAP 的最小 Vectorizer，仅填 raw_vectors。"""
    v = Vectorizer(pca_dim=2, umap_n_components=3)
    v.doc_ids = list(doc_ids)
    v.raw_vectors = {d: np.random.rand(dim).astype(np.float32) for d in doc_ids}
    v.pca_vectors = {d: np.random.rand(2).astype(np.float32) for d in doc_ids}
    v.umap_vectors = {d: np.random.rand(3).astype(np.float32) for d in doc_ids}
    return v


class TestVectorizerSaveLoad:
    def test_save_writes_npz_not_pickle(self, tmp_path):
        """save 应写出 .npz 格式，可被 np.load(allow_pickle=False) 读回。"""
        v = _make_vectorizer(["doc1", "doc2"], dim=4)
        vectors_path = str(tmp_path / "vectors.pkl")
        model_dir = str(tmp_path / "models")
        faiss_path = str(tmp_path / "models" / "faiss.index")

        v.save(vectors_path, model_dir, faiss_path)

        # allow_pickle=False 能成功加载 → 证明不是 pickle 格式
        vd = np.load(vectors_path, allow_pickle=False)
        assert "doc_ids" in vd
        assert "raw_vectors" in vd
        assert "pca_vectors" in vd
        assert "umap_vectors" in vd

    def test_save_load_roundtrip_preserves_data(self, tmp_path):
        """save 后读回，doc_ids 与各向量矩阵应与原始一致。"""
        v = _make_vectorizer(["a", "b", "c"], dim=4)
        vectors_path = str(tmp_path / "vectors.pkl")
        model_dir = str(tmp_path / "models")
        faiss_path = str(tmp_path / "models" / "faiss.index")

        v.save(vectors_path, model_dir, faiss_path)
        vd = np.load(vectors_path, allow_pickle=False)

        # doc_ids 顺序与 raw_vectors 行对齐
        doc_ids = list(vd["doc_ids"])
        assert doc_ids == ["a", "b", "c"]

        # 还原成 {doc_id: vec} 字典，值应与原始一致
        raw_mat = vd["raw_vectors"]
        restored = {did: raw_mat[i] for i, did in enumerate(doc_ids)}
        for d in ["a", "b", "c"]:
            np.testing.assert_array_almost_equal(restored[d], v.raw_vectors[d])

        # 参数也存了
        assert int(vd["pca_dim"]) == 2
        assert int(vd["umap_n_components"]) == 3

    def test_empty_doc_ids_does_not_crash(self, tmp_path):
        """无文档时 save 不应报错（写出空矩阵）。"""
        v = Vectorizer()
        v.doc_ids = []
        v.raw_vectors = {}
        v.pca_vectors = {}
        v.umap_vectors = {}

        vectors_path = str(tmp_path / "vectors.pkl")
        v.save(vectors_path, str(tmp_path / "models"),
               str(tmp_path / "models" / "faiss.index"))

        vd = np.load(vectors_path, allow_pickle=False)
        assert list(vd["doc_ids"]) == []
        assert vd["raw_vectors"].shape[0] == 0
