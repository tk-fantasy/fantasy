from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "aether.db"


class Database:
    """SQLite 异步持久化层。

    使用 WAL 模式提升并发性能，所有写操作通过 asyncio.create_task 异步执行，
    内存缓存保持同步更新，数据库写入在后台完成。
    """

    _instance: Database | None = None
    _db: aiosqlite.Connection | None = None
    _write_lock: asyncio.Lock | None = None

    @classmethod
    async def init(cls) -> Database:
        """初始化数据库连接并创建表结构。"""
        if cls._instance is not None:
            return cls._instance

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(DB_PATH))
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                user_id TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                user_id TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS emoji_preferences (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                emoji_char TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                user_id TEXT DEFAULT '',
                UNIQUE(scope, key, user_id)
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(user_id, key)
            );

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
        """)
        await db.commit()

        # —— 幂等列迁移：旧库的表是更早版本建的，CREATE TABLE IF NOT EXISTS
        # 不会修改已存在的表，导致代码新增的 user_id 列在旧库中永久缺失，
        # sessions_upsert 等写入会抛 OperationalError。这里按需补列。
        async def _ensure_column(table: str, column: str, definition: str) -> None:
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            if column not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
                logger.info("Migration: added column %s.%s", table, column)

        await _ensure_column("sessions", "user_id", "user_id TEXT DEFAULT ''")
        await _ensure_column("rules", "user_id", "user_id TEXT DEFAULT ''")
        await _ensure_column("emoji_preferences", "user_id", "user_id TEXT DEFAULT ''")
        await db.commit()

        cls._db = db
        cls._write_lock = asyncio.Lock()
        cls._instance = cls()
        logger.info("Database initialized at %s", DB_PATH)
        return cls._instance

    @classmethod
    def get(cls) -> Database:
        """获取已初始化的实例。"""
        if cls._instance is None:
            raise RuntimeError("Database not initialized. Call await Database.init() first.")
        return cls._instance

    @classmethod
    async def close(cls) -> None:
        """关闭数据库连接。"""
        if cls._db is not None:
            await cls._db.close()
            cls._db = None
            cls._write_lock = None
            cls._instance = None
            logger.info("Database closed")

    def __init__(self) -> None:
        if self._db is None:
            raise RuntimeError("Database not initialized")

    # ============ Rules 操作 ============

    async def rules_all(self, user_id: str = "") -> list[dict]:
        """获取规则列表，可选按 user_id 过滤。"""
        if user_id:
            async with self._db.execute(
                "SELECT id, data FROM rules WHERE user_id = ? ORDER BY created_at", (user_id,)
            ) as cursor:
                return [json.loads(r[1]) async for r in cursor]
        else:
            async with self._db.execute("SELECT id, data FROM rules ORDER BY created_at") as cursor:
                return [json.loads(r[1]) async for r in cursor]

    async def rules_insert(self, rule_id: str, data: dict, user_id: str = "") -> None:
        now = int(time.time() * 1000)
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO rules (id, data, created_at, updated_at, user_id) VALUES (?, ?, ?, ?, ?)",
                (rule_id, json.dumps(data, ensure_ascii=False), now, now, user_id),
            )
            await self._db.commit()

    async def rules_update(self, rule_id: str, data: dict) -> bool:
        now = int(time.time() * 1000)
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE rules SET data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), now, rule_id),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def rules_delete(self, rule_id: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            await self._db.commit()
            return cursor.rowcount > 0

    # ============ Scheduled Tasks 操作 ============

    async def scheduled_tasks_all(self) -> list[dict]:
        """获取全部定时任务。"""
        async with self._db.execute("SELECT id, data FROM scheduled_tasks ORDER BY created_at") as cursor:
            return [json.loads(r[1]) async for r in cursor]

    async def scheduled_task_insert(self, task_id: str, data: dict) -> None:
        now = int(time.time() * 1000)
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO scheduled_tasks (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (task_id, json.dumps(data, ensure_ascii=False), now, now),
            )
            await self._db.commit()

    async def scheduled_task_update(self, task_id: str, data: dict) -> bool:
        now = int(time.time() * 1000)
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE scheduled_tasks SET data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), now, task_id),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def scheduled_task_delete(self, task_id: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            await self._db.commit()
            return cursor.rowcount > 0

    # ============ Sessions 操作 ============

    async def sessions_all(self, user_id: str = "") -> list[dict]:
        """获取会话列表，可选按 user_id 过滤。"""
        if user_id:
            async with self._db.execute(
                "SELECT id, data FROM sessions WHERE user_id = ?", (user_id,)
            ) as cursor:
                return [json.loads(r[1]) async for r in cursor]
        else:
            async with self._db.execute("SELECT id, data FROM sessions") as cursor:
                return [json.loads(r[1]) async for r in cursor]

    async def sessions_get(self, session_id: str) -> dict | None:
        async with self._db.execute(
            "SELECT id, data FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return json.loads(row[1])

    async def sessions_upsert(self, session_id: str, data: dict, user_id: str = "") -> None:
        now = int(time.time() * 1000)
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO sessions (id, data, created_at, updated_at, user_id) VALUES (?, ?, ?, ?, ?)",
                (session_id, json.dumps(data, ensure_ascii=False), data.get("created_at", now), now, user_id),
            )
            await self._db.commit()

    async def sessions_delete(self, session_id: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await self._db.commit()
            return cursor.rowcount > 0

    async def sessions_delete_all(self, user_id: str = "") -> int:
        """删除所有会话（可按 user_id 过滤），返回删除条数。"""
        async with self._write_lock:
            if user_id:
                cursor = await self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            else:
                cursor = await self._db.execute("DELETE FROM sessions")
            await self._db.commit()
            return cursor.rowcount

    # ============ KV 操作 ============

    async def kv_get(self, key: str) -> str | None:
        async with self._db.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return row[0]

    async def kv_set(self, key: str, value: str) -> None:
        now = int(time.time() * 1000)
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )
            await self._db.commit()

    async def kv_delete(self, key: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute("DELETE FROM kv WHERE key = ?", (key,))
            await self._db.commit()
            return cursor.rowcount > 0

    # ============ Emoji Preferences 操作 ============

    async def emoji_prefs_all(self) -> list[dict]:
        """获取全部 emoji 偏好。"""
        async with self._db.execute(
            "SELECT scope, key, emoji_char FROM emoji_preferences ORDER BY updated_at"
        ) as cursor:
            return [{"scope": r[0], "key": r[1], "emoji_char": r[2]} async for r in cursor]

    async def emoji_pref_upsert(self, scope: str, key: str, emoji_char: str) -> None:
        """保存/更新一条 emoji 偏好。"""
        now = int(time.time() * 1000)
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO emoji_preferences (scope, key, emoji_char, updated_at) VALUES (?, ?, ?, ?)",
                (scope, key, emoji_char, now),
            )
            await self._db.commit()

    async def emoji_pref_delete(self, scope: str, key: str) -> bool:
        """删除一条 emoji 偏好。"""
        async with self._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM emoji_preferences WHERE scope = ? AND key = ?",
                (scope, key),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    # ============ Users 操作 ============

    async def user_create(self, user_id: str, username: str, password_hash: str, display_name: str = "") -> dict:
        """创建新用户。"""
        now = int(time.time() * 1000)
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO users (id, username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, password_hash, display_name, now),
            )
            await self._db.commit()
        return {"id": user_id, "username": username, "display_name": display_name, "created_at": now}

    async def user_get_by_username(self, username: str) -> dict | None:
        """根据用户名获取用户（含 password_hash）。"""
        async with self._db.execute(
            "SELECT id, username, password_hash, display_name, created_at FROM users WHERE username = ?",
            (username,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "created_at": row[4]}
            return None

    async def user_get_by_id(self, user_id: str) -> dict | None:
        """根据 ID 获取用户（不含 password_hash）。"""
        async with self._db.execute(
            "SELECT id, username, display_name, created_at FROM users WHERE id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "display_name": row[2], "created_at": row[3]}
            return None

    async def user_count(self) -> int:
        """获取用户总数。"""
        async with self._db.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    # ============ User Settings 操作 ============

    async def user_setting_get(self, user_id: str, key: str) -> str | None:
        """获取用户的某个设置值。"""
        async with self._db.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
            (user_id, key),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def user_setting_set(self, user_id: str, key: str, value: str) -> None:
        """设置用户的某个值。"""
        now = int(time.time() * 1000)
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO user_settings (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, key, value, now),
            )
            await self._db.commit()

    async def user_settings_all(self, user_id: str) -> dict[str, str]:
        """获取用户的所有设置。"""
        async with self._db.execute(
            "SELECT key, value FROM user_settings WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            return {row[0]: row[1] async for row in cursor}

    async def user_list_all(self) -> list[dict]:
        """获取所有用户列表（不含敏感信息）。"""
        async with self._db.execute(
            "SELECT id, username, display_name, created_at FROM users ORDER BY created_at"
        ) as cursor:
            return [
                {"id": row[0], "username": row[1], "display_name": row[2], "created_at": row[3]}
                async for row in cursor
            ]
