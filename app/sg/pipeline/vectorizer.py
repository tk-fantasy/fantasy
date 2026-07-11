"""向量化与降维 — 从 kg-pipeline 移植，去掉对 Config 的依赖，参数直接传入。

embed 调用通过 embed_fn 回调注入（由 sg_service 从 async embed_client 桥接为同步）。
"""
import os
import numpy as np
import joblib
from sklearn.decomposition import PCA
import umap

from .parser import Document


class Vectorizer:
    """文档向量化、PCA/UMAP 降维、FAISS 索引。

    Args:
        pca_dim: PCA 降维目标维度
        umap_n_components: UMAP 输出维度（3D 球用 3）
        umap_n_neighbors: UMAP 邻居数
        umap_min_dist: UMAP 最小距离
        umap_n_epochs: UMAP 迭代轮数
        max_paragraph_chars: 段落截断长度
    """

    def __init__(
        self,
        pca_dim: int = 50,
        umap_n_components: int = 3,
        umap_n_neighbors: int = 15,
        umap_min_dist: float = 0.1,
        umap_n_epochs: int = 500,
        max_paragraph_chars: int = 512,
    ) -> None:
        self.pca_dim = pca_dim
        self.umap_n_components = umap_n_components
        self.umap_n_neighbors = umap_n_neighbors
        self.umap_min_dist = umap_min_dist
        self.umap_n_epochs = umap_n_epochs
        self.max_paragraph_chars = max_paragraph_chars

        self.pca: PCA | None = None
        self.umap_model = None
        self.faiss_index = None
        self.doc_ids: list[str] = []
        self.raw_vectors: dict[str, np.ndarray] = {}
        self.pca_vectors: dict[str, np.ndarray] = {}
        self.umap_vectors: dict[str, np.ndarray] = {}

    def _faiss_search(self, query_vec: np.ndarray, top_k: int) -> tuple:
        import faiss
        q = query_vec.reshape(1, -1).astype(np.float32)
        faiss.normalize_L2(q)
        scores, indices = self.faiss_index.search(q, min(top_k, self.faiss_index.ntotal))
        return scores[0], indices[0]

    def compute_doc_vectors(self, docs: list[Document], embed_fn, on_progress=None) -> dict[str, np.ndarray]:
        """计算文档向量（段落向量加权平均）。

        Args:
            docs: 文档列表
            embed_fn: 同步回调 embed_fn(texts: list[str]) -> list[list[float]]
            on_progress: 可选进度回调 on_progress(done, total)，每批 embed 完成后调用
        Returns:
            {doc_id: vector}
        """
        paragraphs = []
        doc_index = []
        weights = []

        for doc in docs:
            for sec in doc.sections:
                text = f"{sec.heading}: {sec.content[:self.max_paragraph_chars]}"
                paragraphs.append(text)
                doc_index.append(doc.id)
                w = max(1, len(sec.content))
                weights.append(w)

        if not paragraphs:
            return {}

        # 分批 embed：embed_fn 内部逐条发 HTTP，分批只是为了每批完成后报告进度
        total = len(paragraphs)
        batch_size = 16
        embeddings: list[list[float]] = []
        done = 0
        print(f"  Embedding {total} paragraphs (batch={batch_size})...")
        for i in range(0, total, batch_size):
            batch = paragraphs[i:i + batch_size]
            embeddings.extend(embed_fn(batch))
            done += len(batch)
            if on_progress is not None:
                on_progress(done, total)

        doc_vecs: dict[str, list[tuple[np.ndarray, float]]] = {}
        for doc_id, vec, w in zip(doc_index, embeddings, weights):
            arr = np.array(vec, dtype=np.float32)
            doc_vecs.setdefault(doc_id, []).append((arr, w))

        result: dict[str, np.ndarray] = {}
        for doc_id, vecs_and_weights in doc_vecs.items():
            total_w = sum(w for _, w in vecs_and_weights)
            weighted_sum = sum(v * w for v, w in vecs_and_weights)
            result[doc_id] = weighted_sum / total_w

        self.raw_vectors = result
        self.doc_ids = list(result.keys())
        return result

    def fit_transform(self, doc_vectors: dict[str, np.ndarray] | None = None):
        """PCA + UMAP 降维 + 构建 FAISS 索引。"""
        if doc_vectors is not None:
            self.raw_vectors = doc_vectors
            self.doc_ids = list(doc_vectors.keys())

        n = len(self.doc_ids)
        if n == 0:
            return

        raw_mat = np.array([self.raw_vectors[d] for d in self.doc_ids], dtype=np.float32)
        dim = raw_mat.shape[1]

        pca_n = min(self.pca_dim, n, dim)
        print(f"  PCA({pca_n}) on {n} vectors...")
        self.pca = PCA(n_components=pca_n, random_state=42)
        pca_mat = self.pca.fit_transform(raw_mat)
        for i, doc_id in enumerate(self.doc_ids):
            self.pca_vectors[doc_id] = pca_mat[i]

        explained = sum(self.pca.explained_variance_ratio_) * 100
        print(f"    Explained variance: {explained:.1f}%")

        ndim = self.umap_n_components
        umap_n = min(self.umap_n_neighbors, n - 1)
        if umap_n < 2:
            print(f"  Too few docs, using PCA first {ndim} components")
            for i, doc_id in enumerate(self.doc_ids):
                coord = np.zeros(ndim, dtype=np.float32)
                coord[:min(ndim, pca_mat.shape[1])] = pca_mat[i, :min(ndim, pca_mat.shape[1])]
                self.umap_vectors[doc_id] = coord
        else:
            print(f"  UMAP({ndim}D, n_neighbors={umap_n}, min_dist={self.umap_min_dist})...")
            self.umap_model = umap.UMAP(
                n_components=ndim,
                n_neighbors=umap_n,
                min_dist=self.umap_min_dist,
                n_epochs=self.umap_n_epochs,
                random_state=42,
            )
            umap_mat = self.umap_model.fit_transform(pca_mat)
            for i, doc_id in enumerate(self.doc_ids):
                self.umap_vectors[doc_id] = umap_mat[i].astype(np.float32)

        self._build_faiss_index(raw_mat)

    def _build_faiss_index(self, raw_mat: np.ndarray):
        import faiss
        dim = raw_mat.shape[1]
        normed = raw_mat.copy()
        faiss.normalize_L2(normed)
        self.faiss_index = faiss.IndexFlatIP(dim)
        self.faiss_index.add(normed)

    def get_neighbors_above_threshold(
        self, query_doc_id: str, threshold: float,
    ) -> list[tuple[str, float]]:
        if self.faiss_index is None or self.faiss_index.ntotal == 0:
            return []
        query_vec = self.raw_vectors.get(query_doc_id)
        if query_vec is None:
            return []
        scores, indices = self._faiss_search(query_vec, self.faiss_index.ntotal)
        results = []
        for score, idx in zip(scores, indices):
            did = self.doc_ids[idx]
            if did == query_doc_id:
                continue
            if score >= threshold:
                results.append((did, float(score)))
        return results

    def save(self, vectors_path: str, model_dir: str, faiss_index_path: str):
        """保存向量、降维模型、FAISS 索引到磁盘。

        向量产物用 numpy .npz 格式（np.savez），反序列化不执行任意代码，
        避免 pickle.load 的反序列化 RCE 风险。doc_ids 与各行向量按顺序对齐。

        注意：PCA/UMAP 降维模型（scikit-learn 对象）无法用 npz 序列化，
        下方仍走 joblib.dump（内部是 pickle）。这些 .joblib 文件由本机自行产出、
        仅本地加载，不接收外部输入，RCE 面可接受；切勿加载来源不明的 .joblib。
        """
        import faiss
        os.makedirs(model_dir, exist_ok=True)
        os.makedirs(os.path.dirname(vectors_path), exist_ok=True)

        # doc_ids 即 raw_vectors 的键，顺序一致（见 compute_doc_vectors）
        doc_ids = np.array(self.doc_ids)
        raw_mat = np.array(
            [self.raw_vectors[d] for d in self.doc_ids], dtype=np.float32
        ) if self.doc_ids else np.zeros((0, 0), dtype=np.float32)
        pca_mat = np.array(
            [self.pca_vectors[d] for d in self.doc_ids], dtype=np.float32
        ) if self.doc_ids and self.pca_vectors else np.zeros((0, 0), dtype=np.float32)
        umap_mat = np.array(
            [self.umap_vectors[d] for d in self.doc_ids], dtype=np.float32
        ) if self.doc_ids and self.umap_vectors else np.zeros((0, 0), dtype=np.float32)

        # vectors_path 以 .pkl 命名（历史路径），实际写入 .npz 内容；
        # 用文件对象调用 np.savez 避免它自动追加 .npz 扩展名，保持路径不变。
        # 读端按 vectors_path 直接 np.load 即可。
        with open(vectors_path, "wb") as f:
            np.savez(
                f,
                doc_ids=doc_ids,
                raw_vectors=raw_mat,
                pca_vectors=pca_mat,
                umap_vectors=umap_mat,
                pca_dim=np.array(self.pca_dim),
                umap_n_components=np.array(self.umap_n_components),
            )
        print(f"  Saved vectors to {vectors_path}")

        if self.pca is not None:
            joblib.dump(self.pca, os.path.join(model_dir, "pca_model.joblib"))
        if self.umap_model is not None:
            joblib.dump(self.umap_model, os.path.join(model_dir, "umap_model.joblib"))
        if self.faiss_index is not None:
            faiss.write_index(self.faiss_index, faiss_index_path)
        print(f"  Saved models + FAISS index to {model_dir}")

    def get_3d_coords(self, doc_id: str) -> tuple[float, float, float]:
        v = self.umap_vectors.get(doc_id, np.zeros(self.umap_n_components))
        return (float(v[0]), float(v[1]), float(v[2]))
