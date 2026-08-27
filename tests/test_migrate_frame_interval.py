"""migrate_camera_frame_interval：旧默认 2000ms → 新默认 1000ms 的一次性迁移。"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.migrations import migrate_camera_frame_interval


@pytest.mark.asyncio
async def test_remaps_rows_equal_to_old_default():
    db = AsyncMock()
    db.cameras_remap_frame_interval = AsyncMock(return_value=3)

    await migrate_camera_frame_interval(db)

    db.cameras_remap_frame_interval.assert_awaited_once_with(old_ms=2000, new_ms=1000)


@pytest.mark.asyncio
async def test_swallows_db_errors(monkeypatch):
    """迁移失败只 warning 不阻塞启动（与其他 migrations 同一容错语义）。"""
    db = AsyncMock()
    db.cameras_remap_frame_interval = AsyncMock(side_effect=RuntimeError("db locked"))

    await migrate_camera_frame_interval(db)  # 不应抛出
