"""Emoji 向量搜索服务。"""
from __future__ import annotations

import asyncio
import json
import logging
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

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def is_loading(self) -> bool:
        return self._loading

    async def load_index_async(self) -> None:
        """异步加载索引（不阻塞主线程）。"""
        if self._loaded or self._loading:
            return

        self._loading = True
        try:
            index_path = get_config("emoji.index_path") or str(DEFAULT_INDEX_PATH)
            path = Path(index_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent.parent.parent / path

            logger.info("Loading emoji index from %s ...", path)

            # 在线程池中执行 IO 密集操作
            loop = asyncio.get_event_loop()
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
