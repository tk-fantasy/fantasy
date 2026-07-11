"""Emoji 向量搜索服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

from ..clients.llm_chat_client import LlmChatClient
from ..core.config import get_config

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "emoji_index.json"


class EmojiService:
    """Emoji 向量搜索服务。

    启动时异步加载索引到内存，通过 embed API 做向量搜索。
    """

    def __init__(self, embed_client: LlmChatClient) -> None:
        self._embed_client = embed_client
        self._chars: list[str] = []
        self._names: list[str] = []
        self._vectors: np.ndarray | None = None
        self._loading = False
        self._loaded = False

        # 重建状态（内存单例，进程级）
        self._rebuild_running = False
        self._rebuild_total = 0
        self._rebuild_done = 0
        self._rebuild_errors = 0
        self._rebuild_started_at: float | None = None
        self._rebuild_finished_at: float | None = None
        self._rebuild_message = ""

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def is_loading(self) -> bool:
        return self._loading

    @property
    def rebuild_status(self) -> dict:
        """返回当前重建进度。"""
        return {
            "running": self._rebuild_running,
            "total": self._rebuild_total,
            "done": self._rebuild_done,
            "errors": self._rebuild_errors,
            "started_at": self._rebuild_started_at,
            "finished_at": self._rebuild_finished_at,
            "message": self._rebuild_message,
        }

    def _resolve_index_path(self) -> Path:
        index_path = get_config("emoji.index_path") or str(DEFAULT_INDEX_PATH)
        path = Path(index_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent.parent / path
        return path

    async def load_index_async(self) -> None:
        """异步加载索引（不阻塞主线程）。"""
        if self._loaded or self._loading:
            return

        self._loading = True
        try:
            path = self._resolve_index_path()

            logger.info("Loading emoji index from %s ...", path)

            # 在线程池中执行 IO 密集操作
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, self._load_file, path)

            self._chars = [item["char"] for item in data]
            self._names = [item["name"] for item in data]
            vectors_list = [item["vec"] for item in data]
            self._vectors = np.array(vectors_list, dtype=np.float32)

            # 预计算 L2 范数用于快速余弦相似度
            self._norms = np.linalg.norm(self._vectors, axis=1, keepdims=True)

            self._loaded = True
            logger.info("Emoji index loaded: %d emojis", len(self._chars))
        except Exception:
            logger.exception("Failed to load emoji index")
        finally:
            self._loading = False

    @staticmethod
    def _load_file(path: Path) -> list[dict]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def search(self, query: str, top_k: int = 20) -> list[dict]:
        """搜索与 query 最相似的 emoji。"""
        if not self._loaded:
            return []

        try:
            # 获取 query 向量
            result = await self._embed_client.post_embedding({
                "model": self._embed_client.model,
                "prompt": query,
            })
            query_vec = np.array(result["embedding"], dtype=np.float32)
        except Exception:
            logger.exception("Failed to get embedding for query: %s", query)
            return []

        # 计算余弦相似度
        query_norm = np.linalg.norm(query_vec)
        if query_norm < 1e-10:
            return []

        # 批量余弦相似度: dot(a, b) / (|a| * |b|)
        dots = self._vectors @ query_vec
        sims = dots / (self._norms.flatten() * query_norm + 1e-10)

        # 取 top_k
        top_indices = np.argsort(sims)[::-1][:top_k]

        results = []
        for idx in top_indices:
            results.append({
                "char": self._chars[idx],
                "name": self._names[idx],
                "score": round(float(sims[idx]), 4),
            })
        return results

    async def rebuild_index(self) -> None:
        """重建 emoji 向量索引。

        从现有 emoji_index.json 读取 char/code/name（复用元数据），
        用 embed client 逐个重新生成向量，写回文件后重新加载。
        并发度由 config providers.embed.max_concurrency 控制（默认 8）。
        """
        if self._rebuild_running:
            return

        # 前置检查：embed client 必须可用
        if not self._embed_client.enabled:
            self._rebuild_message = "Embed 模型未配置或未启用，请先在设置页配置 LLM Key"
            logger.warning("Emoji rebuild aborted: embed client not enabled")
            return

        path = self._resolve_index_path()

        # 读取现有索引的元数据（char/code/name），复用不需要重新 embed 的部分
        try:
            loop = asyncio.get_running_loop()
            existing = await loop.run_in_executor(None, self._load_file, path)
        except FileNotFoundError:
            self._rebuild_message = f"索引文件不存在: {path}，无法获取 emoji 列表"
            logger.error("Emoji rebuild aborted: %s not found", path)
            return

        self._rebuild_running = True
        self._rebuild_total = len(existing)
        self._rebuild_done = 0
        self._rebuild_errors = 0
        self._rebuild_started_at = time.time()
        self._rebuild_finished_at = None
        self._rebuild_message = "重建中..."

        try:
            max_concurrency = int(self._embed_client._role_cfg("max_concurrency") or 8)
            semaphore = asyncio.Semaphore(max_concurrency)

            async def embed_one(item: dict) -> dict:
                async with semaphore:
                    try:
                        result = await self._embed_client.post_embedding({
                            "model": self._embed_client.model,
                            "prompt": item["name"],
                        })
                        self._rebuild_done += 1
                        return {
                            "char": item["char"],
                            "code": item.get("code", item["char"]),
                            "name": item["name"],
                            "vec": result["embedding"],
                        }
                    except Exception:
                        self._rebuild_errors += 1
                        self._rebuild_done += 1
                        logger.warning("Failed to embed emoji '%s' (%s), skipped",
                                       item["name"], item["char"])
                        # 保留旧向量作为回退，避免丢条目
                        return {
                            "char": item["char"],
                            "code": item.get("code", item["char"]),
                            "name": item["name"],
                            "vec": item.get("vec", []),
                        }

            tasks = [embed_one(item) for item in existing]
            rebuilt = await asyncio.gather(*tasks)

            # 写回文件（线程池执行 IO）
            await loop.run_in_executor(None, self._write_file, path, rebuilt)

            # 重新加载索引
            self._loaded = False
            await self.load_index_async()

            self._rebuild_message = f"重建完成: {self._rebuild_done} 个, 失败 {self._rebuild_errors} 个"
            logger.info("Emoji index rebuilt: %d done, %d errors", self._rebuild_done, self._rebuild_errors)
        except Exception:
            self._rebuild_message = "重建失败，请查看后端日志"
            logger.exception("Emoji index rebuild failed")
        finally:
            self._rebuild_finished_at = time.time()
            self._rebuild_running = False

    @staticmethod
    def _write_file(path: Path, data: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
