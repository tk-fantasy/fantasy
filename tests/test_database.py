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
        result = await db.sessions_get("s1")
        assert result is not None

        all_sessions = await db.sessions_all()
        assert len(all_sessions) == 1

        await db.sessions_delete("s1")
        await asyncio.sleep(0.1)
        result = await db.sessions_get("s1")
        assert result is None

    @pytest.mark.asyncio
    async def test_close(self, tmp_path: Path):
        db_path = tmp_path / "test2.db"
        with patch("app.core.database.DB_PATH", db_path):
            instance = await Database.init()
            await Database.close()
        assert Database._db is None
