"""RAG 文档助手服务 — 封装 RAG 索引状态与检索/LLM 客户端构建。

原先 RAG 状态散落在 app/main.py 的模块级全局变量（RAG_CHUNKS / RAG_FAISS_INDEX /
RAG_EMBEDDER）与若干辅助函数中，路由层通过 `from ..main import` 延迟导入读取。
本类将其收敛为一个服务对象，由 AppContainer 持有，消除路由对 app.main 全局状态的依赖。

向量后端复用 embed_client（用户在前端 /keys 配置的 embed 模型），与语义图构建共享同一
embedding，消除维度不一致问题。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
import numpy as np

from ..core.config import get_config

logger = logging.getLogger(__name__)


class RagService:
    """RAG 索引状态与检索操作。

    索引在应用启动阶段（lifespan）后台构建，构建完成前 is_ready 为 False。
    向量化通过注入的 embed_client（async）完成，不再依赖外部 pipeline。
    """

    def __init__(self, base_dir: Path, embed_client=None) -> None:
        self._base_dir = base_dir
        self._embed_client = embed_client
        self._loop: asyncio.AbstractEventLoop | None = None
        self.chunks: list[str] = []
        self.faiss_index = None  # faiss.IndexFlatIP | None
        # 记录构建索引时使用的 embed 模型，用于检测模型变更后自动重建
        self._embed_model: str = ""
        self._rebuilding: bool = False
        # docs 指纹缓存：try_load 算好后供 _persist_index 复用，避免同一 build 周期重复扫盘
        self._last_fingerprint: dict[str, list[int]] | None = None
        # 索引持久化目录（与 SQLite 同卷 aether-data，跨重启复用）
        self._index_dir = base_dir / "data" / "rag_index"

    @property
    def is_ready(self) -> bool:
        """RAG 索引是否已构建就绪。"""
        return self.faiss_index is not None and len(self.chunks) > 0

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定主事件循环（后台线程构建索引用）。"""
        self._loop = loop

    def safe_build(self) -> None:
        """build_index 的安全包装，吞掉异常。供后台线程调用。

        启动时先尝试从磁盘加载持久化索引（docs 内容 + embed 模型未变则复用），
        命中则跳过全量向量化；未命中才重建并落盘。
        """
        try:
            if self.try_load():
                logger.info(
                    "RAG index loaded from disk: %d chunks, model=%s",
                    len(self.chunks), self._embed_model,
                )
                return
            self.build_index()
        except Exception as e:
            logger.warning("RAG index build failed: %s", e)
        finally:
            self._rebuilding = False

    def _compute_docs_fingerprint(self) -> dict[str, list[int]]:
        """扫描 docs 目录，返回每个 .md 文件的指纹 {相对路径: [size, mtime_ns]}。

        用于检测 docs 内容是否变更：只要文件大小或修改时间变了就判定为需要重建。
        """
        docs_root = Path(os.environ.get("DOCS_ROOT", str(self._base_dir.parent / "docs")))
        fingerprint: dict[str, list[int]] = {}
        if not docs_root.exists():
            return fingerprint
        for md_path in sorted(docs_root.rglob("*.md")):
            rel = str(md_path.relative_to(docs_root)).replace("\\", "/")
            stat = md_path.stat()
            fingerprint[rel] = [stat.st_size, stat.st_mtime_ns]
        return fingerprint

    def try_load(self) -> bool:
        """尝试从磁盘加载持久化索引。成功返回 True，需重建返回 False。

        判定条件（全部满足才复用）：
        1. 三个产物文件都存在（faiss.index / chunks.json / meta.json）
        2. meta 中记录的 embed 模型与当前 embed_client.model 一致
        3. meta 中记录的 docs 指纹与当前 docs 目录一致
        """
        index_file = self._index_dir / "faiss.index"
        chunks_file = self._index_dir / "chunks.json"
        meta_file = self._index_dir / "meta.json"
        if not (index_file.exists() and chunks_file.exists() and meta_file.exists()):
            return False
        if self._embed_client is None:
            return False

        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            # 模型变更 → 必须重建
            if meta.get("embed_model") != self._embed_client.model:
                logger.info(
                    "RAG: 持久化索引模型不匹配 (disk=%s, current=%s)，需重建",
                    meta.get("embed_model"), self._embed_client.model,
                )
                return False
            # docs 内容变更 → 必须重建
            current_fp = self._compute_docs_fingerprint()
            self._last_fingerprint = current_fp  # 缓存供 _persist_index 复用（同 build 周期 docs 不变）
            if meta.get("docs_fingerprint") != current_fp:
                logger.info("RAG: docs 内容已变更，需重建索引")
                return False

            import faiss  # 文件齐全且模型/docs 未变才需要 faiss；faiss 缺失时此处才暴露 ModuleNotFoundError
            self.faiss_index = faiss.read_index(str(index_file))
            self.chunks = json.loads(chunks_file.read_text(encoding="utf-8"))
            self._embed_model = meta["embed_model"]
            return True
        except Exception as e:
            logger.warning("RAG: 加载持久化索引失败，回退重建: %s", e)
            return False

    def maybe_rebuild_if_model_changed(self) -> None:
        """检测 embed 模型是否变更，变更则在后台线程重建索引。

        由 LLM 设置热重载钩子触发（embed_client.reload 之后调用）。
        _rebuilding 防止并发重建。
        """
        if self._rebuilding:
            return
        if not self._embed_model or not self._embed_client:
            return
        current_model = self._embed_client.model
        if current_model == self._embed_model:
            return
        logger.info("RAG: embed 模型变更 %s -> %s，后台重建索引", self._embed_model, current_model)
        self._rebuilding = True
        loop = self._loop
        if loop is None:
            logger.warning("RAG: 主循环未绑定，无法后台重建")
            self._rebuilding = False
            return
        loop.run_in_executor(None, self.safe_build)

    def build_index(self) -> None:
        """扫描 docs 目录，用 embed_client 向量化后构建 FAISS 索引。

        在后台线程中运行：通过 run_coroutine_threadsafe 把 async embed 调用投递回主循环。
        失败时记录警告，不抛异常。
        """
        import faiss

        loop = self._loop
        if loop is None:
            loop = asyncio.new_event_loop()
        if self._embed_client is None:
            logger.warning("RAG: embed_client 未注入，跳过索引构建")
            return
        if not self._embed_client.enabled:
            logger.warning("RAG: embed 客户端未启用，跳过索引构建")
            return

        docs_root = Path(os.environ.get("DOCS_ROOT", str(self._base_dir.parent / "docs")))
        if not docs_root.exists():
            logger.warning("RAG: docs not found: %s", docs_root)
            return

        chunks: list[str] = []
        for md_path in docs_root.rglob("*.md"):
            text = md_path.read_text(encoding="utf-8")
            sections = text.split("\n## ")
            for sec in sections:
                sec = sec.strip()
                if len(sec) > 50:
                    chunks.append(sec)
        if not chunks:
            logger.warning("RAG: no chunks")
            return

        # 批量向量化：每批 16 条，一次 HTTP 请求拿多条向量（比逐条请求省 ~16x 往返）
        embed_client = self._embed_client

        def _embed_batch(texts: list[str]) -> list[list[float]]:
            fut = asyncio.run_coroutine_threadsafe(
                embed_client.post_embeddings_batch(texts),
                loop,
            )
            return fut.result()

        all_vecs: list[list[float]] = []
        batch = 16
        for i in range(0, len(chunks), batch):
            all_vecs.extend(_embed_batch(chunks[i:i + batch]))

        vectors = np.array(all_vecs, dtype=np.float32)
        faiss.normalize_L2(vectors)

        dim = vectors.shape[1]
        index = faiss.IndexFlatIP(dim)
        index.add(vectors)

        self.chunks = chunks
        self.faiss_index = index
        self._embed_model = embed_client.model
        logger.info("RAG index ready: %d chunks, dim=%d, model=%s", len(chunks), dim, self._embed_model)

        # 落盘持久化：下次启动若 docs + 模型未变则直接读盘，跳过全量向量化
        self._persist_index(index, chunks, self._embed_model)

    def _persist_index(self, index, chunks: list[str], embed_model: str) -> None:
        """把索引产物写到磁盘，供下次启动复用。"""
        import faiss

        try:
            self._index_dir.mkdir(parents=True, exist_ok=True)
            faiss.write_index(index, str(self._index_dir / "faiss.index"))
            (self._index_dir / "chunks.json").write_text(
                json.dumps(chunks, ensure_ascii=False), encoding="utf-8"
            )
            # 复用 try_load 已算的指纹（同一 build 周期 docs 不变），避免重复扫盘
            fingerprint = self._last_fingerprint or self._compute_docs_fingerprint()
            meta = {
                "embed_model": embed_model,
                "docs_fingerprint": fingerprint,
            }
            (self._index_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
            logger.info("RAG index persisted to %s", self._index_dir)
        except Exception as e:
            logger.warning("RAG: 索引落盘失败（不影响本次构建）: %s", e)

    async def search(self, query: str) -> str:
        """embedding + FAISS 搜索 → 返回拼接后的 context 字符串。"""
        import faiss

        if self.faiss_index is None or self._embed_client is None:
            return ""

        embed_client = self._embed_client

        def _embed_and_search():
            # search 在主循环上下文���用，可直接 await；此处用线程安全投递
            loop = self._loop or asyncio.get_event_loop()
            fut = asyncio.run_coroutine_threadsafe(
                embed_client.post_embedding({
                    "model": embed_client.model,
                    "prompt": query,
                }),
                loop,
            )
            q_vec = np.array(fut.result()["embedding"], dtype=np.float32).reshape(1, -1)
            # 维度守卫：模型变更后 query 维度可能与索引不一致，跳过检索避免 FAISS 崩溃
            if q_vec.shape[1] != self.faiss_index.d:
                logger.warning(
                    "RAG search: 维度不匹配 (query=%d, index=%d)，模型可能已变更，跳过检索",
                    q_vec.shape[1], self.faiss_index.d,
                )
                return []
            faiss.normalize_L2(q_vec)
            scores, indices = self.faiss_index.search(q_vec, 5)
            return [self.chunks[i] for i in indices[0] if 0 <= i < len(self.chunks)]

        # search 在路由 async 上下文调用，直接在线程池跑同步块
        loop = asyncio.get_event_loop()
        try:
            context_chunks = await loop.run_in_executor(None, _embed_and_search)
        except Exception as e:
            logger.warning("RAG search 失败，返回空上下文: %s", e)
            return ""
        return "\n\n---\n\n".join(context_chunks)

    def build_llm_client(self) -> tuple:
        """构建 RAG 用的 OpenAI 客户端，返回 (client, model_name)。"""
        import openai

        llm_keys = get_config("llm_keys", [])
        chat_cfg = next((k for k in llm_keys if k.get("type") == "chat"), {})
        chat_key = os.environ.get(chat_cfg.get("api_key_env", ""), "")
        chat_base = chat_cfg.get("base_url", "")
        chat_model = chat_cfg.get("model", "glm-4-flash")
        transport = httpx.HTTPTransport(retries=0)
        client = openai.OpenAI(
            api_key=chat_key, base_url=chat_base,
            http_client=httpx.Client(transport=transport, timeout=60.0, trust_env=False),
        )
        return client, chat_model
