"""第 3 批稳定性修复的回归测试。

覆盖：会话按用户淘汰、SQLite 损坏自愈（rename 保留 + 备份恢复 + 裸建兜底）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. 会话治理：每用户保留最近 N 个
# ---------------------------------------------------------------------------

class TestSessionEviction:
    def _mk(self, store, sid: str, uid: str, updated_at: int):
        from app.services.session_store import SessionState
        s = SessionState(session_id=sid, request_id="r", user_id=uid)
        s.updated_at = updated_at
        store._sessions[sid] = s
        return s

    @pytest.mark.asyncio
    async def test_evict_oldest_beyond_limit(self):
        from app.services.session_store import SessionStore

        store = SessionStore()
        store._loaded = True
        # user-a 3 个会话，user-b 2 个；上限配 2
        self._mk(store, "a1", "user-a", 100)
        self._mk(store, "a2", "user-a", 300)
        self._mk(store, "a3", "user-a", 200)
        self._mk(store, "b1", "user-b", 50)
        self._mk(store, "b2", "user-b", 60)

        with patch("app.core.config.get_config", return_value=2):
            store._delete_session_async = lambda sid: None  # 不真删 DB
            evicted = store._evict_overflow_locked()

        assert evicted == 1
        assert "a1" not in store._sessions      # 最旧的被淘汰
        assert "a2" in store._sessions           # 300 最新保留
        assert "a3" in store._sessions           # 200 次新保留
        assert "b1" in store._sessions and "b2" in store._sessions  # 未超限不动

    @pytest.mark.asyncio
    async def test_zero_disables_eviction(self):
        from app.services.session_store import SessionStore

        store = SessionStore()
        store._loaded = True
        self._mk(store, "a1", "user-a", 100)
        self._mk(store, "a2", "user-a", 200)

        with patch("app.core.config.get_config", return_value=0):
            evicted = store._evict_overflow_locked()
        assert evicted == 0
        assert len(store._sessions) == 2

    @pytest.mark.asyncio
    async def test_store_session_triggers_eviction(self):
        from app.services.session_store import SessionStore, SessionState

        store = SessionStore()
        store._loaded = True
        deletions = []
        store._delete_session_async = lambda sid: deletions.append(sid)

        with patch("app.core.config.get_config", return_value=2), \
             patch.object(store, "_save_session_async"):
            for i in range(4):
                s = SessionState(session_id=f"s{i}", request_id="r", user_id="u")
                s.updated_at = 1000 + i
                await store.store_session(s)

        assert len(deletions) == 2  # 4 个会话超限 2，淘汰最旧 2 个


# ---------------------------------------------------------------------------
# 2. SQLite 损坏自愈
# ---------------------------------------------------------------------------

class TestDatabaseSelfHeal:
    @pytest.mark.asyncio
    async def test_corrupt_file_renamed_and_fresh_created(self, tmp_path, monkeypatch):
        """损坏 db → rename 保留现场 → 无备份 → 裸建新库成功。"""
        import app.core.database as db_mod

        db_file = tmp_path / "aether.db"
        db_file.write_bytes(b"this is not a sqlite file at all")
        monkeypatch.setattr(db_mod, "DB_PATH", db_file)
        monkeypatch.setattr(db_mod.Database, "_instance", None)
        monkeypatch.setattr(db_mod.Database, "_db", None)
        monkeypatch.setattr(db_mod.Database, "_open_conns", [])

        instance = await db_mod.Database.init()
        assert instance is not None
        # 损坏现场被保留
        corrupts = list(tmp_path.glob("aether.db.corrupt-*"))
        assert len(corrupts) == 1
        # 新库可用
        await db_mod.Database.close()
        assert db_file.exists()

    @pytest.mark.asyncio
    async def test_backup_restored_when_corrupt(self, tmp_path, monkeypatch):
        """损坏 db + 有可用备份 → 从备份恢复（数据可用）。"""
        import sqlite3
        import app.core.database as db_mod

        # 生产路径推导：DB_PATH = <root>/app/data/aether.db → 备份根
        # = DB_PATH.parent.parent.parent / "backups" = <root>/backups。
        # 测试目录必须凑足 app/data 两层深度。
        repo = tmp_path / "repo"
        db_file = repo / "app" / "data" / "aether.db"
        db_file.parent.mkdir(parents=True)
        db_file.write_bytes(b"garbage not sqlite")

        backup_root = repo / "backups"
        backup_root.mkdir()
        backup_db = backup_root / "aether.db"
        conn = sqlite3.connect(str(backup_db))
        conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO kv VALUES ('k', 'v')")
        conn.commit()
        conn.close()

        monkeypatch.setattr(db_mod, "DB_PATH", db_file)
        monkeypatch.setattr(db_mod.Database, "_instance", None)
        monkeypatch.setattr(db_mod.Database, "_db", None)
        monkeypatch.setattr(db_mod.Database, "_open_conns", [])

        try:
            instance = await db_mod.Database.init()
            assert instance is not None
            # 恢复后旧数据可用（查询走底层连接：Database 实例无通用 execute）
            conn_obj = db_mod.Database._db
            async with conn_obj.execute("SELECT value FROM kv WHERE key='k'") as cur:
                row = await cur.fetchone()
            assert row is not None and row[0] == "v"
            # 损坏现场保留
            assert len(list(db_file.parent.glob("aether.db.corrupt-*"))) == 1
        finally:
            # 必须显式关闭：aiosqlite worker 线程非 daemon，泄漏会卡死
            # 解释器退出（pytest 进程永不结束）
            await db_mod.Database.close()
