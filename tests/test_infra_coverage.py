"""基础设施/服务/运维覆盖补充测试。

目标模块：app/container.py、app/core/ws_registry.py、app/core/net_guard.py、
app/core/database.py（自愈/恢复/CRUD 分支）、app/services/*（session_store /
summarization / prompt / weekly_report）、app/ops/*（diag / audit / auto_update /
backup）、app/main.py（中间件、辅助函数、lifespan）。

所有外部边界均 mock：LLM 调用、子进程/时间、alert 广播；数据库一律 tmp_path
隔离并在测试结束关闭（防 aiosqlite 非 daemon 线程卡死进程退出）。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.core.database import Database
from app.core import database as dbmod


# ============================================================================
# 公共夹具
# ============================================================================

@pytest.fixture(autouse=True)
def _db_singleton_clean():
    """每条测试前后回收 Database 单例与失联连接（防 pytest 退出挂起）。"""
    if Database._db is not None or Database._open_conns:
        asyncio.run(Database.close_all())
    Database._instance = None
    Database._db = None
    Database._write_lock = None
    yield
    if Database._db is not None or Database._open_conns:
        asyncio.run(Database.close_all())
    Database._instance = None
    Database._db = None
    Database._write_lock = None


@pytest.fixture
async def tdb(tmp_path, monkeypatch):
    """指向 tmp_path 的已初始化 Database（用完即关）。"""
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "t.db")
    inst = await Database.init()
    yield inst
    await Database.close()


# ============================================================================
# app/container.py
# ============================================================================

class TestContainer:
    def _services(self):
        mk = lambda: SimpleNamespace(reload=Mock())
        return {
            "ha_service": SimpleNamespace(), "llm_chat_client": mk(),
            "vision_client": mk(), "embed_client": mk(),
            "session_store": SimpleNamespace(), "vision_service": SimpleNamespace(),
            "vision_key_pool": SimpleNamespace(reload=Mock()), "rule_service": SimpleNamespace(),
            "rule_registry_service": SimpleNamespace(), "automation_service": SimpleNamespace(),
            "summarization_service": SimpleNamespace(), "llm_settings_service": SimpleNamespace(),
            "emoji_service": SimpleNamespace(), "mcp_client_manager": SimpleNamespace(),
            "tool_executor": SimpleNamespace(), "sg_service": SimpleNamespace(),
            "ha_client_ref": ["ha"], "automation_agent_ref": [None],
            "ha_catalog_cache_ref": [""], "ha_controls_cache_ref": [""],
        }

    def test_get_container_uninitialized_raises(self, monkeypatch):
        from app import container as container_mod
        monkeypatch.setattr(container_mod, "_container", None)
        with pytest.raises(RuntimeError, match="not initialized"):
            container_mod.get_container()

    def test_init_container_wires_refs_and_reload(self, monkeypatch):
        from app import container as container_mod
        monkeypatch.setattr(container_mod, "_container", None)
        services = self._services()
        metrics = SimpleNamespace()
        c = container_mod.init_container(services, metrics)
        assert container_mod.get_container() is c
        # 动态属性：ha_client 读取 ref[0]，热替换可见
        assert c.ha_client == "ha"
        services["ha_client_ref"][0] = "ha2"
        assert c.ha_client == "ha2"
        # controls 缓存是容器新建的 list
        assert c.ha_controls_cache_ref == [""]
        # reload_all_clients：三个客户端 + vision_key_pool 池 reload + rag 钩子
        # （池漏 reload 会让迁移/自愈后视觉链路持续拿旧 key 401）
        c.reload_all_clients()
        for name in ("llm_chat_client", "vision_client", "embed_client", "vision_key_pool"):
            services[name].reload.assert_called_once()
        # rag_service 为 None 时不炸（分支覆盖）
        assert c.rag_service is None
        rag = Mock()
        c.rag_service = rag
        c.reload_all_clients()
        rag.maybe_rebuild_if_model_changed.assert_called_once()


# ============================================================================
# app/core/ws_registry.py
# ============================================================================

class _FakeWS:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_json(self, payload):
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(payload)


class TestWsRegistry:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        from app.core import ws_registry
        monkeypatch.setattr(ws_registry, "_sockets", {})
        self.reg = ws_registry
        yield

    async def test_register_push_unregister_lifecycle(self):
        ws = _FakeWS()
        self.reg.register("u1", ws)
        self.reg.register("u1", ws)  # set 去重
        ws2 = _FakeWS()
        self.reg.register("u1", ws2)
        await self.reg.push_to_user("u1", {"type": "ping"})
        assert ws.sent == [{"type": "ping"}]
        assert ws2.sent == [{"type": "ping"}]
        self.reg.unregister("u1", ws)
        await self.reg.push_to_user("u1", {"type": "again"})
        assert ws2.sent[-1] == {"type": "again"}
        self.reg.unregister("u1", ws2)
        # 空 set 自动清理：注册表回到空
        assert self.reg._sockets == {}
        # 未注册用户 push 是 no-op
        await self.reg.push_to_user("ghost", {"x": 1})

    async def test_push_failure_isolated(self):
        bad, good = _FakeWS(fail=True), _FakeWS()
        self.reg.register("u", bad)
        self.reg.register("u", good)
        await self.reg.push_to_user("u", {"m": 1})  # 单连接失败不影响其余
        assert good.sent == [{"m": 1}]

    async def test_push_to_all(self):
        w1, w2 = _FakeWS(), _FakeWS()
        self.reg.register("a", w1)
        self.reg.register("b", w2)
        await self.reg.push_to_all({"alert": True})
        assert w1.sent == [{"alert": True}]
        assert w2.sent == [{"alert": True}]


# ============================================================================
# app/core/net_guard.py
# ============================================================================

class TestNetGuard:
    def test_urlparse_valueerror_branch(self):
        from app.core.net_guard import url_scheme_error, HTTP_SCHEMES
        # 非法 IPv6 触发 urlparse 内部 ValueError → 兜底错误文案
        assert url_scheme_error("http://[::1", HTTP_SCHEMES) == "URL 格式无效"

    def test_link_local_ipv4_allowed(self):
        from app.core.net_guard import is_lan_ipv4
        assert is_lan_ipv4("169.254.10.5") is True
        assert is_lan_ipv4(" 127.0.0.1 ") is True  # strip 生效


# ============================================================================
# app/core/database.py — 内部工具 / 守卫 / 自愈
# ============================================================================

class TestDatabaseHelpers:
    @pytest.mark.migration
    def test_legacy_camera_config_detection(self, monkeypatch):
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "vision": {"rtsp_url": "rtsp://x"},
            "automation": {"camera_vl_display_enabled": True},
        })
        legacy = dbmod._legacy_camera_config()
        assert legacy["vision"]["rtsp_url"] == "rtsp://x"
        assert legacy["automation"]["camera_vl_display_enabled"] is True
        # 全新部署（三段全空）→ None
        monkeypatch.setattr(cfg, "CONFIG", {})
        assert dbmod._legacy_camera_config() is None

    def test_read_env_secret(self, monkeypatch):
        monkeypatch.setenv("AETHER_TEST_SECRET_X", "v123")
        assert dbmod._read_env_secret("AETHER_TEST_SECRET_X") == "v123"
        assert dbmod._read_env_secret("AETHER_TEST_MISSING") == ""

    def test_get_and_ctor_guards(self):
        with pytest.raises(RuntimeError, match="Database not initialized"):
            Database.get()
        with pytest.raises(RuntimeError, match="Database not initialized"):
            Database()

    async def test_init_failure_cleans_connection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "x.db")

        async def boom(db):
            raise RuntimeError("migration boom")

        monkeypatch.setattr(Database, "_finish_init", boom)
        with pytest.raises(RuntimeError, match="migration boom"):
            await Database.init()
        # 连接必须被回收，不留非 daemon 线程
        assert Database._db is None
        assert Database._open_conns == []

    async def test_init_twice_returns_same_instance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "idem" / "aether.db")
        a = await Database.init()
        b = await Database.init()
        assert a is b
        await Database.close()

    async def test_init_finish_failure_with_failing_close(self, tmp_path, monkeypatch):
        """建表失败且连接 close 也失败：内层吞掉 close 异常后仍外抛原错误。"""
        from unittest.mock import MagicMock
        monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "x" / "aether.db")

        class _BadConn:
            row_factory = None

            async def execute(self, *a, **k):
                return MagicMock()

            async def close(self):
                raise RuntimeError("close also fails")

        async def fake_connect(*a, **k):
            return _BadConn()

        monkeypatch.setattr(dbmod.aiosqlite, "connect", fake_connect)

        async def boom(db):
            raise RuntimeError("migration boom")

        monkeypatch.setattr(Database, "_finish_init", boom)
        with pytest.raises(RuntimeError, match="migration boom"):
            await Database.init()
        assert Database._db is None

    async def test_open_connection_probe_failure_with_failing_close_recovers(
            self, tmp_path, monkeypatch):
        """探针失败且 close 也失败：走自愈路径仍能拿到可用新库。"""
        from unittest.mock import MagicMock
        p = tmp_path / "rec" / "a" / "aether.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"not a database")
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        calls = {"n": 0}

        class _Conn:
            row_factory = None

            async def execute(self, sql, *a, **k):
                calls["n"] += 1
                if "sqlite_master" in sql:
                    raise RuntimeError("corrupt probe")
                return MagicMock()

            async def close(self):
                raise RuntimeError("close fails too")

        async def fake_connect(*a, **k):
            return _Conn()

        import aiosqlite as _aiosqlite
        real_connect = _aiosqlite.connect
        connect_calls = {"n": 0}

        async def flaky_connect(*a, **k):
            connect_calls["n"] += 1
            if connect_calls["n"] == 1:  # 仅首次返回"探针失败且关不掉"的假连接
                return await fake_connect(*a, **k)
            return await real_connect(*a, **k)

        monkeypatch.setattr(dbmod.aiosqlite, "connect", flaky_connect)
        inst = await Database.init()
        assert connect_calls["n"] >= 2  # 自愈路径确实用真 connect 建了新库
        await inst.kv_set("k", "v")
        assert await inst.kv_get("k") == "v"
        await Database.close()

    async def test_move_corrupt_files_failure_forces_delete(self, tmp_path, monkeypatch):
        """move 失败 → 强制删除现场文件 → 裸建新库。"""
        p = tmp_path / "rec" / "a" / "aether.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"corrupt!")
        (tmp_path / "rec" / "a" / "aether.db-wal").write_bytes(b"wal")
        monkeypatch.setattr(dbmod, "DB_PATH", p)

        def broken_move(corrupt):
            raise RuntimeError("move failed")

        monkeypatch.setattr(Database, "_move_corrupt_files", broken_move)
        inst = await Database.init()
        await inst.kv_set("k", "v")
        assert await inst.kv_get("k") == "v"
        # move 失败 → 没有 corrupt 现场文件留存（对照 self_heals 用例的 glob 断言）
        assert not list(p.parent.glob("aether.db.corrupt-*"))
        await Database.close()

    async def test_force_delete_tolerates_locked_side_files(self, tmp_path, monkeypatch):
        """强制删除时个别文件被占用（OSError）→ 跳过继续，不影响建新库。"""
        import os
        p = tmp_path / "rec2" / "a" / "aether.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"corrupt!")
        wal = tmp_path / "rec2" / "a" / "aether.db-wal"
        wal.write_bytes(b"wal")
        monkeypatch.setattr(dbmod, "DB_PATH", p)

        def broken_move(corrupt):
            raise RuntimeError("move failed")

        monkeypatch.setattr(Database, "_move_corrupt_files", broken_move)
        fd = os.open(str(wal), os.O_RDWR)  # 占住 -wal 文件句柄 → unlink 报 OSError 被跳过
        try:
            # 被锁的垃圾 -wal 残留会让新库也起不来，init 最终仍失败；
            # 但删除循环没有因单个文件失败而中断：损坏的主库内容已被清掉
            with pytest.raises(Exception):
                await Database.init()
            assert p.read_bytes() != b"corrupt!"
        finally:
            os.close(fd)

    async def test_restored_backup_still_fails_falls_back_to_fresh(
            self, tmp_path, monkeypatch):
        """备份能通过 sqlite3 探针但 aiosqlite 打不开 → 删除备份副本，裸建新库。"""
        import aiosqlite as _aiosqlite
        import sqlite3
        p = tmp_path / "rec" / "a" / "aether.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"corrupt!")
        backup_root = tmp_path / "backups"
        backup_root.mkdir()
        good = backup_root / "aether.db"
        conn = sqlite3.connect(good)
        conn.execute("CREATE TABLE marker(v TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        real_connect = _aiosqlite.connect
        calls = {"n": 0}

        async def flaky_connect(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:  # 第2次 = 恢复后的 _open_connection
                raise RuntimeError("restored open fails")
            return await real_connect(*a, **k)

        monkeypatch.setattr(dbmod.aiosqlite, "connect", flaky_connect)
        inst = await Database.init()
        async with inst._db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='marker'") as cur:
            assert (await cur.fetchone())[0] == 0  # 备份内容没混进来
        await Database.close()

    async def test_close_all_recovers_orphans_even_if_close_fails(self):
        class _BadConn:
            async def close(self):
                raise RuntimeError("already broken")

        Database._open_conns.append(_BadConn())
        n = await Database.close_all()
        assert n == 1
        assert Database._open_conns == []
        # 空注册表再关一次：0
        assert await Database.close_all() == 0

    def test_move_corrupt_files(self, tmp_path, monkeypatch):
        base = tmp_path / "a"
        base.mkdir()
        monkeypatch.setattr(dbmod, "DB_PATH", base / "aether.db")
        (base / "aether.db").write_bytes(b"junk")
        (base / "aether.db-wal").write_bytes(b"wal")
        corrupt = base / "aether.db.corrupt-1"
        Database._move_corrupt_files(corrupt)
        assert corrupt.read_bytes() == b"junk"
        assert Path(str(corrupt) + "-wal").read_bytes() == b"wal"
        assert not (base / "aether.db").exists()


class TestDatabaseRecovery:
    async def test_corrupt_db_self_heals_to_fresh(self, tmp_path, monkeypatch):
        # DB_PATH 需为 tmp_path 下三层：backups 根 = DB_PATH.parent.parent.parent
        p = tmp_path / "rec" / "a" / "aether.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"this is not sqlite at all")
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        inst = await Database.init()
        # 裸建的新库可用，且坏文件留了现场
        await inst.kv_set("k", "v")
        assert await inst.kv_get("k") == "v"
        assert list(p.parent.glob("aether.db.corrupt-*")), "corrupt 文件应被保留"
        await Database.close()

    async def test_corrupt_db_restored_from_backup(self, tmp_path, monkeypatch):
        p = tmp_path / "rec" / "a" / "aether.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"corrupt!")
        backup_root = tmp_path / "backups"
        backup_root.mkdir()
        good = backup_root / "aether.db"
        conn = sqlite3.connect(good)
        conn.execute("CREATE TABLE marker(v TEXT)")
        conn.execute("INSERT INTO marker VALUES('kept')")
        conn.commit()
        conn.close()
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        inst = await Database.init()
        async with inst._db.execute("SELECT v FROM marker") as cur:
            assert (await cur.fetchone())[0] == "kept"
        await Database.close()

    async def test_all_backups_corrupt_falls_back_to_fresh(self, tmp_path, monkeypatch):
        p = tmp_path / "rec" / "a" / "aether.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"corrupt!")
        backup_root = tmp_path / "backups"
        backup_root.mkdir()
        bad = backup_root / "aether.db"
        bad.write_bytes(b"garbage backup")
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        inst = await Database.init()
        await inst.kv_set("k", "v")
        assert await inst.kv_get("k") == "v"
        async with inst._db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='marker'") as cur:
            assert (await cur.fetchone())[0] == 0  # 坏备份内容没有混进来
        await Database.close()


class TestDatabaseCrudBranches:
    async def test_rules_user_filter(self, tdb):
        await tdb.rules_insert("r1", {"n": 1}, user_id="u1")
        await tdb.rules_insert("r2", {"n": 2}, user_id="u2")
        assert [r["n"] for r in await tdb.rules_all("u1")] == [1]
        assert len(await tdb.rules_all()) == 2
        assert await tdb.rules_update("missing", {}) is False

    async def test_cameras_update_and_remap(self, tdb):
        await tdb.cameras_insert({"id": "c1", "name": "客厅", "frame_interval_ms": 2000})
        # 空字段 / 仅禁止字段 → False（分支）
        assert await tdb.cameras_update("c1", {}) is False
        assert await tdb.cameras_update("c1", {"id": "x", "created_at": 1}) is False
        assert await tdb.cameras_update("nope", {"name": "x"}) is False
        assert await tdb.cameras_update("c1", {"name": "卧房"}) is True
        assert (await tdb.cameras_get("c1"))["name"] == "卧房"
        assert await tdb.cameras_get("ghost") is None
        # frame_interval 一次性迁移：只动等于旧默认值的行，幂等
        assert await tdb.cameras_remap_frame_interval(2000, 1000) == 1
        assert await tdb.cameras_remap_frame_interval(2000, 1000) == 0
        assert (await tdb.cameras_get("c1"))["frame_interval_ms"] == 1000

    async def test_vision_logs_insert_prune_tail_delete(self, tdb, monkeypatch):
        class _FakeTime:
            @staticmethod
            def time():
                return 1_000_000_000.0  # ms 尾数 000 → %50 == 0 触发采样修剪

        monkeypatch.setattr(dbmod, "time", _FakeTime)
        monkeypatch.setattr(Database, "VISION_LOG_MAX_ROWS", 3)
        for i in range(5):
            await tdb.vision_log_insert("camA", "preview", {"i": i})
        rows = await tdb.vision_logs_tail()
        assert len(rows) == 3  # 超限修剪生效
        assert rows[0]["content"] == {"i": 4}  # 新在前
        # 过滤分支：camera_id + kind
        await tdb.vision_log_insert("camB", "motion", {"i": 9})
        only_b = await tdb.vision_logs_tail(camera_id="camB")
        assert len(only_b) == 1 and only_b[0]["camera_id"] == "camB"
        only_kind = await tdb.vision_logs_tail(camera_id="camB", kind="motion")
        assert only_kind[0]["kind"] == "motion"
        assert await tdb.vision_logs_tail(camera_id="camB", kind="nomatch") == []
        n = await tdb.vision_logs_delete_camera("camB")
        assert n == 1
        assert await tdb.vision_logs_tail(camera_id="camB") == []

    async def test_family_events_and_stats(self, tdb):
        await tdb.family_event_add("device_op", "light.li", "AI将「客厅灯」执行 打开", actor="AI")
        await tdb.family_event_add("device_op", "light.li", "手动关闭了客厅灯", actor="手动")
        await tdb.family_event_add("device_op", "switch.k2", "触发开关", actor="")
        await tdb.family_event_add("automation", "rule1", "规则触发")
        await tdb.family_event_add("task_success", "cron", "ok")
        await tdb.family_event_add("task_failed", "cron", "bad")
        await tdb.family_event_add("alert", "cam", "掉线")
        await tdb.family_event_add("alert_resolved", "cam", "恢复")
        await tdb.family_event_add("device_state", "sensor.t", "25")
        all_events = await tdb.family_events_since(0)
        assert len(all_events) == 9
        filtered = await tdb.family_events_since(0, kinds=["alert", "alert_resolved"])
        assert [e["kind"] for e in filtered] == ["alert", "alert_resolved"]
        stats = await tdb.family_events_stats(0)
        assert stats["totals"]["device_op"] == 3
        daily = stats["daily"][0]
        assert daily["device_op"] == 3 and daily["automation"] == 1
        assert daily["task"] == 2 and daily["alert"] == 2  # alert_resolved 并入 alert 列
        top = {d["entity"]: d for d in stats["top_devices"]}
        # source 无 "device:" 前缀时 entity 即 source 原样（split(":",1)[-1]）
        assert top["light.li"]["count"] == 2 and top["light.li"]["ai"] == 1 and top["light.li"]["manual"] == 1
        assert top["light.li"]["name"] == "客厅灯"  # 从「」提取名称
        assert top["switch.k2"]["name"] == "switch.k2"  # 无「」回退 source 原样
        assert top["switch.k2"]["ai"] == 1  # 非「手动」前缀归 AI
        assert stats["actor"] == {"ai": 2, "manual": 1}

    async def test_scenes_crud(self, tdb):
        await tdb.scenes_upsert("s1", "回家", [{"t": "light.on"}], user_id="u1")
        await tdb.scenes_upsert("s1", "回家改", [{"t": "light.off"}])  # 冲突更新
        got = await tdb.scenes_get("s1")
        assert got["name"] == "回家改" and got["actions"] == [{"t": "light.off"}]
        assert got["user_id"] == "u1"
        assert (await tdb.scenes_get("ghost")) is None
        await tdb.scenes_upsert("s2", "离家", [])
        assert len(await tdb.scenes_all()) == 2
        assert await tdb.scenes_delete("s1") is True
        assert await tdb.scenes_delete("s1") is False

    async def test_scheduled_tasks_crud(self, tdb):
        await tdb.scheduled_task_insert("t1", {"at": "10:00"})
        await tdb.scheduled_task_insert("t2", {"every": 60})
        assert len(await tdb.scheduled_tasks_all()) == 2
        assert await tdb.scheduled_task_update("t1", {"at": "11:00"}) is True
        assert await tdb.scheduled_task_update("ghost", {}) is False
        assert (await tdb.scheduled_tasks_all())[0] == {"at": "11:00"}
        assert await tdb.scheduled_task_delete("t2") is True
        assert await tdb.scheduled_task_delete("t2") is False

    async def test_sessions_filters_and_delete_all(self, tdb):
        await tdb.sessions_upsert("s1", {"session_id": "s1", "created_at": 5}, user_id="u1")
        await tdb.sessions_upsert("s2", {"session_id": "s2"}, user_id="u2")
        assert [d["session_id"] for d in await tdb.sessions_all("u1")] == ["s1"]
        assert len(await tdb.sessions_all()) == 2
        assert await tdb.sessions_delete_all("u1") == 1
        assert await tdb.sessions_delete_all() == 1
        assert await tdb.sessions_delete_all() == 0

    async def test_emoji_and_scope_prefs(self, tdb):
        await tdb.emoji_pref_upsert("emoji", "灯", "💡")
        await tdb.emoji_pref_upsert("entity_alias", "客厅灯", "light.li")
        assert {"scope": "emoji", "key": "灯", "emoji_char": "💡"} in await tdb.emoji_prefs_all()
        assert await tdb.prefs_get_by_scope("entity_alias") == {"客厅灯": "light.li"}
        await tdb.emoji_pref_upsert("emoji", "灯", "🌟")  # REPLACE 语义
        assert await tdb.prefs_get_by_scope("emoji") == {"灯": "🌟"}
        assert await tdb.emoji_pref_delete("emoji", "灯") is True
        assert await tdb.emoji_pref_delete("emoji", "灯") is False

    async def test_users_and_settings(self, tdb):
        u = await tdb.user_create("id1", "alice", "hash1", "小爱", is_admin=1)
        assert u["is_admin"] == 1
        await tdb.user_create("id2", "bob", "hash2")
        got = await tdb.user_get_by_username("alice")
        assert got["password_hash"] == "hash1"
        assert await tdb.user_get_by_username("ghost") is None
        assert await tdb.user_count() == 2
        by_id = await tdb.user_get_by_id("id2")
        assert by_id["username"] == "bob" and "password_hash" not in by_id
        assert await tdb.user_get_by_id("ghost") is None
        listing = await tdb.user_list_all()
        assert [x["username"] for x in listing] == ["alice", "bob"]  # 按 created_at 序
        # user settings
        await tdb.user_setting_set("id1", "theme", "dark")
        assert await tdb.user_setting_get("id1", "theme") == "dark"
        assert await tdb.user_setting_get("id1", "nope") is None
        await tdb.user_setting_set("id1", "lang", "zh")
        assert await tdb.user_settings_all("id1") == {"theme": "dark", "lang": "zh"}


@pytest.mark.migration
class TestLegacyCameraMigration:
    async def test_migration_backfills_rules_and_focuses(self, tmp_path, monkeypatch):
        p = tmp_path / "mig" / "aether.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        monkeypatch.setenv("RTSP_PASSWORD", "rtsp-sekrit")
        monkeypatch.setenv("PTZ_PW", "ptz-sekrit")
        legacy = {
            "vision": {"rtsp_url": "rtsp://1.1.1.1/s", "device_mac": "aa:bb",
                       "rtsp_password_env": "RTSP_PASSWORD"},
            "ptz": {"enabled": True, "ip": "1.2.3.4", "port": 81, "username": "u",
                    "password_env": "PTZ_PW", "speed": 0.7, "step_ms": 250},
            "automation": {"camera_vl_display_enabled": True},
        }
        # 第一次 init：无 legacy → 只建表；预置存量 rule 与 vision_focuses
        monkeypatch.setattr(dbmod, "_legacy_camera_config", lambda: None)
        db1 = await Database.init()
        await db1.rules_insert("r1", {"name": "old"})
        await db1.kv_set("vision_focuses", json.dumps([{"text": "看门"}]))
        await Database.close()
        # 第二次 init：legacy 出现 → 触发单路→多路迁移
        monkeypatch.setattr(dbmod, "_legacy_camera_config", lambda: legacy)
        db2 = await Database.init()
        cams = await db2.cameras_all()
        assert len(cams) == 1
        cam = cams[0]
        assert cam["source_type"] == "rtsp"
        assert cam["rtsp_password"] == "rtsp-sekrit"
        assert cam["ptz_enabled"] == 1 and cam["ptz_port"] == 81
        assert cam["ptz_password"] == "ptz-sekrit"
        assert cam["display_enabled"] == 1
        cid = cam["id"]
        assert await db2.kv_get("cameras_migrated") == "1"
        async with db2._db.execute("SELECT data FROM rules WHERE id='r1'") as cur:
            (data,) = await cur.fetchone()
        # camera_id 列已删（死列），绑定只存在于 data JSON
        assert json.loads(data)["camera_id"] == cid
        focuses = json.loads(await db2.kv_get("vision_focuses"))
        assert focuses[0]["camera_id"] == cid
        await Database.close()

    async def test_migration_survives_bad_focuses_json(self, tmp_path, monkeypatch):
        p = tmp_path / "mig2" / "aether.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        monkeypatch.setattr(dbmod, "_legacy_camera_config", lambda: None)
        db1 = await Database.init()
        await db1.kv_set("vision_focuses", "not-json{")
        await Database.close()
        monkeypatch.setattr(dbmod, "_legacy_camera_config", lambda: {
            "vision": {}, "ptz": {}, "automation": {}})
        db2 = await Database.init()  # 坏 JSON 不阻塞迁移
        assert len(await db2.cameras_all()) == 1
        await Database.close()


# ============================================================================
# app/services/session_store.py
# ============================================================================

def _make_instruction(ns: str, name: str) -> "object":
    from app.schema.chat_schema import Header, Instruction
    return Instruction(header=Header(type="instruction", namespace=ns, name=name,
                                     timestamp=0, request_id="r", session_id="s"),
                       payload={"stream": "x"})


class TestSessionStateViews:
    def test_title_and_visible_and_debug(self):
        from app.services.session_store import SessionState
        s = SessionState(session_id="sid", request_id="r")
        s.model_messages = [{"role": "user", "content": "x" * 40}]
        assert s.title() == "x" * 30  # 截断到 30
        s.model_messages = [{"role": "assistant", "content": "hi"}]
        assert s.title() == "sid"  # 无 user 消息回退 id
        s.model_messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        visible = s.visible_messages()
        assert visible[0]["message_id"] == "0" and visible[1]["role"] == "assistant"
        s.history_instructions = [
            _make_instruction("Template", "ToastStream"),
            _make_instruction("Template", "Speak"),
        ]
        debug = s.debug_events()
        assert [d["type"] for d in debug] == ["Template.Speak"]  # ToastStream 跳过
        summary = s.summary()
        assert summary["message_count"] == 2 and summary["id"] == "sid"


class TestSessionStorePersistence:
    @pytest.fixture
    async def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "sess.db")
        db = await Database.init()
        from app.services.session_store import SessionStore
        store = SessionStore()
        yield store, db
        await store.shutdown()
        await Database.close()

    async def test_load_from_db_and_get_or_create(self, env):
        store, db = env
        await db.sessions_upsert("s1", {"session_id": "s1", "request_id": "r",
                                        "user_id": "u1",
                                        "model_messages": [{"role": "user", "content": "hi"}]},
                                 user_id="u1")
        await store.load_from_db()
        got = await store.get_session("s1")
        assert got is not None and got.user_id == "u1"
        await store.load_from_db()  # 幂等：重复加载 no-op
        # get_or_create 更新内存里的 request_id/user_id，但不再整序列化落库
        # （轮首只改了 request_id，全量重 dump 是纯浪费；轮末 store_session 落盘）
        s = await store.get_or_create("s1", "r2", user_id="u9")
        assert s.request_id == "r2" and s.user_id == "u9"
        await store.shutdown()
        async with db._db.execute("SELECT user_id FROM sessions WHERE id='s1'") as cur:
            assert (await cur.fetchone())[0] == "u1"  # 轮首未写库
        await store.store_session(s)
        await store.shutdown()
        async with db._db.execute("SELECT user_id FROM sessions WHERE id='s1'") as cur:
            assert (await cur.fetchone())[0] == "u9"  # store_session 落盘

    async def test_load_from_db_failure_starts_fresh(self, env, monkeypatch):
        store, _ = env
        monkeypatch.setattr(dbmod.Database, "get", Mock(side_effect=RuntimeError("db down")))
        await store.load_from_db()  # 失败不抛，标记已加载
        assert store._loaded is True

    def test_save_and_delete_without_loop_is_noop(self):
        from app.services.session_store import SessionState, SessionStore
        store = SessionStore()
        s = SessionState(session_id="x", request_id="r")
        store._save_session_async(s)  # 无事件循环 → RuntimeError 分支吞掉
        store._delete_session_async("x")
        assert store._pending_tasks == set()

    async def test_save_with_retry_then_success(self, env, monkeypatch):
        store, _ = env
        calls = {"n": 0}

        class FlakyDb:
            async def sessions_upsert(self, *a, **k):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("db busy")

        monkeypatch.setattr(dbmod.Database, "get", lambda: FlakyDb())
        await store._save_with_retry("sid", {}, "", prev=None)
        assert calls["n"] == 3  # 重试 2 次后成功

    async def test_save_with_retry_exhausted_no_raise(self, env, monkeypatch):
        store, _ = env

        class DeadDb:
            async def sessions_upsert(self, *a, **k):
                raise RuntimeError("gone")

        monkeypatch.setattr(dbmod.Database, "get", lambda: DeadDb())
        await store._save_with_retry("sid", {}, "")  # 最终失败只记日志
        assert True

    async def test_await_prev_swallows_prev_error(self):
        from app.services.session_store import SessionStore

        async def bad():
            raise ValueError("prev failed")

        prev = asyncio.create_task(bad())
        done = asyncio.create_task(SessionStore._await_prev(prev))
        await asyncio.wait_for(done, 2)
        assert done.result() is None

    async def test_await_prev_reraises_when_self_cancelled(self):
        from app.services.session_store import SessionStore
        ev = asyncio.Event()
        prev = asyncio.create_task(ev.wait())
        t = asyncio.create_task(SessionStore._await_prev(prev))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        prev.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prev

    async def test_pop_save_chain(self):
        from app.services.session_store import SessionStore
        store = SessionStore()
        t = asyncio.create_task(asyncio.sleep(0))
        await t
        store._save_chains["s"] = t
        store._pop_save_chain("s", t)
        assert "s" not in store._save_chains
        other = asyncio.create_task(asyncio.sleep(0))
        store._save_chains["s"] = other
        store._pop_save_chain("s", t)  # 链尾不是自己 → 不动
        assert store._save_chains["s"] is other
        other.cancel()

    async def test_evict_overflow_and_delete_chain(self, env, monkeypatch):
        store, db = env
        monkeypatch.setattr(type(store), "_max_sessions_per_user",
                            staticmethod(lambda: 2))
        for i in range(3):
            s = await store.create_session(user_id="u")
            s.updated_at = 1000 + i  # 控制"最旧"
            await store.store_session(s)
        evicted = store._evict_overflow_locked()
        assert evicted == 0  # store_session 内已淘汰，现仅 2 个
        assert len(store._sessions) == 2
        await store.shutdown()
        assert len(await db.sessions_all()) == 2  # DB 同步删除最旧
        # limit=0 关闭淘汰
        monkeypatch.setattr(type(store), "_max_sessions_per_user", staticmethod(lambda: 0))
        async with store._lock:
            assert store._evict_overflow_locked() == 0

    def test_max_sessions_per_user_bad_config(self, monkeypatch):
        import app.core.config as cfg
        from app.services.session_store import SessionStore
        def bad(*a, **k):
            raise ValueError("bad config")
        monkeypatch.setattr(cfg, "get_config", bad)
        assert SessionStore._max_sessions_per_user() == 50

    async def test_delete_session_and_delete_all(self, env):
        store, db = env
        await store.create_session(user_id="a")
        await store.create_session(user_id="a")
        s3 = await store.create_session(user_id="b")
        assert await store.delete_session(s3.session_id) is True
        assert await store.delete_session(s3.session_id) is False
        n = await store.delete_all(user_id="a")
        assert n == 2
        await store.shutdown()
        assert await db.sessions_all() == []

    def test_delete_all_without_loop(self):
        from app.services.session_store import SessionStore
        store = SessionStore()  # 无 DB（未 init）→ RuntimeError 分支吞掉
        assert asyncio.run(store.delete_all()) == 0

    async def test_fork_session(self, env):
        store, _ = env
        src = await store.create_session(user_id="u1")
        src.model_messages = [{"role": "user", "content": f"m{i}"} for i in range(4)]
        src.summaries = [{"id": "s0", "text": "old"}]
        src.latest_visual_state = {"a": 1}
        await store.store_session(src)
        forked = await store.fork_session(src.session_id, "1", user_id="u2")
        assert forked.session_id != src.session_id
        assert [m["content"] for m in forked.model_messages] == ["m0", "m1"]
        assert forked.user_id == "u2"
        assert forked.summaries == [{"id": "s0", "text": "old"}]
        assert forked.latest_visual_state == {"a": 1}
        # 非法 message_id → 保留全部；越界 → 截到最后一条
        full = await store.fork_session(src.session_id, "abc")
        assert len(full.model_messages) == 4
        over = await store.fork_session(src.session_id, "99")
        assert len(over.model_messages) == 4
        under = await store.fork_session(src.session_id, "-9")
        assert under.model_messages == []
        assert await store.fork_session("ghost", "0") is None

    async def test_list_summaries_order_and_filter(self, env, monkeypatch):
        store, _ = env
        # store_session 会用 _now_ms() 覆盖 updated_at，打桩成递增时钟保证顺序稳定
        import app.services.session_store as ss_mod
        clock = {"n": 0}

        def _fake_now():
            clock["n"] += 1000
            return clock["n"]

        monkeypatch.setattr(ss_mod, "_now_ms", _fake_now)
        a = await store.create_session(user_id="u1")
        await store.store_session(a)
        b = await store.create_session(user_id="u1")
        await store.store_session(b)
        c = await store.create_session(user_id="u2")
        await store.store_session(c)
        ids = [s["id"] for s in await store.list_summaries("u1")]
        assert ids == [b.session_id, a.session_id]  # 新在前
        assert len(await store.list_summaries()) == 3

    async def test_undo_and_clear(self, env):
        store, _ = env
        s = await store.create_session()
        s.model_messages = [{"role": "user", "content": "q"},
                            {"role": "assistant", "content": "a"}]
        await store.store_session(s)
        assert await store.undo_last_message(s.session_id) is True
        assert await store.undo_last_message(s.session_id) is False  # 不足一对
        assert await store.undo_last_message("ghost") is False
        s.model_messages = [{"role": "user", "content": "x"}]
        await store.store_session(s)
        assert await store.clear_messages(s.session_id) is True
        cleared = await store.get_session(s.session_id)
        assert cleared.model_messages == [] and cleared.summaries == []
        assert await store.clear_messages("ghost") is False

    async def test_truncate_history_on_store(self, env):
        store, _ = env
        s = await store.create_session()
        # 序列化会对每项调 model_dump，必须是真实 pydantic 对象
        s.history_events = [_make_instruction("Template", "Speak") for _ in range(150)]
        s.history_instructions = [_make_instruction("Template", "Toast") for _ in range(300)]
        s.model_messages = [{"role": "user", "content": "m"}] * 120
        s.summaries = [{"id": i} for i in range(20)]
        await store.store_session(s)
        assert len(s.history_events) == 100
        assert len(s.history_instructions) == 200
        assert len(s.model_messages) == 100
        assert len(s.summaries) == 10

    def test_deserialize_defaults(self):
        from app.services.session_store import SessionStore
        s = SessionStore()._deserialize_session({"session_id": "s9"})
        assert s.request_id == "s9" and s.user_id == "" and s.model_messages == []

    async def test_delete_all_without_running_loop_skips_db(self, env, monkeypatch):
        """无事件循环可投递时（RuntimeError），只清内存、跳过 DB 删除。"""
        import asyncio as _asyncio

        store, _ = env
        s = await store.get_or_create("d1", "r", user_id="u1")
        assert s is not None

        def boom(*a, **k):
            raise RuntimeError("no loop")

        monkeypatch.setattr(_asyncio, "create_task", boom)
        n = await store.delete_all("u1")
        assert n == 1
        assert await store.get_session("d1") is None


# ============================================================================
# app/services/summarization_service.py
# ============================================================================

class _FakeChatClient:
    def __init__(self, enabled=True, reply="  摘要文本  ", fail=False):
        self.enabled = enabled
        self.reply = reply
        self.fail = fail
        self.calls = []

    async def chat(self, messages, timeout=None):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("llm down")
        return self.reply


def _session_with_turns(turns: int, content: str = "内容" * 10):
    from app.services.session_store import SessionState
    s = SessionState(session_id="sum-s", request_id="r")
    for i in range(turns):
        s.model_messages.append({"role": "user", "content": content})
        s.model_messages.append({"role": "assistant", "content": "好的"})
    return s


class TestSummarizationService:
    def _svc(self, monkeypatch, client=None):
        from app.services.summarization_service import SummarizationService
        monkeypatch.setattr(
            "app.services.summarization_service.build_per_user_chat_client",
            AsyncMock(return_value=None))
        return SummarizationService(chat_client=client or _FakeChatClient(enabled=False))

    def test_estimate_tokens_minimum(self):
        from app.services.summarization_service import SummarizationService
        assert SummarizationService().estimate_tokens([]) >= 1
        assert SummarizationService().estimate_tokens([{"content": "abcd" * 10}]) == 30

    def test_should_compress_soft_and_hard(self, monkeypatch):
        svc = self._svc(monkeypatch)
        soft = _session_with_turns(12)  # soft=12, hard=16
        assert svc.should_compress(soft) == (True, "soft")
        hard = _session_with_turns(16)
        assert svc.should_compress(hard) == (True, "hard")
        ok = _session_with_turns(2)
        assert svc.should_compress(ok) == (False, None)

    async def test_refresh_skips_when_count_unchanged(self, monkeypatch):
        svc = self._svc(monkeypatch)
        s = _session_with_turns(2)
        assert await svc.refresh_summaries(s) == []
        assert await svc.refresh_summaries(s) == []  # 消息数未变 → 跳过

    async def test_refresh_no_compress_needed_records_count(self, monkeypatch):
        svc = self._svc(monkeypatch)
        s = _session_with_turns(2)
        assert await svc.refresh_summaries(s) == []
        assert svc._last_message_count["sum-s"] == 4

    async def test_refresh_keep_window_covers_all_returns_unchanged(self, monkeypatch):
        svc = self._svc(monkeypatch)
        s = _session_with_turns(2)
        # 用超大 token 触发 hard，但消息数 < keep 窗口 → older 为空 → 原样返回
        s.model_messages = [{"role": "user", "content": "x" * 40000}]
        assert await svc.refresh_summaries(s) == []

    async def test_refresh_all_whitespace_blocks_returns_unchanged(self, monkeypatch):
        svc = self._svc(monkeypatch)
        s = _session_with_turns(0)
        s.model_messages = [{"role": "user", "content": "   "}] * 40  # hard via chars? 空白也有长度
        s.model_messages = [{"role": "user", "content": " " * 3000} for _ in range(40)]
        assert await svc.refresh_summaries(s) == []  # 全空白 → 无文本块 → 不生成

    async def test_refresh_compresses_trims_and_rolls(self, monkeypatch):
        client = _FakeChatClient(enabled=True, reply="压缩摘要")
        svc = self._svc(monkeypatch, client=client)
        s = _session_with_turns(20, content="历史内容" * 20)  # 40 条消息
        # 预置旧摘要 → 滚动承接
        s.summaries = [{"id": "summary-0", "text": "更早的摘要"}]
        summaries = await svc.refresh_summaries(s)
        assert len(summaries) == 2
        assert summaries[0]["text"] == "压缩摘要"
        assert summaries[0]["source_count"] == summaries[1]["source_count"]
        # 已摘要部分被裁掉：保留窗口 recent_turns(5)*2=10 条
        assert len(s.model_messages) == 10
        assert svc._last_message_count["sum-s"] == 10
        # LLM 输入里滚动包含旧摘要文本
        joined = json.dumps(client.calls, ensure_ascii=False)
        assert "更早的摘要" in joined

    async def test_refresh_trim_disabled_keeps_messages(self, monkeypatch):
        import app.services.summarization_service as ss
        client = _FakeChatClient(enabled=True, reply="摘要")
        svc = self._svc(monkeypatch, client=client)
        real_get_config = ss.get_config

        def fake_get_config(key, default=None):
            if key == "rag.summary_trim_enabled":
                return False
            return real_get_config(key, default)

        monkeypatch.setattr(ss, "get_config", fake_get_config)
        s = _session_with_turns(20)
        await svc.refresh_summaries(s)
        assert len(s.model_messages) == 40  # 不裁剪
        assert svc._last_message_count["sum-s"] == 40

    async def test_per_user_client_preferred(self, monkeypatch):
        import app.services.summarization_service as ss
        per_user = _FakeChatClient(enabled=True, reply="用户级摘要")
        monkeypatch.setattr(ss, "build_per_user_chat_client",
                            AsyncMock(return_value=per_user))
        from app.services.summarization_service import SummarizationService
        svc = SummarizationService(chat_client=_FakeChatClient(enabled=True, reply="全局"))
        s = _session_with_turns(20)
        summaries = await svc.refresh_summaries(s, user_id="u1")
        assert summaries[0]["text"] == "用户级摘要"

    async def test_summarize_chunk_fallbacks(self, monkeypatch):
        import app.services.summarization_service as ss
        svc = self._svc(monkeypatch)
        # 无客户端 → 截断（多元素块才有 "历史摘要(N条)" 前缀）
        out = await svc._summarize_chunk(["第一段内容", "第二段内容"], None)
        assert "历史摘要" in out and "第一段内容" in out and "第二段内容" in out
        # 单元素块 → 前 240 字
        out_solo = await svc._summarize_chunk(["一" * 300], None)
        assert out_solo == "一" * 240
        # 单元素块 → 前 240 字
        out1 = svc._truncate_summary(["x" * 300])
        assert len(out1) == 240
        # LLM 关闭 → 截断
        real_get_config = ss.get_config

        def off(key, default=None):
            if key == "llm.summary_enabled":
                return False
            return real_get_config(key, default)

        monkeypatch.setattr(ss, "get_config", off)
        c = _FakeChatClient(enabled=True)
        assert await svc._summarize_chunk(["a", "b"], c) == svc._truncate_summary(["a", "b"])
        assert c.calls == []
        # LLM 抛异常 → 截断兜底
        bad = _FakeChatClient(enabled=True, fail=True)
        monkeypatch.setattr(ss, "get_config", real_get_config)
        assert "历史摘要" in await svc._summarize_chunk(["a", "b"], bad)
        # 空回复 → 截断
        empty = _FakeChatClient(enabled=True, reply="   ")
        assert "历史摘要" in await svc._summarize_chunk(["a", "b"], empty)


# ============================================================================
# app/services/prompt_service.py
# ============================================================================

class TestBuildSystemPrompt:
    async def test_full_prompt_with_controls_focus_summaries(self, monkeypatch):
        from app.services import prompt_service as ps
        from app.services import weather_service

        async def fake_weather():
            return {"t": 20}

        monkeypatch.setattr(weather_service, "get_weather", fake_weather)
        monkeypatch.setattr(weather_service, "format_weather_detail", lambda d: "晴 20°C")
        prompt = await ps.build_system_prompt(
            visual_summary={"action": "tracking", "feedback": "正常"},
            device_controls="- light.li 开关",
            vision_focuses=[{"text": "看门口", "enabled": True},
                            {"text": "已停用", "enabled": False}],
            summaries=[{"text": "昨天聊过灯"}, {"text": ""}],
        )
        assert "晴 20°C" in prompt
        assert "设备可控项" in prompt and "- light.li 开关" in prompt
        assert "动作=tracking" in prompt and "反馈=正常" in prompt
        assert "看门口" in prompt and "已停用" not in prompt
        assert "昨天聊过灯" in prompt and "历史对话摘要" in prompt
        assert "当前时间" in prompt  # 时间注入防编造

    async def test_catalog_branch_when_no_controls(self, monkeypatch):
        from app.services import prompt_service as ps
        prompt = await ps.build_system_prompt(device_catalog="# 设备A\n- light.a")
        assert "当前 Home Assistant 可用设备" in prompt
        assert "# 设备A" in prompt

    async def test_weather_failure_is_silent(self, monkeypatch):
        from app.services import prompt_service as ps
        from app.services import weather_service

        async def boom():
            raise RuntimeError("weather down")

        monkeypatch.setattr(weather_service, "get_weather", boom)
        prompt = await ps.build_system_prompt()
        assert "当前时间" in prompt  # 其余部分照常
        assert "°C" not in prompt

    async def test_persona_and_guidelines_overrides(self, monkeypatch):
        import app.core.config as cfg
        from app.services import prompt_service as ps
        monkeypatch.setattr(cfg, "CONFIG", {
            "chat_assistant": {"persona": "自定义人格", "guidelines": "自定义守则"}})
        prompt = await ps.build_system_prompt()
        assert prompt.startswith("自定义人格")
        assert "自定义守则" in prompt


# ============================================================================
# app/services/weekly_report_service.py
# ============================================================================

class TestWeeklyReportService:
    @pytest.fixture
    async def wr(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "wr.db")
        db = await Database.init()
        from app.services import weekly_report_service as wrs
        from app.services.alert_service import alert_service
        svc = wrs.WeeklyReportService()
        broadcast = AsyncMock()
        monkeypatch.setattr(alert_service, "broadcast_report", broadcast)
        yield svc, db, broadcast, wrs
        await svc.stop()
        await Database.close()

    async def _seed_events(self, db):
        await db.family_event_add("device_op", "light.li", "AI将「客厅灯」执行 打开", actor="AI")
        await db.family_event_add("device_op", "light.li", "手动关灯", actor="手动")
        await db.family_event_add("automation", "r1", "触发")
        await db.family_event_add("task_success", "c1", "ok")
        await db.family_event_add("task_failed", "c1", "bad")
        await db.family_event_add("alert", "cam", "掉线")
        await db.family_event_add("alert_resolved", "cam", "恢复")
        await db.family_event_add("device_state", "sensor.t", "25")

    async def test_generate_no_events(self, wr):
        svc, db, broadcast, wrs = wr
        report = await svc.generate()
        assert report == {"generated": False, "reason": "no_events"}
        broadcast.assert_not_awaited()

    async def test_generate_llm_disabled_falls_back_to_stats(self, wr):
        svc, db, broadcast, wrs = wr
        await self._seed_events(db)
        report = await svc.generate()  # llm=None → 统计文本
        assert report["generated"] is True
        assert "本周自动化触发 1 次" in report["text"]
        assert "定时任务执行 2 次（失败 1 次）" in report["text"]
        assert "告警 1 次，已恢复 1 次" in report["text"]
        assert "2 台设备有动态（AI 操作 1 次、手动操作 1 次）" in report["text"]
        broadcast.assert_awaited_once()
        # 落 kv + weekly_report 事件
        assert float(await db.kv_get("weekly_report:last")) > 0
        events = await db.family_events_since(0, kinds=["weekly_report"])
        assert events[-1]["message"] == report["text"]

    async def test_generate_llm_success_and_set_client(self, wr):
        svc, db, broadcast, wrs = wr
        await self._seed_events(db)
        svc.set_llm_client(_FakeChatClient(enabled=True, reply="  亲爱的家庭周报  "))
        report = await svc.generate()
        assert report["text"] == "亲爱的家庭周报"

    async def test_generate_llm_failure_falls_back(self, wr):
        svc, db, broadcast, wrs = wr
        await self._seed_events(db)
        svc.set_llm_client(_FakeChatClient(enabled=True, fail=True))
        report = await svc.generate()
        assert "本周自动化触发" in report["text"]

    async def test_latest_report(self, wr):
        svc, db, broadcast, wrs = wr
        assert await svc.latest_report() is None
        await db.family_event_add("weekly_report", "report", "第一期周报")
        got = await svc.latest_report()
        assert got["text"] == "第一期周报" and got["generated_at"] > 0

    async def test_already_generated(self, wr):
        svc, db, broadcast, wrs = wr
        target = datetime(2026, 8, 30, 20, 0)
        assert await svc._already_generated(target) is False  # kv 缺失
        await db.kv_set("weekly_report:last", str(target.timestamp()))
        assert await svc._already_generated(target) is True
        await db.kv_set("weekly_report:last", "not-a-float")
        assert await svc._already_generated(target) is False  # 坏值 → False

    def test_this_weeks_target(self, monkeypatch):
        import app.core.config as cfg
        from app.services.weekly_report_service import WeeklyReportService
        monkeypatch.setattr(cfg, "CONFIG", {"weekly_report": {"hour": 9}})
        wed = datetime(2026, 9, 2, 12, 0)  # 周三
        t = WeeklyReportService._this_weeks_target(wed)
        assert (t.weekday(), t.hour, t.minute) == (6, 9, 0)
        assert t == datetime(2026, 9, 6, 9, 0)
        sun_after = datetime(2026, 9, 6, 21, 0)
        assert WeeklyReportService._this_weeks_target(sun_after) == datetime(2026, 9, 6, 9, 0)
        # 缺省 hour=20
        monkeypatch.setattr(cfg, "CONFIG", {})
        assert WeeklyReportService._this_weeks_target(wed).hour == 20

    async def test_start_stop_lifecycle(self, wr, monkeypatch):
        svc, db, broadcast, wrs = wr
        # 默认开启（与 CHANGELOG 一致）→ 起循环；stop 取消
        await svc.start()
        assert svc._loop_task is not None and not svc._loop_task.done()
        await svc.stop()
        assert svc._loop_task is None
        await svc.stop()  # 幂等
        # 显式关闭 → 不起循环
        real_get_config = wrs.get_config

        def on(key, default=None):
            if key == "weekly_report.enabled":
                return False
            return real_get_config(key, default)

        monkeypatch.setattr(wrs, "get_config", on)
        await svc.start()
        assert svc._loop_task is None

    async def test_is_enabled_exception_returns_true(self, wr, monkeypatch):
        svc, db, broadcast, wrs = wr

        def boom(*a, **k):
            raise RuntimeError("cfg broken")

        monkeypatch.setattr(wrs, "get_config", boom)
        assert svc._is_enabled() is True

    async def test_daily_check_generates_and_ignores_error_then_cancel(self, wr, monkeypatch):
        svc, db, broadcast, wrs = wr
        past = datetime.now() - timedelta(minutes=1)
        monkeypatch.setattr(svc, "_this_weeks_target", lambda now: past)
        monkeypatch.setattr(svc, "_already_generated", AsyncMock(return_value=False))
        gen = AsyncMock(side_effect=[RuntimeError("boom"), asyncio.CancelledError()])
        monkeypatch.setattr(svc, "generate", gen)
        # 6 小时的循环 sleep 压缩为 5ms，让两轮检查立刻发生
        real_sleep = asyncio.sleep

        async def fast_sleep(delay, *a, **k):
            if isinstance(delay, (int, float)) and delay >= 5:
                await real_sleep(0.005, *a, **k)
            else:
                await real_sleep(delay, *a, **k)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        task = asyncio.create_task(svc._daily_check())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert gen.await_count == 2  # 第一次异常被吞，第二次 CancelledError 传播

    async def test_count_chat_turns_counts_recent_users(self, wr, monkeypatch):
        svc, db, broadcast, wrs = wr
        await db.sessions_upsert("s1", {"session_id": "s1", "updated_at": int(
            (__import__("time").time()) * 1000),
            "model_messages": [{"role": "user", "content": "a"},
                               {"role": "assistant", "content": "b"},
                               {"role": "user", "content": "c"}]})
        # 陈旧会话不计
        await db.sessions_upsert("s2", {"session_id": "s2", "updated_at": 0,
                                        "model_messages": [{"role": "user", "content": "old"}]})
        assert await svc._count_chat_turns() == 2


# ============================================================================
# app/ops/diag.py
# ============================================================================

class TestDiag:
    @pytest.fixture(autouse=True)
    def _audit_to_tmp(self, tmp_path, monkeypatch):
        from app.ops import audit
        monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path / "audit")
        monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit" / "ops_audit.jsonl")
        yield

    def test_collect_system_info(self, monkeypatch):
        from app.ops import diag
        info = diag.collect_system_info()
        assert info["cpu_count"] >= 1 and "app_version" in info
        assert info["mem_total_mb"] is None or info["mem_total_mb"] > 0
        # 磁盘探测失败 → 键缺失但不抛
        def boom(p):
            raise OSError("no disk")
        monkeypatch.setattr(diag.shutil, "disk_usage", boom)
        assert "disk_free_gb" not in diag.collect_system_info()

    async def test_collect_docker_status_none_and_error(self, tmp_path, monkeypatch):
        from app.ops import diag
        assert await diag.collect_docker_status() is None  # 无 docker.sock
        fake_sock = tmp_path / "docker.sock"
        fake_sock.write_bytes(b"")
        monkeypatch.setattr(diag, "DOCKER_SOCK", fake_sock)
        result = await asyncio.wait_for(diag.collect_docker_status(), 10)
        assert isinstance(result, dict) and "error" in result

    def test_collect_log_files_budget_and_tail(self, tmp_path, monkeypatch):
        from app.ops import diag
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setattr(diag, "LOGS_DIR", logs)
        assert diag.collect_log_files() == []  # 无匹配后缀文件 → []
        old = logs / "old.log"
        old.write_bytes(b"a" * 30)
        new = logs / "new.log"
        new.write_bytes(b"b" * 20)
        os.utime(old, (1000000000, 1000000000))
        # 预算 10 → 取最新文件尾部 10 字节
        collected = diag.collect_log_files(budget=10)
        assert len(collected) == 1
        name, data = collected[0]
        assert name == "new.log" and data == b"b" * 10  # 尾部
        # 预算 0 → 立即 break
        assert diag.collect_log_files(budget=0) == []
        # 读取失败（OSError）→ 跳过该文件
        def broken_open(self, *a, **k):
            raise OSError("denied")

        monkeypatch.setattr(Path, "open", broken_open)
        assert diag.collect_log_files(budget=1000) == []

    def test_collect_log_files_skips_subdirs_and_suffixes(self, tmp_path, monkeypatch):
        from app.ops import diag
        logs = tmp_path / "logs"
        (logs / "audit").mkdir(parents=True)
        (logs / "note.md").write_text("x")
        good = logs / "app.log"
        good.write_text("hello log")
        monkeypatch.setattr(diag, "LOGS_DIR", logs)
        collected = diag.collect_log_files()
        assert [n for n, _ in collected] == ["app.log"]

    async def test_build_diagnostic_package(self, tmp_path, monkeypatch):
        from app.ops import diag
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
        (tmp_path / "config.json").write_text(
            json.dumps({"llm": {"api_key": "sk-1234567890abcd"},
                        "home": {"home_name": "我的家"}}), encoding="utf-8")
        monkeypatch.setattr(diag, "collect_docker_status",
                            AsyncMock(return_value=[{"name": "aether", "state": "running",
                                                     "image": "aether:1.0"}]))
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "app.log").write_text("log line")
        monkeypatch.setattr(diag, "LOGS_DIR", logs)
        blob, filename = await diag.build_diagnostic_package("tester")
        assert filename.startswith("aether-diag-") and filename.endswith(".zip")
        import zipfile
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = zf.namelist()
            assert "manifest.json" in names and "system/docker.json" in names
            assert "config/config_sanitized.json" in names and "README.txt" in names
            assert "logs/app.log" in names
            sanitized = json.loads(zf.read("config/config_sanitized.json"))
            assert sanitized["llm"]["api_key"] == "sk-1****abcd"
            assert sanitized["home"]["home_name"] == "[已脱敏]"
            assert b"tester" in zf.read("manifest.json")
        from app.ops import audit
        tail = audit.tail()
        assert tail[-1]["action"] == "diag_export" and tail[-1]["operator"] == "tester"

    def test_full_config_failure_paths(self, tmp_path, monkeypatch):
        import app.core.config as cfg
        from app.ops import diag
        monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "missing.json")
        assert diag._full_config() == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid", encoding="utf-8")
        monkeypatch.setattr(cfg, "CONFIG_PATH", bad)
        assert diag._full_config() == {}


# ============================================================================
# app/ops/audit.py
# ============================================================================

class TestAudit:
    def test_record_success_and_tail(self, tmp_path, monkeypatch):
        from app.ops import audit
        monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path / "audit")
        monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit" / "ops_audit.jsonl")
        entry = audit.record("op", "act", {"k": 1})
        assert entry["operator"] == "op" and entry["detail"] == {"k": 1}
        audit.record("op2", "act2")
        tail = audit.tail()
        assert len(tail) == 2 and tail[0]["operator"] == "op"
        assert audit.tail(limit=1)[0]["operator"] == "op2"

    def test_record_oserror_swallowed(self, tmp_path, monkeypatch):
        from app.ops import audit
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file")
        monkeypatch.setattr(audit, "AUDIT_DIR", blocker / "audit")  # 父级是文件 → mkdir 失败
        monkeypatch.setattr(audit, "AUDIT_FILE", blocker / "audit" / "x.jsonl")
        entry = audit.record("op", "act")  # 不抛
        assert entry["action"] == "act"

    def test_clear_semantics(self, tmp_path, monkeypatch):
        from app.ops import audit
        monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
        f = tmp_path / "ops_audit.jsonl"
        monkeypatch.setattr(audit, "AUDIT_FILE", f)
        assert audit.clear() == 0  # 文件不存在
        audit.record("a", "x")
        audit.record("b", "y")
        assert audit.clear() == 2
        assert f.read_text(encoding="utf-8") == ""
        # 文件是目录 → OSError → 0
        f.unlink()
        f.mkdir()
        assert audit.clear() == 0

    def test_tail_bad_lines_and_oserror(self, tmp_path, monkeypatch):
        from app.ops import audit
        monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
        f = tmp_path / "ops_audit.jsonl"
        monkeypatch.setattr(audit, "AUDIT_FILE", f)
        f.write_text('{"ok": 1}\nnot-json\n{"ok": 2}\n', encoding="utf-8")
        assert [e["ok"] for e in audit.tail()] == [1, 2]
        f.unlink()
        f.mkdir()  # 目录 → read_text OSError → []
        assert audit.tail() == []


# ============================================================================
# app/ops/auto_update.py
# ============================================================================

def _write_pack(directory: Path, version: str, mtime: float | None = None) -> Path:
    p = directory / f"aether-update-{version}.tar.gz"
    manifest = json.dumps({"version": version}).encode("utf-8")
    with tarfile.open(p, "w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class TestAutoUpdate:
    @pytest.fixture(autouse=True)
    def _packdir(self, tmp_path, monkeypatch):
        from app.ops import pack_export
        self.pack_dir = tmp_path / "backups"
        monkeypatch.setattr(pack_export, "PACK_DIR", self.pack_dir)
        self.pe = pack_export
        yield

    def test_find_candidate_none_when_dir_missing(self, monkeypatch):
        from app.ops import auto_update
        monkeypatch.setattr(self.pe, "PACK_DIR", self.pack_dir / "nope")
        assert auto_update.find_candidate() is None

    def test_find_candidate_filters(self, tmp_path, monkeypatch):
        from app.ops import auto_update
        self.pack_dir.mkdir()
        monkeypatch.setattr("app.ops.auto_update.get_version", lambda: "1.0.0")
        old_ts = __import__("time").time() - 3600
        # 未落稳（mtime 太新）→ 跳过
        _write_pack(self.pack_dir, "9.9.9")  # mtime=now
        # 坏包（无 manifest）→ 跳过
        broken = self.pack_dir / "aether-update-8.8.8.tar.gz"
        broken.write_bytes(b"junk")
        os.utime(broken, (old_ts, old_ts))
        # 旧版本 → 跳过
        _write_pack(self.pack_dir, "0.9.0", mtime=old_ts)
        # 新版本 → 候选
        good = _write_pack(self.pack_dir, "2.0.0", mtime=old_ts)
        # 名字不匹配 → 忽略
        (self.pack_dir / "random.tar.gz").write_bytes(b"x")
        assert auto_update.find_candidate() == (good, "2.0.0")
        # 多候选取最高版本
        _write_pack(self.pack_dir, "3.0.0", mtime=old_ts)
        path, ver = auto_update.find_candidate()
        assert ver == "3.0.0"

    async def test_watcher_applies_candidate(self, monkeypatch):
        from app.ops import auto_update
        real_sleep = asyncio.sleep
        applied = {}

        async def fake_apply(name, operator):
            applied["args"] = (name, operator)
            return {"from_version": "1.0.0", "to_version": "9.9.9"}

        monkeypatch.setattr(auto_update, "auto_upgrade_enabled", lambda: True)
        pack = SimpleNamespace(name="aether-update-9.9.9.tar.gz")
        monkeypatch.setattr(auto_update, "find_candidate", lambda: (pack, "9.9.9"))
        monkeypatch.setattr(self.pe, "apply_local_pack", fake_apply)

        async def fast_sleep(_):
            raise asyncio.CancelledError  # 一轮后结束循环

        monkeypatch.setattr(auto_update.asyncio, "sleep", fast_sleep)
        task = asyncio.create_task(auto_update.watcher_loop())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert applied["args"] == ("aether-update-9.9.9.tar.gz", "auto")

    async def test_watcher_quarantines_failed_pack(self, tmp_path, monkeypatch):
        from app.ops import auto_update
        real_sleep = asyncio.sleep
        self.pack_dir.mkdir()
        pack = tmp_path / "aether-update-9.9.9.tar.gz"
        pack.write_bytes(b"pack")
        monkeypatch.setattr(auto_update, "auto_upgrade_enabled", lambda: True)
        monkeypatch.setattr(auto_update, "find_candidate", lambda: (pack, "9.9.9"))

        async def failing_apply(name, operator):
            raise RuntimeError("docker load failed")

        monkeypatch.setattr(self.pe, "apply_local_pack", failing_apply)

        async def fast_sleep(_):
            raise asyncio.CancelledError

        monkeypatch.setattr(auto_update.asyncio, "sleep", fast_sleep)
        task = asyncio.create_task(auto_update.watcher_loop())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not pack.exists()
        assert (tmp_path / "aether-update-9.9.9.tar.gz.failed").exists()

    async def test_watcher_disabled_is_noop(self, monkeypatch):
        from app.ops import auto_update
        monkeypatch.setattr(auto_update, "auto_upgrade_enabled", lambda: False)
        monkeypatch.setattr(auto_update, "find_candidate",
                            Mock(side_effect=AssertionError("不该被调")))

        async def fast_sleep(_):
            raise asyncio.CancelledError

        monkeypatch.setattr(auto_update.asyncio, "sleep", fast_sleep)
        task = asyncio.create_task(auto_update.watcher_loop())
        with pytest.raises(asyncio.CancelledError):
            await task

    def test_auto_upgrade_enabled_config(self, monkeypatch):
        import app.core.config as cfg
        from app.ops import auto_update
        monkeypatch.setattr(cfg, "CONFIG", {"update": {"auto_upgrade": False}})
        assert auto_update.auto_upgrade_enabled() is False
        monkeypatch.setattr(cfg, "CONFIG", {})
        assert auto_update.auto_upgrade_enabled() is True


# ============================================================================
# app/ops/backup.py
# ============================================================================

@pytest.fixture
def bak_env(tmp_path, monkeypatch):
    """backup 模块路径全指向 tmp，audit 重定向。"""
    from app.ops import audit
    from app.ops import backup as bk
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(bk, "DATA_DIR", data_dir)
    monkeypatch.setattr(bk, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(bk, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit" / "ops_audit.jsonl")
    (tmp_path / "config.json").write_text('{"ha": {"url": "http://x"}}', encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=sk-test\n", encoding="utf-8")
    return tmp_path


def _seed_db(data_dir: Path):
    conn = sqlite3.connect(data_dir / "aether.db")
    conn.execute("CREATE TABLE t(v TEXT)")
    conn.execute("INSERT INTO t VALUES('kept')")
    conn.commit()
    conn.close()
    (data_dir / "jwt_secret").write_text("s3cret", encoding="utf-8")


class TestBackup:
    def test_dir_size_branches(self, tmp_path):
        from app.ops import backup as bk
        f = tmp_path / "f.txt"
        f.write_bytes(b"12345")
        d = tmp_path / "d"
        d.mkdir()
        (d / "a").write_bytes(b"12")
        (d / "sub").mkdir()
        (d / "sub" / "b").write_bytes(b"123")
        assert bk._dir_size(f) == 5
        assert bk._dir_size(d) == 5
        assert bk._dir_size(tmp_path / "missing") == 0

    def test_disk_guard_raises_when_insufficient(self, bak_env, monkeypatch):
        from app.ops import backup as bk
        monkeypatch.setattr(bk.shutil, "disk_usage",
                            lambda p: SimpleNamespace(free=10, total=100))
        with pytest.raises(RuntimeError, match="磁盘剩余"):
            bk._disk_guard(10 ** 9)

    def test_sqlite_snapshot_missing_db(self, bak_env):
        from app.ops import backup as bk
        assert bk._sqlite_snapshot(bak_env / "snap.db") is False

    def test_sqlite_snapshot_copies_content(self, bak_env):
        from app.ops import backup as bk
        _seed_db(bk.DATA_DIR)
        dest = bak_env / "snap.db"
        assert bk._sqlite_snapshot(dest) is True
        conn = sqlite3.connect(dest)
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "kept"
        conn.close()

    def test_delete_backup_missing_returns_false(self, bak_env):
        from app.ops import backup as bk
        assert bk.delete_backup("aether-backup-20260101-000000.tar.gz") is False

    def test_restore_backup_applies_files_and_schedules_restart(self, bak_env):
        from app.ops import backup as bk
        _seed_db(bk.DATA_DIR)
        created = bk.create_backup("tester")
        # 现场被改坏，恢复后应回到备份内容
        (bak_env / "config.json").write_text('{"broken": true}', encoding="utf-8")
        (bak_env / ".env").write_text("KEY=changed\n", encoding="utf-8")
        (bk.DATA_DIR / "jwt_secret").write_text("rotated", encoding="utf-8")

        fired = {}

        class FakeTimer:
            def __init__(self, interval, fn):
                fired["interval"] = interval
                fired["fn"] = fn

            daemon = True

            def start(self):
                fired["started"] = True

        import threading
        orig_timer = threading.Timer
        threading.Timer = FakeTimer
        try:
            info = bk.restore_backup(created["name"], "operator")
        finally:
            threading.Timer = orig_timer
        assert info["restored"] is True and info["restarting"] is True
        assert info["has_config"] and info["has_env"] and info["has_data"]
        assert json.loads((bak_env / "config.json").read_text(encoding="utf-8")) == {
            "ha": {"url": "http://x"}}
        assert "KEY=sk-test" in (bak_env / ".env").read_text(encoding="utf-8")
        assert (bk.DATA_DIR / "jwt_secret").read_text(encoding="utf-8") == "s3cret"
        # 旧数据目录被挪到回滚位，未直接删除
        trash = list(bk.BACKUP_DIR.glob(".restore-old-*"))
        assert trash, "旧 data/ 应挪入回滚目录"
        assert fired["interval"] == 2.0 and fired["started"] is True
        assert fired["fn"].__name__ == "_exit_soon"


# ============================================================================
# app/main.py — 辅助函数
# ============================================================================

class _FakeWebsocket:
    def __init__(self, cookies=None, headers=None):
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.close_code = None

    async def close(self, code=None):
        self.close_code = code


class TestMainHelpers:
    async def test_ws_verify_token_paths(self, monkeypatch):
        import app.main as main
        from app.core import auth as auth_mod

        monkeypatch.setattr(main, "APP_TOKEN", "")
        token = auth_mod.create_access_token(user_id="u1", username="alice")
        # Bearer access token → user_id
        ws = _FakeWebsocket(headers={"Authorization": f"Bearer {token}"})
        assert await main._ws_verify_token(ws) == "u1" and ws.close_code is None
        # cookie 亦可
        ws = _FakeWebsocket(cookies={auth_mod.ACCESS_COOKIE: token})
        assert await main._ws_verify_token(ws) == "u1"
        # refresh token → 拒绝
        refresh = auth_mod.create_refresh_token("u1")
        ws = _FakeWebsocket(headers={"Authorization": f"Bearer {refresh}"})
        assert await main._ws_verify_token(ws) is None and ws.close_code == 1008
        # 无任何凭证 → 拒绝
        ws = _FakeWebsocket()
        assert await main._ws_verify_token(ws) is None and ws.close_code == 1008
        # 非法 JWT + APP_TOKEN 回退通道
        monkeypatch.setattr(main, "APP_TOKEN", "shared-secret")
        ws = _FakeWebsocket(headers={"Authorization": "Bearer garbage",
                                     "X-API-Token": "shared-secret"})
        assert await main._ws_verify_token(ws) == ""
        ws = _FakeWebsocket(headers={"X-API-Token": "wrong"})
        assert await main._ws_verify_token(ws) is None and ws.close_code == 1008

    async def test_ws_heartbeat_sends_ping_and_stops_on_error(self):
        import app.main as main

        class _WS:
            def __init__(self, fail_after=2):
                self.pings = []
                self.fail_after = fail_after

            async def send_json(self, payload):
                self.pings.append(payload)
                if len(self.pings) >= self.fail_after:
                    raise RuntimeError("closed")

        ws = _WS()
        task = asyncio.create_task(main._ws_heartbeat(ws, interval=0.01))
        await asyncio.wait_for(asyncio.shield(task), 2)
        assert len(ws.pings) >= 2  # 至少两次 ping 后因异常退出循环
        assert task.done() and task.exception() is None

    async def test_rebuild_agent_updates_global_and_dispatcher(self, monkeypatch):
        import app.main as main
        import app.mcp.langchain_tools as lt
        import app.agents.langgraph_agent as la

        monkeypatch.setattr(lt, "convert_all_tools", lambda m, full_name=False: ["toolA"])
        monkeypatch.setattr(la, "build_chat_agent",
                            lambda tools: ("AGENT", ("c1", "c2")))
        set_agent = AsyncMock()
        monkeypatch.setattr(main, "dispatcher", SimpleNamespace(set_agent=set_agent))
        monkeypatch.setattr(main, "langgraph_agent", None)
        await main._rebuild_agent()
        assert main.langgraph_agent == "AGENT"
        set_agent.assert_awaited_once_with("AGENT", tools=["toolA"], clients=("c1", "c2"))

    async def test_sync_ha_runtime_refs_rewires_everything(self, monkeypatch):
        import app.main as main
        new_client, new_service = object(), SimpleNamespace(get_all_devices=lambda: [])
        rule_svc = SimpleNamespace(set_ha_devices_provider=Mock())
        monkeypatch.setattr(main, "rule_service", rule_svc)
        monkeypatch.setattr(main, "dispatcher",
                            SimpleNamespace(set_ha_service=Mock()))
        il = SimpleNamespace(update_ha_refs=Mock())
        monkeypatch.setattr(main._container, "integration_layer", il)
        cm = SimpleNamespace(set_ha_service=Mock())
        monkeypatch.setattr(main, "_services", {"camera_manager": cm})
        td = SimpleNamespace(ha_service=None)
        monkeypatch.setattr(main._container, "tool_deps", td, raising=False)
        ss = SimpleNamespace(set_ha=Mock())
        monkeypatch.setattr(main._container, "scene_service", ss)
        monkeypatch.setattr(main, "ha_client", "old")
        monkeypatch.setattr(main, "ha_service", "old-svc")
        main.sync_ha_runtime_refs(new_client, new_service)
        assert main.ha_client is new_client and main.ha_service is new_service
        rule_svc.set_ha_devices_provider.assert_called_once_with(new_service.get_all_devices)
        main.dispatcher.set_ha_service.assert_called_once_with(new_service)
        il.update_ha_refs.assert_called_once_with(new_client, new_service)
        cm.set_ha_service.assert_called_once_with(new_service)
        assert td.ha_service is new_service
        ss.set_ha.assert_called_once_with(new_client, new_service)

    async def test_refresh_ha_catalog_updates_caches(self, monkeypatch):
        import app.main as main
        from app.services import device_registry as dr
        monkeypatch.setattr(dr, "build_device_snapshot",
                            AsyncMock(return_value={"devices": []}))
        monkeypatch.setattr(dr, "render_catalog_text", lambda s: "CAT-TEXT")
        monkeypatch.setattr(dr, "render_controls_text", lambda s: "CTRL-TEXT")
        monkeypatch.setattr(main, "_ha_catalog_cache_ref", ["old"])
        monkeypatch.setattr(main, "_ha_controls_cache_ref", ["old"])
        await main._refresh_ha_catalog()
        assert main._ha_catalog_cache_ref[0] == "CAT-TEXT"
        assert main._ha_controls_cache_ref[0] == "CTRL-TEXT"
        # 业务异常被吞、缓存保持旧值
        monkeypatch.setattr(dr, "build_device_snapshot",
                            AsyncMock(side_effect=RuntimeError("ha down")))
        await main._refresh_ha_catalog()
        assert main._ha_catalog_cache_ref[0] == "CAT-TEXT"

    async def test_ha_catalog_refresh_loop_breaks_on_cancel(self, monkeypatch):
        import app.main as main
        from app.services import device_registry as dr
        monkeypatch.setattr(dr, "build_device_snapshot",
                            AsyncMock(side_effect=asyncio.CancelledError()))

        real_sleep = asyncio.sleep

        async def fast_sleep(delay, *a, **k):
            if isinstance(delay, (int, float)) and delay >= 5:
                await real_sleep(0)
            else:
                await real_sleep(delay)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        task = asyncio.create_task(main._ha_catalog_refresh_loop())
        await asyncio.wait_for(task, 5)  # except CancelledError: break → 正常返回
        assert task.result() is None

    async def test_build_dispatch_fn(self):
        import app.main as main

        class _Header:
            def __init__(self, ns, name):
                self.namespace = ns
                self.name = name

        class _Inst:
            def __init__(self, ns=None, name=None, payload=None):
                self.header = _Header(ns, name) if ns else None
                self.payload = payload

        dispatch = AsyncMock(return_value=[
            _Inst("Template", "ToastStream", {"stream": "流式回复"})])
        fn = main._build_dispatch_fn(SimpleNamespace(dispatch=dispatch))
        assert await fn("你好", "sess", "u") == "流式回复"
        # 非流式指令 → 空串
        dispatch.return_value = [_Inst("Template", "Speak", {})]
        assert await fn("你好", "sess", "u") == ""
        # 无指令 → 空串
        dispatch.return_value = []
        assert await fn("你好", "sess", "u") == ""
        # dispatch 异常 → 兜底文案
        dispatch.side_effect = RuntimeError("down")
        assert await fn("你好", "sess", "u") == "抱歉，处理消息时出错了。"

    async def test_start_host_integrations_scans_real_dir(self, monkeypatch):
        import importlib.util
        import app.main as main

        # 扫描真实 integrations/ 目录，但打桩 loader 让每个 main.py 加载后
        # start 恒返回 None——避免真实启动飞书（本环境可能配置了凭证）
        real_spec = importlib.util.spec_from_file_location

        def fake_spec(name, path):
            spec = real_spec(name, path)
            if spec is not None and spec.loader is not None:
                spec.loader.exec_module = lambda mod: setattr(mod, "start", None)
            return spec

        monkeypatch.setattr(importlib.util, "spec_from_file_location", fake_spec)
        started = main._start_host_integrations(
            SimpleNamespace(dispatcher=None, integration_layer=None),
            asyncio.get_running_loop())
        # 真实 integrations/ 里只有 feishu 有 main.py，
        # xiaoai/test-camera/qwen-adapter 只有 plugin.py；start 全部 None → 空
        assert started == []

    async def test_start_host_integrations_load_failure_is_isolated(self, monkeypatch):
        import importlib.util
        import app.main as main

        def broken(*a, **k):
            raise ValueError("bad module")

        monkeypatch.setattr(importlib.util, "spec_from_file_location", broken)
        started = main._start_host_integrations(
            SimpleNamespace(dispatcher=None, integration_layer=None),
            asyncio.get_running_loop())
        assert started == []  # 加载失败只记日志

    def test_load_host_integration_meta(self, tmp_path):
        import app.main as main
        # 无 meta.py → 默认
        meta = main._load_host_integration_meta("x", str(tmp_path))
        assert meta["name"] == "x" and meta["alive"] is True
        # 有 meta.py → 读取声明
        d = tmp_path / "x"
        d.mkdir()
        (d / "meta.py").write_text(
            "NAME='飞书'\nVERSION='1.0'\nDESCRIPTION='d'\n"
            "CAPABILITIES=['ws']\nCONFIG_SCHEMA={'a': 1}\n", encoding="utf-8")
        meta = main._load_host_integration_meta("x", str(tmp_path))
        assert meta == {"name": "飞书", "version": "1.0", "description": "d",
                        "capabilities": ["ws"], "config_schema": {"a": 1}, "alive": True}
        # 坏 meta.py → 回退默认
        (d / "meta.py").write_text("raise RuntimeError(1)\n", encoding="utf-8")
        assert main._load_host_integration_meta("x", str(tmp_path))["name"] == "x"

    async def test_restart_host_integration(self, monkeypatch):
        import app.main as main
        stop = Mock(side_effect=RuntimeError("stop fail"))
        start = Mock(return_value="new-instance")
        mod = SimpleNamespace(stop=stop, start=start)
        register = Mock()
        monkeypatch.setattr(main, "_host_integrations_ref", [("feishu", mod, None)])
        monkeypatch.setattr(main._container, "integration_layer",
                            SimpleNamespace(register_host_integration=register))
        monkeypatch.setattr(main._container, "dispatcher", None)
        ok = main._restart_host_integration("feishu")
        assert ok is True
        stop.assert_called_once()
        start.assert_called_once()
        assert main._host_integrations_ref[0][2] == "new-instance"
        called_name, called_meta = register.call_args.args
        assert called_name == "feishu" and called_meta["alive"] is True        # 找不到该集成 → False
        assert main._restart_host_integration("ghost") is False

    def test_stop_host_integrations(self, monkeypatch):
        import app.main as main
        ok_mod = SimpleNamespace(stop=Mock())
        bad_mod = SimpleNamespace(stop=Mock(side_effect=RuntimeError("x")))
        no_stop = SimpleNamespace()
        main._stop_host_integrations([("a", ok_mod, 1), ("b", bad_mod, 2),
                                      ("c", no_stop, 3)])
        ok_mod.stop.assert_called_once()
        bad_mod.stop.assert_called_once()

    def test_primary_camera_state(self, monkeypatch):
        import app.main as main
        from app.agents.dispatcher import Dispatcher
        monkeypatch.setattr(main, "_services", {})
        assert main._primary_camera_state() == dict(Dispatcher.EMPTY_CAMERA_STATE)
        monkeypatch.setattr(main, "_services", {"camera_manager": SimpleNamespace()})
        assert main._primary_camera_state() == dict(Dispatcher.EMPTY_CAMERA_STATE)
        monkeypatch.setattr(main, "_services", {"camera_manager": SimpleNamespace(
            primary_camera_id=lambda: None)})
        assert main._primary_camera_state() == dict(Dispatcher.EMPTY_CAMERA_STATE)
        monkeypatch.setattr(main, "_services", {"camera_manager": SimpleNamespace(
            primary_camera_id=lambda: "cam1",
            get_state=lambda cid: {"id": cid, "online": True})})
        assert main._primary_camera_state() == {"id": "cam1", "online": True}


# ============================================================================
# app/main.py — 中间件与 SPA fallback（不进 lifespan 的 TestClient）
# ============================================================================

def _auth_header() -> dict:
    from app.core.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token(user_id='u', username='t')}"}


class TestMainMiddlewareAndSpa:
    @pytest.fixture(scope="class")
    def client(self, tmp_path_factory):
        """进入完整 lifespan 的共享客户端（与 test_http_smoke 同路径，DB 指向 tmp）。"""
        from fastapi.testclient import TestClient
        import app.main as main
        db_path = tmp_path_factory.mktemp("mw") / "mw" / "aether.db"
        old_path = dbmod.DB_PATH
        dbmod.DB_PATH = db_path
        try:
            with TestClient(main.app) as c:
                yield c
        finally:
            dbmod.DB_PATH = old_path
            asyncio.run(Database.close_all())
            Database._instance = None
            Database._db = None
            Database._write_lock = None

    def test_rate_limit_429(self, client, monkeypatch):
        import app.main as main
        monkeypatch.setattr(main.global_limiter, "check", lambda ip: False)
        resp = client.get("/api/health", headers=_auth_header())
        assert resp.status_code == 429
        assert resp.json()["code"] == "rate_limited"

    def test_app_token_bypass(self, client, monkeypatch):
        import app.main as main
        monkeypatch.setattr(main, "APP_TOKEN", "tok-123")
        resp = client.get("/api/health", headers={"X-API-Token": "tok-123"})
        assert resp.status_code == 200
        resp = client.get("/api/health", headers={"X-API-Token": "wrong"})
        assert resp.status_code == 401

    def test_refresh_token_rejected_by_middleware(self, client):
        from app.core.auth import create_refresh_token
        resp = client.get(
            "/api/health",
            headers={"Authorization": f"Bearer {create_refresh_token('u')}"})
        assert resp.status_code == 401

    def test_tracing_records_errors_and_sets_header(self, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main
        record = Mock()
        monkeypatch.setattr(main.metrics_service, "record_request", record)
        snap = Mock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(main.metrics_service, "snapshot", snap)
        # 不进 lifespan（该路由无启动依赖）；raise_server_exceptions=False
        # 让 ServerErrorMiddleware 把异常转成 500 响应而不是抛进测试
        client = TestClient(main.app, raise_server_exceptions=False)
        resp = client.get("/api/metrics", headers=_auth_header())
        assert resp.status_code == 500
        # 异常路径 header 不回写（仅成功响应附加），但 metrics 必须记 error=True
        assert record.call_args.kwargs.get("error") is True

    async def test_spa_fallback_branches(self, tmp_path, monkeypatch):
        import app.main as main
        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "real.txt").write_text("static", encoding="utf-8")
        monkeypatch.setattr(main, "FRONTEND_DIR", frontend)
        # api/ws 前缀 → 404 JSON
        resp = await main.spa_fallback("api/cameras")
        assert resp.status_code == 404
        resp = await main.spa_fallback("ws/chat")
        assert resp.status_code == 404
        # 路径穿越 → 404
        resp = await main.spa_fallback("../secret.txt")
        assert resp.status_code == 404
        # 存在的静态文件 → FileResponse
        resp = await main.spa_fallback("real.txt")
        assert resp.status_code == 200 and Path(resp.path).name == "real.txt"
        # 不存在且无 index.html → 重定向 /landing
        resp = await main.spa_fallback("missing-page")
        assert resp.status_code == 307 and resp.headers["location"] == "/landing"

    async def test_spa_fallback_serves_index(self, tmp_path, monkeypatch):
        import app.main as main
        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "index.html").write_text("<html>spa</html>", encoding="utf-8")
        monkeypatch.setattr(main, "FRONTEND_DIR", frontend)
        resp = await main.spa_fallback("some/route")
        assert resp.status_code == 200 and Path(resp.path).name == "index.html"


# ============================================================================
# app/main.py — lifespan（TestClient 进入完整生命周期；插件目录用空 tmp 隔离）
# ============================================================================

class TestLifespan:
    def test_lifespan_full_start_stop(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main

        # DB 指到 tmp，绝不碰真实 app/data
        db_path = tmp_path / "lifespan" / "a" / "aether.db"
        monkeypatch.setattr(dbmod, "DB_PATH", db_path)
        # APP_TOKEN 告警分支
        monkeypatch.setenv("APP_TOKEN", "some-token")
        # integration.enabled=True + 空插件目录 → 覆盖插件平台启动/停止分支
        empty_plugins = tmp_path / "plugins"
        empty_plugins.mkdir()
        real_get_config = main.get_config

        def fake_get_config(key, default=None):
            if key == "integration.enabled":
                return True
            if key == "integration.plugin_dir":
                return str(empty_plugins)
            return real_get_config(key, default)

        monkeypatch.setattr(main, "get_config", fake_get_config)
        # 测试环境无 chat LLM 配置，build_chat_agent 会直接抛错——打桩成最小 agent
        import app.agents.langgraph_agent as _la
        import app.mcp.langchain_tools as _lt
        monkeypatch.setattr(_la, "build_chat_agent",
                            lambda tools, model_config=None: ("AGENT", ()))
        monkeypatch.setattr(_lt, "convert_all_tools", lambda m, full_name=False: [])
        # 不真正启动宿主侧集成（本环境 .env 可能配了真实飞书凭证，防止外连）
        monkeypatch.setattr(main, "_start_host_integrations", lambda c, loop: [])
        # 后台周期循环加速（>=5s 的 sleep 压缩为 0.05s），让
        # _periodic_health_loop / _ha_catalog_refresh_loop 循环体真实执行
        real_sleep = asyncio.sleep

        async def fast_sleep(delay, *a, **k):
            if isinstance(delay, (int, float)) and delay >= 5:
                await real_sleep(0.05, *a, **k)
            else:
                await real_sleep(delay, *a, **k)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        with TestClient(main.app) as c:
            # 启动期副作用可观察
            assert main._container.integration_layer is not None
            assert main._container.integration_layer.sink_manager is not None
            assert main.dispatcher is not None
            assert main.langgraph_agent is not None
            assert main._container.scheduler_service is not None
            assert main._container.scene_service is not None
            assert main._container.weekly_report_service is not None
            assert main._container.tool_deps is not None
            assert c.get("/healthz").status_code == 200
            body = c.get("/healthz").json()
            assert body["status"] == "ok" and "uptime_seconds" in body
        # 关闭期副作用：全局状态复位、数据库关闭、调度器停止
        assert main.dispatcher is None
        assert main.langgraph_agent is None
        assert main._container.dispatcher is None
        assert Database._db is None
        assert main._container.scheduler_service._running is False

