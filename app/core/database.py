from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "aether.db"


def _legacy_camera_config() -> dict | None:
    """检测 config.json 是否有旧 vision/ptz 段;有则返回三段合并 dict,无则 None。

    用于单路→多路迁移:老部署的 vision/ptz 段会被迁移成 cameras 表一行。
    全新部署(无这些段)返回 None,跳过迁移。
    """
    from .config import CONFIG
    has_vision = bool(CONFIG.get("vision"))
    has_ptz = bool(CONFIG.get("ptz"))
    auto_disp = CONFIG.get("automation", {}).get("camera_vl_display_enabled")
    if not (has_vision or has_ptz or auto_disp is not None):
        return None
    return {
        "vision": CONFIG.get("vision", {}),
        "ptz": CONFIG.get("ptz", {}),
        "automation": {"camera_vl_display_enabled": auto_disp},
    }


def _read_env_secret(name: str) -> str:
    """从 .env / os.environ 读一个明文密钥(迁移专用,读后旧 env 会被删)。"""
    return os.environ.get(name, "")


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
        # 设 Row 工厂:cameras_all/cameras_get 用 dict(r) 转字典;
        # Row 同时支持索引访问(r[1]),现有 rules_all 等不受影响。
        db.row_factory = aiosqlite.Row
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
                created_at INTEGER NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0
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

            CREATE TABLE IF NOT EXISTS cameras (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL DEFAULT '',
                enabled         INTEGER DEFAULT 1,
                sort_order      INTEGER DEFAULT 0,
                source_type     TEXT NOT NULL DEFAULT 'usb',
                usb_index       INTEGER,
                rtsp_url        TEXT DEFAULT '',
                rtsp_username   TEXT DEFAULT '',
                rtsp_password   TEXT DEFAULT '',
                area            TEXT DEFAULT '',
                device_mac      TEXT DEFAULT '',
                discovery_enabled INTEGER DEFAULT 1,
                ptz_enabled     INTEGER DEFAULT 0,
                ptz_ip          TEXT DEFAULT '',
                ptz_port        INTEGER DEFAULT 80,
                ptz_username    TEXT DEFAULT '',
                ptz_password    TEXT DEFAULT '',
                ptz_speed       REAL DEFAULT 0.5,
                ptz_step_ms     INTEGER DEFAULT 300,
                motion_hash_size           INTEGER DEFAULT 16,
                motion_threshold           INTEGER DEFAULT 15,
                motion_check_interval      REAL DEFAULT 1.0,
                vision_min_infer_interval   REAL DEFAULT 8.0,
                vision_max_idle_interval    REAL DEFAULT 120.0,
                vision_use_img_count        INTEGER DEFAULT 3,
                frame_interval_ms           INTEGER DEFAULT 2000,
                display_enabled             INTEGER DEFAULT 1,
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')*1000),
                updated_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')*1000)
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
        # 多摄像头:rules 表加 camera_id 列(空串=全局规则,归所有摄像头)
        await _ensure_column("rules", "camera_id", "camera_id TEXT DEFAULT ''")
        # 管理员分级（安全审计 2B）：旧库补 is_admin 列；无人是管理员时把
        # 最早注册的用户提升为管理员（存量部署的户主即首用户）
        await _ensure_column("users", "is_admin", "is_admin INTEGER NOT NULL DEFAULT 0")
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1") as cur:
            if (await cur.fetchone())[0] == 0:
                await db.execute(
                    "UPDATE users SET is_admin = 1 WHERE id = "
                    "(SELECT id FROM users ORDER BY created_at LIMIT 1)"
                )
        await db.commit()

        cls._db = db
        cls._write_lock = asyncio.Lock()
        cls._instance = cls()

        # —— 单摄像头 → 多路迁移(KV 标记幂等)——
        # D6:测试环境 conftest 默认 patch _legacy_camera_config=None 跳过迁移,
        # 防 vision/ptz 段污染全量回归;仅 @pytest.mark.migration 测试开启。
        if (await cls._instance.kv_get("cameras_migrated")) != "1":
            legacy = _legacy_camera_config()
            if legacy is not None:
                instance = cls._instance
                cid = f"cam_{secrets.token_hex(3)}"
                v = legacy.get("vision", {})
                p = legacy.get("ptz", {})
                rtsp_url = str(v.get("rtsp_url", ""))
                await instance.cameras_insert({
                    "id": cid, "name": "默认摄像头", "enabled": 1, "sort_order": 0,
                    "source_type": "rtsp" if rtsp_url else "usb",
                    "usb_index": int(v.get("camera_index", 0)) if not rtsp_url else None,
                    "rtsp_url": rtsp_url,
                    "rtsp_username": str(v.get("rtsp_username", "")),
                    "rtsp_password": _read_env_secret(
                        str(v.get("rtsp_password_env", "RTSP_PASSWORD"))),
                    "area": "", "device_mac": str(v.get("device_mac", "")),
                    "discovery_enabled": 1,
                    "ptz_enabled": 1 if p.get("enabled") else 0,
                    "ptz_ip": str(p.get("ip", "")),
                    "ptz_port": int(p.get("port", 80)),
                    "ptz_username": str(p.get("username", "")),
                    "ptz_password": _read_env_secret(
                        str(p.get("password_env", "PTZ_PASSWORD"))),
                    "ptz_speed": float(p.get("speed", 0.5)),
                    "ptz_step_ms": int(p.get("step_ms", 300)),
                    "motion_hash_size": int(v.get("motion_hash_size", 16)),
                    "motion_threshold": int(v.get("motion_threshold", 15)),
                    "motion_check_interval": float(v.get("motion_check_interval_seconds", 1.0)),
                    "vision_min_infer_interval": float(v.get("min_infer_interval_seconds", 8.0)),
                    "vision_max_idle_interval": float(v.get("max_idle_interval_seconds", 120.0)),
                    "vision_use_img_count": int(v.get("vision_use_img_count", 3)),
                    "frame_interval_ms": int(v.get("frame_interval_ms", 2000)),
                    "display_enabled": 1 if legacy.get("automation", {}).get("camera_vl_display_enabled") else 0,
                })
                # 把现有规则的 camera_id 回填到新 id(data JSON blob + 列都设)
                async with db.execute("SELECT id, data FROM rules") as cur:
                    for row in await cur.fetchall():
                        d = json.loads(row[1]) if row[1] else {}
                        d["camera_id"] = cid
                        await db.execute(
                            "UPDATE rules SET data = ?, camera_id = ? WHERE id = ?",
                            (json.dumps(d, ensure_ascii=False), cid, row[0]))
                # vision_focuses KV 每条加 camera_id(若存在)
                fv = await instance.kv_get("vision_focuses")
                if fv:
                    try:
                        arr = json.loads(fv)
                        for item in arr:
                            item.setdefault("camera_id", cid)
                        await instance.kv_set("vision_focuses", json.dumps(arr, ensure_ascii=False))
                    except (ValueError, TypeError):
                        pass
                await db.commit()
                # 只在真迁移了才置 KV 标记。全新部署(legacy is None)不置位:
                # 这样 KV=="1" 严格表示"已从 legacy 迁移过",不污染全新部署的判定。
                await cls._instance.kv_set("cameras_migrated", "1")

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

    # ============ Cameras 操作(多摄像头)============

    # cameras 表列名(与 DDL 一一对应,供 insert/update 校验)。
    # D8:不含 vision_jpeg_quality/vision_downscale —— VLM 编码参数全局统一,
    # per-camera 列会"存了不生效"。
    _CAMERA_COLS = (
        "id", "name", "enabled", "sort_order", "source_type", "usb_index",
        "rtsp_url", "rtsp_username", "rtsp_password", "area", "device_mac",
        "discovery_enabled", "ptz_enabled", "ptz_ip", "ptz_port",
        "ptz_username", "ptz_password", "ptz_speed", "ptz_step_ms",
        "motion_hash_size", "motion_threshold", "motion_check_interval",
        "vision_min_infer_interval", "vision_max_idle_interval",
        "vision_use_img_count", "frame_interval_ms", "display_enabled",
    )

    async def cameras_all(self) -> list[dict]:
        """获取所有摄像头,按 sort_order 排序。"""
        async with self._db.execute(
            "SELECT * FROM cameras ORDER BY sort_order, created_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def cameras_get(self, camera_id: str) -> dict | None:
        """按 id 取单路;不存在返回 None。"""
        async with self._db.execute(
            "SELECT * FROM cameras WHERE id = ?", (camera_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def cameras_insert(self, data: dict) -> str:
        """插入一路摄像头。data 必须含 id;只插 data 内的列,其余靠 DB DEFAULT。

        CameraManager 空表播种与 /api/cameras POST 都受益——只传必要字段,
        motion/vision 等列靠 schema DEFAULT(15 / 3.0 / ...),避免显式插 None
        覆盖 DEFAULT 导致 CameraStream 构造时 int(None) 崩。
        """
        cols = [c for c in self._CAMERA_COLS if c in data]
        values = [data[c] for c in cols]
        placeholders = ",".join(["?"] * len(cols))
        col_list = ",".join(cols)
        async with self._write_lock:
            await self._db.execute(
                f"INSERT INTO cameras ({col_list}) VALUES ({placeholders})",
                values,
            )
            await self._db.commit()
        return data["id"]

    async def cameras_update(self, camera_id: str, fields: dict) -> bool:
        """部分更新指定列(id/created_at/updated_at 不可改)。自动刷 updated_at。"""
        if not fields:
            return False
        forbidden = {"id", "created_at", "updated_at"}
        set_cols = [k for k in fields if k in self._CAMERA_COLS and k not in forbidden]
        if not set_cols:
            return False
        now = int(time.time() * 1000)
        assignments = ",".join([f"{c} = ?" for c in set_cols])
        params = [fields[c] for c in set_cols] + [now, camera_id]
        async with self._write_lock:
            cursor = await self._db.execute(
                f"UPDATE cameras SET {assignments}, updated_at = ? WHERE id = ?", params
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def cameras_delete(self, camera_id: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM cameras WHERE id = ?", (camera_id,)
            )
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

    async def prefs_get_by_scope(self, scope: str) -> dict[str, str]:
        """取某 scope 下全部 {key: value} 偏好（复用 emoji_preferences 表）。

        entity_alias（实体别名）等用户自定义映射也存这张表，scope 区分用途。
        """
        async with self._db.execute(
            "SELECT key, emoji_char FROM emoji_preferences WHERE scope = ?",
            (scope,),
        ) as cursor:
            return {r[0]: r[1] async for r in cursor}

    # ============ Users 操作 ============

    async def user_create(
        self, user_id: str, username: str, password_hash: str,
        display_name: str = "", is_admin: int = 0,
    ) -> dict:
        """创建新用户（首个用户由调用方传 is_admin=1）。"""
        now = int(time.time() * 1000)
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO users (id, username, password_hash, display_name, created_at, is_admin) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, password_hash, display_name, now, is_admin),
            )
            await self._db.commit()
        return {"id": user_id, "username": username, "display_name": display_name, "created_at": now, "is_admin": is_admin}

    async def user_get_by_username(self, username: str) -> dict | None:
        """根据用户名获取用户（含 password_hash）。"""
        async with self._db.execute(
            "SELECT id, username, password_hash, display_name, created_at, is_admin FROM users WHERE username = ?",
            (username,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "created_at": row[4], "is_admin": row[5]}
            return None

    async def user_count(self) -> int:
        """用户总数（注册时判定「首用户即管理员」）。"""
        async with self._db.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def user_get_by_id(self, user_id: str) -> dict | None:
        """根据 ID 获取用户（不含 password_hash）。"""
        async with self._db.execute(
            "SELECT id, username, display_name, created_at, is_admin FROM users WHERE id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "display_name": row[2], "created_at": row[3], "is_admin": row[4]}
            return None

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
