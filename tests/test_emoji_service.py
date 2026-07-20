"""Tests for EmojiService.rebuild_index — 首次创建与重建。

验证 rebuild 在 emoji_index.json 不存在时从内置种子加载元数据，
以及索引存在时从现有索引复用元数据。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_embed_client(dim: int = 4) -> MagicMock:
    """构造 mock embed client：enabled=True，post_embedding 返回定长向量。"""
    c = MagicMock()
    c.enabled = True
    c.model = "test-embed"
    c._role_cfg = MagicMock(return_value=2)
    c.post_embedding = AsyncMock(return_value={"embedding": [0.1] * dim})
    return c


class TestRebuildFromSeed:
    """索引文件不存在时从种子首次创建。"""

    @pytest.mark.asyncio
    async def test_rebuild_from_seed_when_index_missing(self, tmp_path):
        from app.services.emoji_service import EmojiService

        # 索引路径指向不存在的文件；种子用临时文件
        index_path = tmp_path / "emoji_index.json"
        seed_path = tmp_path / "emoji_seed.json"
        seed_data = [
            {"char": "💡", "code": "U+1F4A1", "name": "light bulb"},
            {"char": "🔥", "code": "U+1F525", "name": "fire"},
        ]
        seed_path.write_text(json.dumps(seed_data), encoding="utf-8")

        svc = EmojiService(embed_client=_make_embed_client())
        with patch.object(svc, "_resolve_index_path", return_value=index_path), \
             patch("app.services.emoji_service.SEED_PATH", seed_path), \
             patch.object(svc, "load_index_async", new_callable=AsyncMock):
            await svc.rebuild_index()

        # 索引文件被创建，含向量
        assert index_path.exists()
        written = json.loads(index_path.read_text(encoding="utf-8"))
        assert len(written) == 2
        assert all("vec" in item and len(item["vec"]) == 4 for item in written)
        assert svc._rebuild_done == 2
        assert svc._rebuild_errors == 0
        assert "重建完成" in svc._rebuild_message

    @pytest.mark.asyncio
    async def test_rebuild_aborts_when_neither_index_nor_seed(self, tmp_path):
        from app.services.emoji_service import EmojiService

        index_path = tmp_path / "emoji_index.json"
        seed_path = tmp_path / "missing_seed.json"

        svc = EmojiService(embed_client=_make_embed_client())
        with patch.object(svc, "_resolve_index_path", return_value=index_path), \
             patch("app.services.emoji_service.SEED_PATH", seed_path):
            await svc.rebuild_index()

        assert not svc._rebuild_running
        assert "均不存在" in svc._rebuild_message


class TestRebuildFromExisting:
    """索引文件存在时从现有索引复用元数据重建。"""

    @pytest.mark.asyncio
    async def test_rebuild_from_existing_index(self, tmp_path):
        from app.services.emoji_service import EmojiService

        index_path = tmp_path / "emoji_index.json"
        existing = [
            {"char": "☀️", "code": "U+2600", "name": "sun", "vec": [0.0, 0.0, 0.0, 0.0]},
            {"char": "🌧️", "code": "U+1F327", "name": "cloud with rain", "vec": [0.0, 0.0, 0.0, 0.0]},
        ]
        index_path.write_text(json.dumps(existing), encoding="utf-8")

        svc = EmojiService(embed_client=_make_embed_client())
        with patch.object(svc, "_resolve_index_path", return_value=index_path), \
             patch.object(svc, "load_index_async", new_callable=AsyncMock):
            await svc.rebuild_index()

        written = json.loads(index_path.read_text(encoding="utf-8"))
        assert len(written) == 2
        # 向量被重新 embed（不再是全 0）
        assert all(item["vec"] == [0.1, 0.1, 0.1, 0.1] for item in written)
        assert svc._rebuild_done == 2
