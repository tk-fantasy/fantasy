"""事件统计聚合（家庭报告页图表数据源）回归测试。

覆盖：
- family_events actor 列迁移（旧库无列时 _ensure_column 补列）
- family_events_stats 聚合：totals / daily 分桶 / top_devices 分组与
  AI/手动拆分（含存量数据无 actor 列值时按 message 前缀回退）
- GET /api/events/stats 路由（days 边界）
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _reset_db_singleton(monkeypatch, tmp_path):
    """隔离进程级单例：其他测试文件（如 test_action_maps_route）会把
    Database._db 置 None 留下失联连接，这里先复位单例并把 DB_PATH 指到
    本用例的临时库；连接登记进 _open_conns，由 conftest 的 close_all 统一回收。
    """
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")


@pytest.mark.asyncio
async def test_actor_column_migration(tmp_path, monkeypatch):
    """旧库（无 actor 列）打开时自动补列，写入带 actor 不报错。"""
    import aiosqlite

    from app.core.database import Database
    db_path = tmp_path / "old.db"
    # 手工建一个"旧版" family_events 表（无 actor 列）
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS family_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,"
            "kind TEXT NOT NULL, source TEXT DEFAULT '', message TEXT DEFAULT '')"
        )
        await conn.execute(
            "INSERT INTO family_events (created_at, kind, source, message) VALUES (?, ?, ?, ?)",
            (int(time.time() * 1000), "device_op", "device:light.old", "AI将「旧灯」执行 打开"),
        )
        await conn.commit()

    monkeypatch.setattr("app.core.database.DB_PATH", db_path)  # 指向手工建的旧库
    await Database.init()
    try:
        db = Database.get()
        # 迁移后旧行可读、新行可带 actor 写入
        rows = await db.family_events_since(0)
        assert rows[0]["actor"] == ""
        await db.family_event_add("device_op", "device:light.new", "AI将「新灯」执行 打开", "AI")
        rows = await db.family_events_since(0)
        assert rows[-1]["actor"] == "AI"
    finally:
        # Database 是进程级单例（init 见 _instance 直接复用），不 close 会
        # 把本库连接泄给下一个用例
        await Database.close()


@pytest.mark.asyncio
async def test_stats_aggregation(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    db = Database.get()
    now = int(time.time() * 1000)
    day_ms = 24 * 3600 * 1000

    # 覆盖所有 kind 与 actor 两种来源（结构化 actor + 存量 message 前缀回退）
    await db.family_event_add("device_op", "device:light.a", "AI将「卧室灯」执行 打开", "AI")
    await db.family_event_add("device_op", "device:light.a", "手动将「卧室灯」执行 关闭", "手动")
    await db.family_event_add("device_op", "device:switch.b", "AI将「插座」执行 打开", "AI")
    # 存量数据：无 actor（空串）→ 回退 message 前缀
    await db.family_event_add("device_op", "device:light.a", "手动将「卧室灯」执行 打开")
    await db.family_event_add("automation", "rule:回家", "规则「回家」条件成立，执行了 2 个动作")
    await db.family_event_add("task_success", "scheduler:早安", "任务完成")
    await db.family_event_add("task_failed", "scheduler:晚安", "任务失败")
    await db.family_event_add("alert", "camera:c1", "摄像头离线")
    await db.family_event_add("alert_resolved", "camera:c1", "摄像头恢复")
    await db.family_event_add("device_state", "device:sensor.x", "传感器 1 小时内变化 3 次")
    await db.family_event_add("weekly_report", "weekly", "周报文本")

    stats = await db.family_events_stats(now - day_ms)

    # totals 按 kind 计数
    assert stats["totals"]["device_op"] == 4
    assert stats["totals"]["automation"] == 1
    assert stats["totals"]["task_success"] == 1
    assert stats["totals"]["task_failed"] == 1
    assert stats["totals"]["alert"] == 1
    assert stats["totals"]["device_state"] == 1

    # daily 分桶：今天的桶里各分类计数正确（alert_resolved 并入 alert，task_* 并入 task）
    today = stats["daily"][-1]
    assert today["device_op"] == 4
    assert today["automation"] == 1
    assert today["task"] == 2
    assert today["alert"] == 2
    # device_state 不进堆叠桶（噪声）
    assert "device_state" not in today

    # top_devices：按设备分组 + AI/手动拆分（含回退）+ 中文名提取
    top = {d["entity"]: d for d in stats["top_devices"]}
    assert top["light.a"]["count"] == 3
    assert top["light.a"]["ai"] == 1
    assert top["light.a"]["manual"] == 2
    assert top["light.a"]["name"] == "卧室灯"
    assert top["switch.b"]["count"] == 1
    assert top["switch.b"]["name"] == "插座"
    # 排序按次数降序
    counts = [d["count"] for d in stats["top_devices"]]
    assert counts == sorted(counts, reverse=True)

    # actor 总计
    assert stats["actor"] == {"ai": 2, "manual": 2}

    await Database.close()


@pytest.mark.asyncio
async def test_stats_empty(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    stats = await Database.get().family_events_stats(int(time.time() * 1000))
    assert stats["totals"] == {}
    assert stats["daily"] == []
    assert stats["top_devices"] == []
    assert stats["actor"] == {"ai": 0, "manual": 0}

    await Database.close()


@pytest.mark.asyncio
async def test_stats_route_days_boundary(tmp_path, monkeypatch):
    """路由层：days 合法值透传；ge/le 边界由 FastAPI Query 声明保证。"""
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    try:
        db = Database.get()
        await db.family_event_add("device_op", "device:light.a", "AI将「灯」执行 打开", "AI")

        from app.routes.report_routes import events_stats
        res = await events_stats(days=90)
        assert res.data["totals"]["device_op"] == 1
        assert res.data["actor"]["ai"] == 1
        # Query(ge=1, le=90) 校验在 FastAPI 路由层，直调函数不触发——
        # 改为校验参数声明里的约束元数据
        import inspect

        meta = inspect.signature(events_stats).parameters["days"].default.metadata
        constraints = {type(m).__name__: getattr(m, m.__class__.__name__.lower()) for m in meta}
        assert constraints.get("Ge") == 1 and constraints.get("Le") == 90
    finally:
        await Database.close()


@pytest.mark.asyncio
async def test_events_list_quota_keeps_lowfreq_kinds_visible(tmp_path):
    """时间线 500 条截断按类型保底：高频 device_state 不该把低频 automation 挤出窗口。"""
    from app.core.database import Database
    await Database.init()
    try:
        db = Database.get()
        now = int(time.time() * 1000)
        # 600 条高频 device_state + 4 条更早的 automation
        for i in range(600):
            await db.family_event_add("device_state", "device:sensor.x", f"变化 {i}")
        for i in range(4):
            await db.family_event_add("automation", "rule:回家", f"规则「回家」执行 {i}")

        from app.routes.report_routes import list_events
        res = await list_events(days=7, kind="")
        kinds = {}
        for e in res.data:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        # automation 保底露出（≤80 条配额内全保留），不再为 0
        assert kinds.get("automation") == 4
        # 总量仍受 500 上限约束
        assert len(res.data) <= 500
        # 高频事件占其余名额
        assert kinds.get("device_state", 0) >= 400
        # 最新在前
        assert res.data[0]["created_at"] >= res.data[-1]["created_at"]
    finally:
        await Database.close()


@pytest.mark.asyncio
async def test_events_list_date_drilldown(tmp_path):
    """date=YYYY-MM-DD 精确查某一天：只含当天事件， days 被忽略。"""
    import datetime as dt

    from app.core.database import Database
    await Database.init()
    try:
        db = Database.get()
        # 今天 23:00 前的"昨天"事件
        now = dt.datetime.now().astimezone()
        yesterday = now - dt.timedelta(days=1)
        ts_y = int(yesterday.replace(hour=12, minute=0).timestamp() * 1000)
        ts_t = int(now.timestamp() * 1000) - 1000
        await db._db.execute(
            "INSERT INTO family_events (created_at, kind, source, message, actor) VALUES (?,?,?,?,?)",
            (ts_y, "automation", "rule:昨天", "昨天的自动化", ""))
        await db._db.execute(
            "INSERT INTO family_events (created_at, kind, source, message, actor) VALUES (?,?,?,?,?)",
            (ts_t, "automation", "rule:今天", "今天的自动化", ""))
        await db._db.commit()

        from app.routes.report_routes import list_events
        res = await list_events(days=7, kind="", date=yesterday.strftime("%Y-%m-%d"))
        assert len(res.data) == 1
        assert res.data[0]["message"] == "昨天的自动化"
    finally:
        await Database.close()
