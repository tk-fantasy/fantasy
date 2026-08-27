"""vision_logs 识别日志：落库/过滤/删除（独立 SQLite，仿 test_call_service_operable 模式）。"""
from __future__ import annotations

import pytest

import app.core.database as db_mod
from app.core.database import Database


@pytest.fixture()
async def db(tmp_path, monkeypatch):
    """独立数据库实例（重置单例 + 模块级 DB_PATH 指 tmp）。

    Database.init 直接引用模块级 DB_PATH，patch 模块属性即可
    （与 tests/test_call_service_operable.py 同模式）。
    """
    Database._instance = None
    Database._db = None
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "t.db")
    instance = await Database.init()
    yield instance
    await Database.close()


async def test_insert_tail_filter_delete(db):
    await db.vision_log_insert("vcam_test-camera", "preview", {"event": "person_detected"})
    await db.vision_log_insert("vcam_test-camera", "rule_eval",
                               {"condition": "画面中出现人", "result": 1})
    await db.vision_log_insert("cam_real", "preview", {"event": "no_event"})

    # 全量
    all_rows = await db.vision_logs_tail()
    assert len(all_rows) == 3

    # 按 camera_id 过滤
    vrows = await db.vision_logs_tail(camera_id="vcam_test-camera")
    assert len(vrows) == 2
    assert all(r["camera_id"] == "vcam_test-camera" for r in vrows)

    # 按 kind 过滤
    evals = await db.vision_logs_tail(kind="rule_eval")
    assert len(evals) == 1
    assert evals[0]["content"]["result"] == 1

    # 倒序（新在前）
    assert all_rows[0]["camera_id"] == "cam_real"

    # limit
    limited = await db.vision_logs_tail(limit=2)
    assert len(limited) == 2

    # 按摄像头删除
    deleted = await db.vision_logs_delete_camera("vcam_test-camera")
    assert deleted == 2
    remain = await db.vision_logs_tail()
    assert len(remain) == 1
    assert remain[0]["camera_id"] == "cam_real"
