"""语义图构建服务 — 桥接 Aether 的 async LLM 客户端与同步 KG pipeline。

核心难点：pipeline 是同步代码（ThreadPoolExecutor + 同步回调），而 Aether 的
embed_client / llm_chat_client 是 async。解法是在 pipeline 线程内用
asyncio.run_coroutine_threadsafe 把回调投递回主事件循环，等同步结果返回。

构建产物落在 app/sg/output/<timestamp>/，含：
  graph.json          — 3D 球视图 + 节点/边数据
  vectors.pkl         — 原始/PCA/UMAP 向量
  models/faiss.index  — 向量检索索引
  models/pca_model.joblib / umap_model.joblib
  llm_progress.json   — LLM 邻居分析断点续传
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..sg.sg_config import SgConfig

logger = logging.getLogger(__name__)

# 语义图产物输出根目录：app/sg/output/
# sg_service.py 位于 app/services/，需向上两级到 app/ 再进 sg/output
OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "sg" / "output"


class SemanticGraphService:
    """语义图构建与产物查询服务。

    单任务模型：同一时刻只允许一个构建任务运行。状态通过 status/progress/message
    暴露给前端轮询。
    """

    def __init__(self, embed_client: Any, llm_chat_client: Any) -> None:
        self._embed_client = embed_client
        self._chat_client = llm_chat_client
        self._loop: asyncio.AbstractEventLoop | None = None

        # 任务状态
        self.status: str = "idle"   # idle | running | done | error
        self.progress: int = 0      # 0-100
        self.message: str = ""
        self.task_dir: Path | None = None
        self._cancel = False

    # ── 事件循环绑定（lifespan 启动后调用）──
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定主事件循环，供线程内回调投递。"""
        self._loop = loop

    # ── 状态查询 ──
    def snapshot(self) -> dict[str, Any]:
        """返回当前任务状态快照（供前端轮询）。"""
        return {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "task_dir": str(self.task_dir) if self.task_dir else None,
        }

    # ── 取消 ──
    def cancel(self) -> None:
        """请求取消当前构建任务（协作式，下一阶段检查点生效）。"""
        self._cancel = True
        self.message = "正在取消..."

    # ── 产物查询 ──
    @staticmethod
    def latest_graph() -> tuple[dict, Path] | None:
        """扫描 output/ 目录，返回最近一次构建的 (graph_data, task_dir)。

        无产物返回 None。
        """
        if not OUTPUT_ROOT.exists():
            return None
        for td in sorted(OUTPUT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            g = td / "graph.json"
            if g.exists():
                try:
                    return json.loads(g.read_text(encoding="utf-8")), td
                except Exception:
                    continue
        return None

    # ── 构建入口 ──
    async def build_async(self) -> dict[str, Any]:
        """异步触发构建。pipeline 在默认线程池执行，回调投递回主循环。

        Returns:
            构建结果快照。
        """
        if self.status == "running":
            return {"error": "已有构建任务在运行", **self.snapshot()}

        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        cfg = SgConfig.from_config()
        if not cfg.ready:
            self.status = "error"
            self.message = "未配置 embed 或 chat key，请先在 /keys 页面配置"
            return self.snapshot()

        docs_root = Path(os.environ.get("DOCS_ROOT") or str(Path(__file__).resolve().parent.parent.parent / "docs"))
        if not docs_root.exists():
            self.status = "error"
            self.message = f"docs 目录不存在: {docs_root}"
            return self.snapshot()

        self.status = "running"
        self.progress = 0
        self.message = "启动构建..."
        self._cancel = False

        # 创建本次任务目录
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.task_dir = OUTPUT_ROOT / ts
        self.task_dir.mkdir(parents=True, exist_ok=True)

        loop = self._loop
        # fire-and-forget：后台线程跑构建，路由立即返回状态快照供前端轮询
        # 不 await executor，否则 HTTP 响应要等整个构建（数分钟）才返回
        future = loop.run_in_executor(None, self._build_sync, cfg, docs_root)

        def _on_done(fut: Any) -> None:
            try:
                fut.result()
            except Exception as e:
                logger.exception("语义图构建失败")
                self.status = "error"
                self.message = f"构建失败: {e}"
                return
            if self._cancel:
                self.status = "idle"
                self.message = "已取消"
            else:
                self.status = "done"
                self.progress = 100
                self.message = "构建完成"

        future.add_done_callback(_on_done)
        return self.snapshot()

    # ── 同步构建（在线程池中运行）──
    def _build_sync(self, cfg: SgConfig, docs_root: Path) -> None:
        """5 步流水线：解析 → 向量化 → 实体抽取 → 邻居分析 → 导出图。"""
        # sg_service.py 位于 app/services/，pipeline 位于 app/sg/pipeline/
        from ..sg.pipeline.parser import parse_all
        from ..sg.pipeline.vectorizer import Vectorizer
        from ..sg.pipeline.entity_extractor import EntityExtractor
        from ..sg.pipeline.relation_analyzer import analyze_neighbor_pairs
        from ..sg.pipeline.rules import apply_rules
        from ..sg.pipeline.graph_builder import GraphBuilder

        task_dir = self.task_dir
        assert task_dir is not None

        # ---- 桥接回调 ----
        embed_fn = self._make_embed_fn()
        chat_fn = self._make_chat_fn()

        # ---- Step 1: 解析文档 ----
        self._set(5, "解析文档...")
        index_path = str(docs_root / "index.json")
        all_docs, entity_doc_map = parse_all(str(docs_root), index_path)
        if not all_docs:
            raise RuntimeError("未解析到任何文档，请检查 docs 目录")
        logger.info("SG step1: parsed %d docs", len(all_docs))

        # ---- Step 2: 向量化 ----
        self._set(15, "向量化文档...")

        def _on_embed(done, total):
            if total > 0:
                self._set(15 + int(done / total * 25), f"向量化 {done}/{total}")

        vectorizer = Vectorizer(
            pca_dim=cfg.pca_dim,
            umap_n_components=cfg.umap_n_components,
            umap_n_neighbors=cfg.umap_n_neighbors,
            umap_min_dist=cfg.umap_min_dist,
            umap_n_epochs=cfg.umap_n_epochs,
            max_paragraph_chars=cfg.max_paragraph_chars,
        )
        vectorizer.compute_doc_vectors(all_docs, embed_fn, on_progress=_on_embed)
        self._set(40, "降维（PCA + UMAP）...")
        vectorizer.fit_transform()

        vectors_path = str(task_dir / "vectors.pkl")
        model_dir = str(task_dir / "models")
        faiss_path = str(task_dir / "models" / "faiss.index")
        vectorizer.save(vectors_path, model_dir, faiss_path)
        logger.info("SG step2: vectors saved (dim=%d)", cfg.pca_dim)

        if self._cancel:
            return

        # ---- Step 3: 实体抽取 ----
        self._set(45, "抽取实体...")
        extractor = EntityExtractor(chat_fn, max_workers=cfg.max_workers)

        def _on_entity(done, total):
            if total > 0:
                self._set(45 + int(done / total * 15), f"实体抽取 {done}/{total}")

        entity_results = extractor.extract_batch(all_docs, on_progress=_on_entity)
        logger.info("SG step3: extracted entities from %d docs", len(entity_results))

        # 回填实体到 doc.sections，激活 rules._entity_cooccurrence_edges（共享实体连边）
        # entity_results 与 all_docs 等长、按索引对应
        for doc, result in zip(all_docs, entity_results):
            names = [e.get("name", "") for e in (result or {}).get("entities", []) if e.get("name")]
            if names and doc.sections:
                doc.sections[0].entities = names
        total_entities = sum(len(s.entities) for d in all_docs for s in d.sections)
        logger.info("SG step3: backfilled %d entity names into sections", total_entities)

        if self._cancel:
            return

        # ---- Step 4: 邻居关系分析（规则边 + LLM 邻居边）----
        self._set(60, "分析文档关系...")

        def _on_relation(done, total):
            if total > 0:
                self._set(60 + int(done / total * 30), f"关系分析 {done}/{total}")

        rule_edges = list(apply_rules(all_docs))
        logger.info("SG step4a: %d rule edges", len(rule_edges))

        llm_edges = analyze_neighbor_pairs(
            all_docs, vectorizer, cfg.threshold, chat_fn,
            max_workers=cfg.max_workers, task_dir=task_dir, on_progress=_on_relation,
        )
        logger.info("SG step4b: %d llm neighbor edges", len(llm_edges))

        if self._cancel:
            return

        # ---- Step 5: 导出图 ----
        self._set(92, "导出 graph.json...")
        all_edges = rule_edges + llm_edges
        coords_3d = {d.id: vectorizer.get_3d_coords(d.id) for d in all_docs}
        graph_path = str(task_dir / "graph.json")
        builder = GraphBuilder(graph_export_path=graph_path)
        builder.build(all_docs, all_edges, coords_3d=coords_3d)
        logger.info("SG step5: graph.json exported to %s", graph_path)

    # ── 回调桥接 ──
    def _make_embed_fn(self):
        """构造同步 embed_fn(texts: list[str]) -> list[list[float]]。

        pipeline 线程内调用，通过 run_coroutine_threadsafe 投递到主循环。
        """
        loop = self._loop
        embed_client = self._embed_client

        def embed_fn(texts: list[str]) -> list[list[float]]:
            assert loop is not None
            results: list[list[float]] = []
            # 逐条 embedding（pipeline 侧已分批，这里一条一请求）
            for text in texts:
                fut = asyncio.run_coroutine_threadsafe(
                    embed_client.post_embedding({
                        "model": embed_client.model,
                        "prompt": text,
                    }),
                    loop,
                )
                resp = fut.result()  # 同步等待
                results.append(resp["embedding"])
            return results

        return embed_fn

    def _make_chat_fn(self):
        """构造同步 chat_fn(messages, max_tokens) -> str。

        pipeline 线程内调用，投递到主循环等待 LLM 返回。
        """
        loop = self._loop
        chat_client = self._chat_client

        def chat_fn(messages: list[dict], max_tokens: int = 1024) -> str:
            assert loop is not None
            fut = asyncio.run_coroutine_threadsafe(
                chat_client.chat(messages, timeout=120),
                loop,
            )
            return fut.result()

        return chat_fn

    # ── 辅助 ──
    def _set(self, progress: int, message: str) -> None:
        """更新进度（忽略取消态）。"""
        if self._cancel:
            return
        self.progress = min(progress, 100)
        self.message = message
