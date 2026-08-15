"""Integration tests for Database using a temporary SQLite file."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.database import Database


@pytest.fixture(autouse=True)
async def _reset_db_singleton():
    """Reset the Database singleton before each test."""
    Database._instance = None
    Database._db = None
    Database._write_lock = None
    yield
    if Database._db:
        await Database._db.close()
    Database._instance = None
    Database._db = None
    Database._write_lock = None


@pytest.fixture
async def db(tmp_path: Path):
    """Create a Database instance with a temp file."""
    db_path = tmp_path / "test.db"
    with patch("app.core.database.DB_PATH", db_path):
        instance = await Database.init()
        yield instance


class TestDatabase:
    @pytest.mark.asyncio
    async def test_init_creates_tables(self, db):
        assert db._db is not None
        cursor = await db._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in await cursor.fetchall()}
        assert "rules" in tables
        assert "sessions" in tables
        assert "kv" in tables

    @pytest.mark.asyncio
    async def test_rules_crud(self, db):
        rule = {"id": "r1", "name": "test", "condition": "有人"}
        await db.rules_insert("r1", rule)
        await asyncio.sleep(0.1)
        rules = await db.rules_all()
        assert len(rules) == 1
        assert rules[0]["name"] == "test"

        rule["name"] = "updated"
        await db.rules_update("r1", rule)
        await asyncio.sleep(0.1)
        rules = await db.rules_all()
        assert rules[0]["name"] == "updated"

        await db.rules_delete("r1")
        await asyncio.sleep(0.1)
        rules = await db.rules_all()
        assert len(rules) == 0

    @pytest.mark.asyncio
    async def test_sessions_crud(self, db):
        session_data = {"data": "test"}
        await db.sessions_upsert("s1", session_data)
        await asyncio.sleep(0.1)
        assert len(await db.sessions_all()) == 1

        await db.sessions_delete("s1")
        await asyncio.sleep(0.1)
        assert len(await db.sessions_all()) == 0

    @pytest.mark.asyncio
    async def test_close(self, tmp_path: Path):
        db_path = tmp_path / "test2.db"
        with patch("app.core.database.DB_PATH", db_path):
            instance = await Database.init()
            await Database.close()
        assert Database._db is None


# ============================================================================
# Task 1: cameras 表 + rules.camera_id + 单路→多路幂等迁移
# ============================================================================

class TestCamerasTable:
    """cameras 表 DDL + CRUD 全套。"""

    @pytest.mark.asyncio
    async def test_cameras_table_and_crud(self, db):
        # insert
        new_id = await db.cameras_insert({
            "id": "cam_aaaaaa", "name": "客厅", "enabled": 1, "sort_order": 0,
            "source_type": "rtsp", "rtsp_url": "rtsp://1.2.3.4/stream",
            "rtsp_username": "admin", "rtsp_password": "pwd",
            "device_mac": "aa-bb-cc-dd-ee-ff", "discovery_enabled": 1,
            "ptz_enabled": 1, "ptz_ip": "1.2.3.4", "ptz_port": 80,
            "ptz_username": "admin", "ptz_password": "pwd",
            "ptz_speed": 0.5, "ptz_step_ms": 300,
            "motion_hash_size": 16, "motion_threshold": 15,
            "motion_check_interval": 1.0,
            "vision_min_infer_interval": 8.0,
            "vision_max_idle_interval": 120.0, "vision_use_img_count": 3,
            "frame_interval_ms": 2000, "display_enabled": 1,
        })
        assert new_id == "cam_aaaaaa"

        # get
        row = await db.cameras_get("cam_aaaaaa")
        assert row is not None
        assert row["name"] == "客厅"
        assert row["source_type"] == "rtsp"
        assert row["rtsp_url"] == "rtsp://1.2.3.4/stream"

        # all
        all_rows = await db.cameras_all()
        assert len(all_rows) == 1

        # update(部分字段,未传保留)
        ok = await db.cameras_update("cam_aaaaaa", {"name": "客厅2", "ptz_speed": 0.8})
        assert ok is True
        row2 = await db.cameras_get("cam_aaaaaa")
        assert row2["name"] == "客厅2"
        assert row2["ptz_speed"] == 0.8
        assert row2["rtsp_url"] == "rtsp://1.2.3.4/stream"

        # delete
        ok = await db.cameras_delete("cam_aaaaaa")
        assert ok is True
        assert await db.cameras_get("cam_aaaaaa") is None


class TestRulesCameraIdColumn:
    """rules 表 camera_id 列(迁移补列)。"""

    @pytest.mark.asyncio
    async def test_rules_has_camera_id_column(self, db):
        async with db._db.execute("PRAGMA table_info(rules)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        assert "camera_id" in cols


class TestCamerasMigration:
    """单路→多路幂等迁移(D6:用 @pytest.mark.migration 标记开启)。"""

    @pytest.mark.asyncio
    @pytest.mark.migration
    async def test_migration_from_legacy_config(self, tmp_path: Path, monkeypatch):
        """老部署(config 有 vision/ptz 段)首次迁移:生成一条默认摄像头记录。"""
        from app.core import database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "t.db")
        legacy = {
            "vision": {
                "rtsp_url": "rtsp://192.168.1.10/x",
                "rtsp_username": "admin", "rtsp_password_env": "RTSP_PASSWORD",
                "motion_threshold": 20, "motion_hash_size": 16,
                "min_infer_interval_seconds": 3.0,
                "max_idle_interval_seconds": 60.0, "vision_use_img_count": 3,
                "frame_interval_ms": 1000, "device_mac": "60-a3-e3-de-e0-54",
            },
            "ptz": {
                "enabled": True, "ip": "192.168.1.10", "port": 80,
                "username": "admin", "password_env": "PTZ_PASSWORD",
                "speed": 0.5, "step_ms": 300,
            },
            "automation": {"camera_vl_display_enabled": True},
        }
        with patch.object(db_mod, "_legacy_camera_config", return_value=legacy), \
             patch.object(db_mod, "_read_env_secret",
                          side_effect=lambda k: {"RTSP_PASSWORD": "rp", "PTZ_PASSWORD": "pp"}.get(k, "")):
            inst = await Database.init()
        try:
            rows = await inst.cameras_all()
            assert len(rows) == 1
            r = rows[0]
            assert r["id"].startswith("cam_")
            assert r["name"] == "默认摄像头"
            assert r["source_type"] == "rtsp"
            assert r["rtsp_url"] == "rtsp://192.168.1.10/x"
            assert r["rtsp_password"] == "rp"
            assert r["ptz_enabled"] == 1
            assert r["ptz_ip"] == "192.168.1.10"
            assert r["ptz_password"] == "pp"
            assert r["motion_threshold"] == 20
            assert r["device_mac"] == "60-a3-e3-de-e0-54"
            assert (await inst.kv_get("cameras_migrated")) == "1"
        finally:
            await Database.close()

    @pytest.mark.asyncio
    @pytest.mark.migration
    async def test_migration_idempotent(self, tmp_path: Path, monkeypatch):
        """二次 init 不重复迁移(KV 标记命中)。"""
        from app.core import database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "t.db")
        legacy = {
            "vision": {"rtsp_url": "rtsp://x"},
            "ptz": {"ip": "1.1.1.1"},
            "automation": {"camera_vl_display_enabled": True},
        }
        with patch.object(db_mod, "_legacy_camera_config", return_value=legacy), \
             patch.object(db_mod, "_read_env_secret", return_value=""):
            await Database.init()
            await Database.close()
            inst = await Database.init()
        try:
            rows = await inst.cameras_all()
            assert len(rows) == 1
        finally:
            await Database.close()

    @pytest.mark.asyncio
    async def test_migration_skipped_for_new_deploy(self, tmp_path: Path):
        """全新部署(无 legacy config,conftest 默认关迁移)→ cameras 表空,KV 不置位。"""
        with patch("app.core.database.DB_PATH", tmp_path / "t.db"):
            inst = await Database.init()
        try:
            assert await inst.cameras_all() == []
            assert (await inst.kv_get("cameras_migrated")) is None
        finally:
            await Database.close()
