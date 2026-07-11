"""Tests for RagService — 索引持久化与复用逻辑。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.rag_service import RagService


def _make_embed_client(model: str = "test-embed", dim: int = 8):
    """构造 mock embed_client，post_embeddings_batch 返回固定向量。"""
    client = MagicMock()
    client.model = model
    client.enabled = True

    async def _batch(texts, timeout=60):
        # 每条文本返回一个固定向量（基于文本 hash 确定内容）
        return [[float(hash(t) % 100) / 100] * dim for t in texts]

    client.post_embeddings_batch = _batch
    return client


@pytest.fixture
def docs_root(tmp_path: Path) -> Path:
    """创建临时 docs 目录，含 2 个 markdown 文件。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\n## Section A1\n\n" + "x" * 60, encoding="utf-8")
    (docs / "b.md").write_text("# B\n\n## Section B1\n\n" + "y" * 60, encoding="utf-8")
    return docs


@pytest.fixture
def rag(tmp_path: Path, docs_root: Path) -> RagService:
    """构造 RagService，base_dir 指向 tmp_path/app，docs 指向 fixture docs_root。

    后台线程跑事件循环：build_index 在调用线程同步等待 run_coroutine_threadsafe
    投递的协程，需要 loop 实际在跑。
    """
    import os

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    embed_client = _make_embed_client()
    svc = RagService(base_dir=app_dir, embed_client=embed_client)

    loop = asyncio.new_event_loop()
    import threading
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    svc._loop = loop

    os.environ["DOCS_ROOT"] = str(docs_root)
    yield svc
    os.environ.pop("DOCS_ROOT", None)
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=2)


class TestTryLoad:
    def test_returns_false_when_no_index(self, rag: RagService):
        """索引目录不存在时 try_load 返回 False。"""
        assert rag.try_load() is False

    def test_returns_false_when_embed_client_none(self, rag: RagService):
        """embed_client 为 None 时返回 False。"""
        rag._embed_client = None
        assert rag.try_load() is False


class TestBuildAndLoad:
    def test_build_then_load_roundtrip(self, rag: RagService):
        """build_index 后落盘 → 新实例 try_load 返回 True + chunks 一致。"""
        rag.build_index()
        assert rag.is_ready
        original_chunks = list(rag.chunks)
        original_model = rag._embed_model

        # 新实例，复用同一个 base_dir（索引已落盘）
        # try_load 是纯文件 I/O，不需要事件循环
        svc2 = RagService(
            base_dir=rag._base_dir,
            embed_client=_make_embed_client(model=original_model),
        )
        assert svc2.try_load() is True
        assert svc2.chunks == original_chunks
        assert svc2._embed_model == original_model
        assert svc2.is_ready

    def test_load_skips_when_model_changed(self, rag: RagService):
        """meta 记录 model A，新实例用 model B → try_load 返回 False。"""
        rag.build_index()

        svc2 = RagService(
            base_dir=rag._base_dir,
            embed_client=_make_embed_client(model="different-model"),
        )
        assert svc2.try_load() is False

    def test_load_skips_when_docs_changed(self, rag: RagService, docs_root: Path):
        """build 后修改 docs 内容 → 指纹不匹配 → try_load 返回 False。"""
        rag.build_index()
        original_model = rag._embed_model

        # 修改 docs：新增一个文件
        (docs_root / "c.md").write_text("# C\n\n## Section C1\n\n" + "z" * 60, encoding="utf-8")

        svc2 = RagService(
            base_dir=rag._base_dir,
            embed_client=_make_embed_client(model=original_model),
        )
        assert svc2.try_load() is False


class TestSafeBuild:
    def test_safe_build_loads_from_disk_on_second_call(self, rag: RagService):
        """第一次 safe_build 构建并落盘，第二次 safe_build 直接读盘复用。"""
        rag.safe_build()
        assert rag.is_ready
        chunks_after_build = list(rag.chunks)

        # 模拟重启：清空内存索引
        rag.faiss_index = None
        rag.chunks = []
        rag._embed_model = ""

        # 第二次 safe_build → 应从磁盘加载，不调 embed
        rag.safe_build()
        assert rag.is_ready
        assert rag.chunks == chunks_after_build
