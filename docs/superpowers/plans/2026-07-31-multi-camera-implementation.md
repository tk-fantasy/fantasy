# 多摄像头改造 — 可执行 TDD 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Aether 现有单摄像头全局单例改造为一台 Aether 管理多路(首期 4 路)摄像头,每路独立配置/采集/运动门控,共享受控的云端 VLM 双通道并发预算(展示 1 + 自动化/工具 5),并整合 ONVIF 自动发现(每路独立 MAC 绑定)。

**Architecture:** 引入 `CameraManager`(多路生命周期 + 双通道 `Semaphore` 调度)管理 `dict[camera_id → CameraStream]`;`CameraStream` 参数化,不再读全局 config;`PtzService`/`CameraDiscoveryService` 从全局单例改为按 `camera_id` 参数化;设备/规则/关注项配置真源迁移到 DB `cameras` 表;规则(`rules.data` JSON blob)与视觉关注项(KV)加 `camera_id`。

**Tech Stack:** Python 3 (asyncio + aiosqlite + OpenCV + onvif-zeep-async),FastAPI,pytest/pytest-asyncio,Vue 3 Composition API + `<script setup>`。

**Spec / 大纲来源:** `docs/superpowers/specs/2026-07-29-multi-camera-design.md`(已批准 spec)。本计划在 spec 基础上补全实施细节(已用「⚠️ 实施注意」标注),并把原有 12 步大纲升级为含完整测试+实现代码的 TDD 计划。

## Global Constraints

- **ID 方案**:摄像头 ID 是 `cam_<6位随机>`(生成一次永不改)+ 用户可改 `name` + 显示序号 `sort_order`(删除留空缺不重排)。
- **流协议**:MJPEG。不引 ZLM/录像(本期非目标)。
- **VLM 并发**:云端 glm-4v(上限 10)。展示推理全局 1 路(独立通道);自动化+工具共享并发池上限 5;峰值 1+5=6 < 10。
- **密钥存储**:`cameras` 表密码字段存明文(`rtsp_password`/`ptz_password`),与现有 `user_settings` 存明文 LLM key 一致,是项目既有模式。
- **幂等迁移判据**:用 KV 标记 `cameras_migrated == "1"`(非 spec 的"表非空",防全新部署删空 + 残留 env 误判)。
- **全局端点兼容**:`/api/health`、`/api/state` 返回主摄像头(第一个 enabled)状态,保留 `/camera` 弹窗外的前端引用不崩。
- **命名/拷贝**:中文 UI 文案;commit 前缀 `feat:`/`test:`/`refactor:`/`docs:`,中文描述。
- **测试约定**:pytest + `@pytest.mark.asyncio`;`@patch("app.core.config.CONFIG", ...)` 由 `conftest.py` autouse 提供;ONVIF/HA 用 `AsyncMock`/`MagicMock`;服务直接实例化,无 DB fixture。
- **Python 注释风格**:匹配现有代码——中文注释,说明"为什么"而非"做什么"。

---

## File Structure

**新增文件:**

| 文件 | 职责 |
|---|---|
| `app/services/camera_manager.py` | 多路摄像头生命周期(`dict[str, CameraStream]`)+ 双通道 `Semaphore` 并发调度 + CRUD 转发 DB |
| `app/routes/camera_routes.py` | `/api/cameras` 全套 REST(吸收 ptz/discovery/focuses/vision-focus 端点) |
| `tests/test_camera_manager.py` | CameraManager 生命周期 + 双通道并发上限单测 |
| `tests/test_camera_stream.py` | CameraStream 参数化重构单测(现有无此文件) |
| `frontend/src/views/CameraSettingsView.vue` | 摄像头管理页(卡片列表 + 编辑面板) |
| `frontend/src/composables/useCamera.js` | camera API 封装(CRUD + display 控制 + focuses) |

**修改文件:**

| 文件 | 改动 |
|---|---|
| `app/core/database.py` | 加 `cameras` 表 DDL + `cameras_*` CRUD + `_ensure_column("rules","camera_id")` + 迁移逻辑 |
| `app/camera_stream.py` | `__init__` 参数化(读 config dict 不读全局)+ `set_display_enabled` + 回调带 camera_id |
| `app/services/camera_discovery_service.py` | 所有方法加 `camera_id`,MAC/子网/凭证从 cameras 行读 |
| `app/services/ptz_service.py` | 按相机管理(去单例化或 `dict[camera_id → PtzService]`) |
| `app/services/vision_service.py` | `_vision_focuses` 改 `dict[str, list]`,方法加 `camera_id` |
| `app/services/automation_service.py` | `evaluate(frames, camera_id="")` 按摄像头过滤规则 |
| `app/services/rule_registry_service.py` | `AutomationRule` 加 `camera_id`,`load_from_db`/`add_rule` 透传 |
| `app/services/rule_service.py` | `build_rule` 带 `camera_id`,`_fallback_rule`/`setdefault` 补字段 |
| `app/container.py` | `camera_stream` 字段 → `camera_manager` |
| `app/bootstrap.py` | `CameraManager(...)` 替代 `CameraStream(...)` |
| `app/main.py` | lifespan camera 接线 + 路由注册 + 后台 MAC 捕获 + health/state 兼容 |
| `app/tools.py` | `vision_chat`/`verify_condition` 加 camera_id + 摄像头列表注入 |
| `app/mcp/local_mcp_servers.py` | `create_verify_condition_handler` 加 camera_id |
| `app/agents/dispatcher.py` | 取 focus / get_state 带 camera_id |
| `app/agents/automation_agent.py` | 按摄像头取 frames |
| `app/routes/settings_routes.py` | 移除 vision-focus 6 个 handler(迁到 camera_routes) |
| `app/routes/rule_routes.py` | 规则端点支持 `?camera_id=` 过滤 |
| `config.example.json` | 删除 `vision`/`ptz`/`automation.camera_vl_display_enabled` 段 |
| `tests/conftest.py` | `_patch_config` 保留 vision/ptz 段供迁移测试用 |
| `frontend/src/views/ChatView.vue` | `/camera` 弹窗加切换标签 + display 控制 |
| `frontend/src/views/MonitorView.vue` | 多路适配 |
| `frontend/src/router/index.js` | 新路由 `/cameras` |
| `frontend/src/components/SidebarNav.vue` | 导航项 |
| `frontend/src/utils/api.js` | cameraAPI |

**删除文件(迁移完成后):**

| 文件 | 原因 |
|---|---|
| `app/routes/ptz_routes.py` | 合并到 camera_routes(先共存验证,Step 6 末删) |
| `app/routes/discovery_routes.py` | 合并到 camera_routes(同上) |

---

## Task 1: DB — `cameras` 表 + `rules.camera_id` 列 + 幂等迁移

**Files:**
- Modify: `app/core/database.py` (DDL 在 `:39-93` executescript 内;`_ensure_column` 在 `:99-104`;迁移逻辑加在 `init()` 内 `:109` 之后)
- Modify: `tests/conftest.py`(`_patch_config` `:29-84`,确认保留 vision/ptz 段)
- Test: `tests/test_database.py`(现有文件,追加测试类)

**Interfaces:**
- Consumes: `db.kv_get(key)` / `db.kv_set(key, value)`(KV 表已存在,`database.py:40-44`)
- Produces: `cameras` 表;`rules` 表 `camera_id` 列;`cameras_all/cameras_get/cameras_insert/cameras_update/cameras_delete` 方法;KV `cameras_migrated` 标记;`_ensure_column("rules", "camera_id", ...)`。Step 2+ 依赖 `cameras_get(camera_id)` 返回 dict;Step 3 依赖 `cameras_update(camera_id, {...})`;Step 4 manager.initialize 依赖 `cameras_all()`。

- [ ] **Step 1.1: 写失败测试 — `cameras` 表 DDL + CRUD**

追加到 `tests/test_database.py`(若不存在则新建,文件头需 `import pytest` + `from unittest.mock import patch` + `from app.core import database`):

```python
@pytest.mark.asyncio
async def test_cameras_table_and_crud(tmp_path, monkeypatch):
    """cameras 表建表幂等 + CRUD 全套。"""
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    db = await database.Database.init()
    try:
        # 二次 init 幂等:表已存在不应报错
        await database.Database.init()

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
            "motion_check_interval": 1.0, "vision_downscale": 448,
            "vision_jpeg_quality": 60, "vision_min_infer_interval": 8.0,
            "vision_max_idle_interval": 120.0, "vision_use_img_count": 3,
            "frame_interval_ms": 1000, "display_enabled": 1,
        })
        assert new_id == "cam_aaaaaa"

        # get
        row = await db.cameras_get("cam_aaaaaa")
        assert row["name"] == "客厅"
        assert row["source_type"] == "rtsp"
        assert row["rtsp_url"] == "rtsp://1.2.3.4/stream"

        # all
        all_rows = await db.cameras_all()
        assert len(all_rows) == 1

        # update
        ok = await db.cameras_update("cam_aaaaaa", {"name": "客厅2", "ptz_speed": 0.8})
        assert ok is True
        row2 = await db.cameras_get("cam_aaaaaa")
        assert row2["name"] == "客厅2"
        assert row2["ptz_speed"] == 0.8
        # 未传字段保留
        assert row2["rtsp_url"] == "rtsp://1.2.3.4/stream"

        # delete
        ok = await db.cameras_delete("cam_aaaaaa")
        assert ok is True
        assert await db.cameras_get("cam_aaaaaa") is None
    finally:
        await database.Database.close()
```

- [ ] **Step 1.2: 运行测试,确认失败**

Run: `pytest tests/test_database.py::test_cameras_table_and_crud -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'cameras_insert'`(表/方法尚未存在)

- [ ] **Step 1.3: 实现 — 加 DDL + CRUD**

在 `database.py` 的 `init()` executescript 内(`:92` `scheduled_tasks` 表之后)追加 `cameras` 表 DDL。字段名严格对应 spec §4 映射表的 cameras 列名:

```python
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
        vision_downscale            INTEGER DEFAULT 448,
        vision_jpeg_quality         INTEGER DEFAULT 60,
        vision_min_infer_interval   REAL DEFAULT 8.0,
        vision_max_idle_interval    REAL DEFAULT 120.0,
        vision_use_img_count        INTEGER DEFAULT 3,
        frame_interval_ms           INTEGER DEFAULT 1000,
        display_enabled             INTEGER DEFAULT 1,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
```

在同一 executescript 末尾、`_ensure_column` 调用处(`:106-109`)追加一行:

```python
        await _ensure_column("rules", "camera_id", "camera_id TEXT DEFAULT ''")
```

在 `Database` 类内(紧挨现有 `rules_*` 方法之后,约 `:174` 之后)加 CRUD 方法。模式对齐 `rules_*`(`rules_all` 在 `:140`、`rules_insert` 在 `:151`):

```python
    async def cameras_all(self) -> list[dict]:
        """获取所有摄像头,按 sort_order 排序。"""
        async with self._db.execute(
            "SELECT * FROM cameras ORDER BY sort_order, created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def cameras_get(self, camera_id: str) -> dict | None:
        """按 id 取单路;不存在返回 None。"""
        async with self._db.execute(
            "SELECT * FROM cameras WHERE id = ?", (camera_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def cameras_insert(self, data: dict) -> str:
        """插入一路摄像头。data 必须含 id。"""
        cols = ("id", "name", "enabled", "sort_order", "source_type", "usb_index",
                "rtsp_url", "rtsp_username", "rtsp_password", "area", "device_mac",
                "discovery_enabled", "ptz_enabled", "ptz_ip", "ptz_port",
                "ptz_username", "ptz_password", "ptz_speed", "ptz_step_ms",
                "motion_hash_size", "motion_threshold", "motion_check_interval",
                "vision_downscale", "vision_jpeg_quality", "vision_min_infer_interval",
                "vision_max_idle_interval", "vision_use_img_count", "frame_interval_ms",
                "display_enabled")
        values = [data.get(c) for c in cols]
        placeholders = ",".join(["?"] * len(cols))
        col_list = ",".join(cols)
        async with self._db.execute(
            f"INSERT INTO cameras ({col_list}) VALUES ({placeholders})", values
        ):
            await self._db.commit()
        return data["id"]

    async def cameras_update(self, camera_id: str, fields: dict) -> bool:
        """部分更新指定列(id/created_at/updated_at 不可改)。更新 updated_at。"""
        if not fields:
            return False
        forbidden = {"id", "created_at"}
        set_cols = [k for k in fields if k not in forbidden]
        if not set_cols:
            return False
        assignments = ",".join([f"{c} = ?" for c in set_cols])
        params = [fields[c] for c in set_cols] + [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), camera_id]
        async with self._db.execute(
            f"UPDATE cameras SET {assignments}, updated_at = ? WHERE id = ?", params
        ) as cur:
            await self._db.commit()
            return cur.rowcount > 0

    async def cameras_delete(self, camera_id: str) -> bool:
        async with self._db.execute(
            "DELETE FROM cameras WHERE id = ?", (camera_id,)
        ) as cur:
            await self._db.commit()
            return cur.rowcount > 0
```

⚠️ 检查 `database.py` 顶部是否已 import `datetime`(供 `cameras_update` 的 `updated_at`)。若无,加 `from datetime import datetime`。

- [ ] **Step 1.4: 运行测试,确认通过**

Run: `pytest tests/test_database.py::test_cameras_table_and_crud -v`
Expected: PASS

- [ ] **Step 1.5: 写失败测试 — rules 表 camera_id 列**

```python
@pytest.mark.asyncio
async def test_rules_camera_id_column(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(db_path := tmp_path / "t.db"))
    db = await database.Database.init()
    try:
        async with db._db.execute("PRAGMA table_info(rules)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        assert "camera_id" in cols
    finally:
        await database.Database.close()
```

- [ ] **Step 1.6: 运行,确认通过(Step 1.3 已加迁移)**

Run: `pytest tests/test_database.py::test_rules_camera_id_column -v`
Expected: PASS(因为 Step 1.3 已经加了 `_ensure_column("rules", "camera_id", ...)`)。

- [ ] **Step 1.7: 写失败测试 — 幂等迁移逻辑**

⚠️ **实施注意**:迁移判据用 KV 标记 `cameras_migrated`,非 spec §4 的"表非空"。理由:全新部署时用户可能手动删空所有摄像头但 config.json 仍有旧 env 残留,"表非空"会误判为"已迁移"导致永不迁移。

```python
@pytest.mark.asyncio
async def test_migration_from_legacy_config(tmp_path, monkeypatch):
    """老部署(config 有 vision/ptz 段)首次迁移:生成一条默认摄像头记录。"""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "t.db"))
    # 模拟老 config.json
    legacy = {
        "vision": {"rtsp_url": "rtsp://192.168.1.10/x",
                   "rtsp_username": "admin", "motion_threshold": 20,
                   "motion_hash_size": 16, "downscale_max_side": 448,
                   "jpeg_quality": 70, "min_infer_interval_seconds": 3.0,
                   "max_idle_interval_seconds": 60.0, "vision_use_img_count": 3,
                   "frame_interval_ms": 1000, "device_mac": "60-a3-e3-de-e0-54"},
        "ptz": {"enabled": True, "ip": "192.168.1.10", "port": 80,
                "username": "admin", "speed": 0.5, "step_ms": 300},
        "automation": {"camera_vl_display_enabled": True},
    }
    with patch.object(database, "_legacy_camera_config", return_value=legacy), \
         patch.object(database, "_read_env_secret", side_effect=lambda k: {"RTSP_PASSWORD": "rp", "PTZ_PASSWORD": "pp"}.get(k, "")):
        db = await database.Database.init()
        try:
            rows = await db.cameras_all()
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
            # KV 标记已置位
            assert (await db.kv_get("cameras_migrated")) == "1"
        finally:
            await database.Database.close()

@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path, monkeypatch):
    """二次 init 不重复迁移(KV 标记命中)。"""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "t.db"))
    legacy = {"vision": {"rtsp_url": "rtsp://x"}, "ptz": {"ip": "1.1.1.1"},
              "automation": {"camera_vl_display_enabled": True}}
    with patch.object(database, "_legacy_camera_config", return_value=legacy), \
         patch.object(database, "_read_env_secret", return_value=""):
        await database.Database.init()
        await database.Database.close()
        # 二次
        db = await database.Database.init()
        try:
            rows = await db.cameras_all()
            assert len(rows) == 1   # 不是 2
        finally:
            await database.Database.close()

@pytest.mark.asyncio
async def test_migration_skipped_for_new_deploy(tmp_path, monkeypatch):
    """全新部署(无 legacy config)跳过迁移,cameras 表为空,KV 不置位。"""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "t.db"))
    with patch.object(database, "_legacy_camera_config", return_value=None):
        db = await database.Database.init()
        try:
            assert await db.cameras_all() == []
            assert (await db.kv_get("cameras_migrated")) is None
        finally:
            await database.Database.close()
```

- [ ] **Step 1.8: 实现 — 迁移逻辑**

在 `database.py` 加模块级辅助函数(放在 `Database` 类之外,靠近文件顶部 import 之后):

```python
import os
import secrets

def _legacy_camera_config() -> dict | None:
    """检测 config.json 是否有旧 vision/ptz 段;有则返回三段合并 dict,无则 None。"""
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
    """从 .env / os.environ 读一个明文密钥(迁移专用,读后会被删)。"""
    return os.environ.get(name, "")
```

在 `Database.init()` 内,`_ensure_column(... rules camera_id ...)` 之后、`await self._db.commit()` 之后(`:109` 之后)插入迁移逻辑:

```python
        # —— 单摄像头 → 多路迁移(KV 标记幂等)——
        if (await self.kv_get("cameras_migrated")) != "1":
            legacy = _legacy_camera_config()
            if legacy is not None:
                import asyncio
                cid = f"cam_{secrets.token_hex(3)}"
                v = legacy.get("vision", {})
                p = legacy.get("ptz", {})
                rtsp_url = str(v.get("rtsp_url", ""))
                await self.cameras_insert({
                    "id": cid, "name": "默认摄像头", "enabled": 1, "sort_order": 0,
                    "source_type": "rtsp" if rtsp_url else "usb",
                    "usb_index": int(v.get("camera_index", 0)) if not rtsp_url else None,
                    "rtsp_url": rtsp_url,
                    "rtsp_username": str(v.get("rtsp_username", "")),
                    "rtsp_password": _read_env_secret("RTSP_PASSWORD"),
                    "area": "", "device_mac": str(v.get("device_mac", "")),
                    "discovery_enabled": 1,
                    "ptz_enabled": 1 if p.get("enabled") else 0,
                    "ptz_ip": str(p.get("ip", "")),
                    "ptz_port": int(p.get("port", 80)),
                    "ptz_username": str(p.get("username", "")),
                    "ptz_password": _read_env_secret("PTZ_PASSWORD"),
                    "ptz_speed": float(p.get("speed", 0.5)),
                    "ptz_step_ms": int(p.get("step_ms", 300)),
                    "motion_hash_size": int(v.get("motion_hash_size", 16)),
                    "motion_threshold": int(v.get("motion_threshold", 15)),
                    "motion_check_interval": float(v.get("motion_check_interval_seconds", 1.0)),
                    "vision_downscale": int(v.get("downscale_max_side", 448)),
                    "vision_jpeg_quality": int(v.get("jpeg_quality", 60)),
                    "vision_min_infer_interval": float(v.get("min_infer_interval_seconds", 8.0)),
                    "vision_max_idle_interval": float(v.get("max_idle_interval_seconds", 120.0)),
                    "vision_use_img_count": int(v.get("vision_use_img_count", 3)),
                    "frame_interval_ms": int(v.get("frame_interval_ms", 1000)),
                    "display_enabled": 1 if legacy.get("automation", {}).get("camera_vl_display_enabled") else 0,
                })
                # 把现有规则的 camera_id 回填到新 id(data JSON blob 内 + 列都设)
                async with self._db.execute("SELECT id, data FROM rules") as cur:
                    for row in await cur.fetchall():
                        import json as _json
                        d = _json.loads(row[1]) if row[1] else {}
                        d["camera_id"] = cid
                        await self._db.execute(
                            "UPDATE rules SET data = ?, camera_id = ? WHERE id = ?",
                            (_json.dumps(d, ensure_ascii=False), cid, row[0]))
                # vision_focuses KV 每条加 camera_id(若存在)
                fv = await self.kv_get("vision_focuses")
                if fv:
                    import json as _json2
                    try:
                        arr = _json2.loads(fv)
                        for item in arr:
                            item.setdefault("camera_id", cid)
                        await self.kv_set("vision_focuses", _json2.dumps(arr, ensure_ascii=False))
                    except (ValueError, TypeError):
                        pass
                await self._db.commit()
            await self.kv_set("cameras_migrated", "1")
```

⚠️ config 段与 .env 密钥的**删除**不在本步做(避免迁移成功但删除后回滚困难)。删除逻辑放在 Step 11(配置清理),迁移完成且联调通过后由人工/脚本执行。

- [ ] **Step 1.9: 运行迁移测试,确认通过**

Run: `pytest tests/test_database.py -v -k "migration or cameras_table or rules_camera_id"`
Expected: 4 个测试全 PASS

- [ ] **Step 1.10: 全量回归**

Run: `pytest tests/test_database.py -v`
Expected: 全部 PASS(新测试不破坏既有 rules/sessions/kv 测试)

- [ ] **Step 1.11: Commit**

```bash
git add app/core/database.py tests/test_database.py
git commit -m "feat(db): cameras 表 + rules.camera_id 列 + 单路→多路幂等迁移"
```

---

## Task 2: CameraStream 参数化重构 + per-camera focuses

**Files:**
- Modify: `app/camera_stream.py`(`CameraState` `:30-49`;`__init__` `:64-127`;`set_camera_vl_display_enabled` `:329`;`set_on_automation_trigger` `:321`;`_resolve_rtsp_url` `:486-506`;worker 内 discovery 调用 `:614`)
- Modify: `app/services/vision_service.py`(`_vision_focuses` `:35`;focus CRUD `:64-93`)
- Test: `tests/test_camera_stream.py`(新建)

**Interfaces:**
- Consumes: `cameras` 表行映射成的 `config: dict`(Step 1 产出);`VisionService` per-camera focus API(本步改造)。
- Produces: `CameraStream(camera_id, config, vision_service, ...)` 构造;`set_display_enabled(bool)`;`set_on_automation_trigger(callback)` 回调签名变 `Callable[[str], None]`;`CameraState.camera_id`;worker 调 `find_and_apply(self.camera_id)`;`VisionService._vision_focuses: dict[str, list[dict]]` + 各方法带 `camera_id`。Step 4 manager 依赖这些。

- [ ] **Step 2.1: 写失败测试 — 参数化构造 + display 开关 + 回调带 camera_id**

新建 `tests/test_camera_stream.py`。核心是用 config dict 构造,而非全局 config:

```python
import pytest
from unittest.mock import MagicMock, AsyncMock
from app.camera_stream import CameraStream, CameraState


def _config(overrides=None):
    """单路配置 dict(cameras 表行映射)。"""
    base = {
        "id": "cam_test01", "source_type": "rtsp",
        "rtsp_url": "rtsp://1.2.3.4/stream", "rtsp_username": "admin",
        "rtsp_password": "pwd", "usb_index": 0,
        "motion_hash_size": 16, "motion_threshold": 15, "motion_check_interval": 1.0,
        "vision_downscale": 448, "vision_jpeg_quality": 60,
        "vision_min_infer_interval": 8.0, "vision_max_idle_interval": 120.0,
        "vision_use_img_count": 3, "frame_interval_ms": 1000, "display_enabled": 1,
    }
    base.update(overrides or {})
    return base


class TestCameraStreamConstruction:
    def test_reads_camera_id_from_config(self):
        vs = MagicMock()
        s = CameraStream("cam_test01", _config(), vision_service=vs)
        assert s.camera_id == "cam_test01"

    def test_reads_params_from_config_dict(self):
        """不读全局 get_config,全从 config dict 读。"""
        vs = MagicMock()
        cfg = _config({"motion_threshold": 25, "vision_min_infer_interval": 12.0})
        s = CameraStream("cam_test01", cfg, vision_service=vs)
        assert s._motion.threshold == 25
        assert s._min_infer_interval == 12.0

    def test_state_has_camera_id(self):
        s = CameraStream("cam_test01", _config(), vision_service=MagicMock())
        st = s.get_state()
        assert st["camera_id"] == "cam_test01"


class TestDisplaySwitch:
    def test_set_display_enabled_toggles_flag(self):
        s = CameraStream("cam_test01", _config({"display_enabled": 0}), vision_service=MagicMock())
        assert s._camera_vl_display_enabled is False
        s.set_display_enabled(True)
        assert s._camera_vl_display_enabled is True
        s.set_display_enabled(False)
        assert s._camera_vl_display_enabled is False


class TestAutomationTriggerCallback:
    def test_callback_receives_camera_id(self):
        received = []
        s = CameraStream("cam_test01", _config(), vision_service=MagicMock())
        s.set_on_automation_trigger(lambda cid: received.append(cid))
        s._on_automation_trigger()  # 模拟运动触发
        assert received == ["cam_test01"]
```

- [ ] **Step 2.2: 运行,确认失败**

Run: `pytest tests/test_camera_stream.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument` / `AttributeError`(尚未参数化)。

- [ ] **Step 2.3: 实现 — `CameraStream` 参数化**

改 `__init__`(`camera_stream.py:64`),签名改为:

```python
    def __init__(self, camera_id: str, config: dict,
                 vision_service: "VisionService | None" = None,
                 on_automation_trigger=None,
                 discovery_service=None) -> None:
        self.camera_id = camera_id
        self._config = config
        c = config  # 单字符别名,正文读字段用
        # 摄像头来源:rtsp 优先于 usb_index
        self._rtsp_url = str(c.get("rtsp_url", "")).strip()
        self._camera_index = int(c.get("usb_index", 0)) if not self._rtsp_url else 0
        self._rtsp_username = str(c.get("rtsp_username", ""))
        self._rtsp_password = str(c.get("rtsp_password", ""))
        self._recognizer = vision_service or VisionService()
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._cap = None
        self._latest_frame = None
        self._latest_jpeg = None
        self._latest_result = ActionResult("idle", "等待识别。", {"source": "vision", "enabled": self._recognizer.enabled, "camera_id": camera_id})
        self._infer_busy = False
        self._presence_count = 0
        self._absence_count = 0
        self._presence_threshold = 3

        self._motion = MotionDetector(
            hash_size=int(c.get("motion_hash_size", 16)),
            threshold=int(c.get("motion_threshold", 15)),
        )
        self._motion_check_interval = max(0.05, float(c.get("motion_check_interval", 0.2)))
        self._min_infer_interval = max(0.5, float(c.get("vision_min_infer_interval", 3.0)))
        self._max_idle_interval = max(self._min_infer_interval, float(c.get("vision_max_idle_interval", 60.0)))
        self._last_motion_check = 0.0
        self._last_model_run_at = 0.0
        self._infer_count = 0
        self._infer_started_at = 0.0
        self._infer_timeout = max(5.0, float(c.get("vision_infer_timeout_seconds", 45.0)))

        # 健壮性参数(保留原默认,可由 config 覆盖)
        self._last_success_backend = None
        self._consecutive_open_failures = 0
        self._read_retry_count = max(0, int(c.get("read_retry_count", 3)))
        self._read_retry_interval = max(0.02, float(c.get("read_retry_interval_seconds", 0.1)))
        self._release_cooldown = max(0.1, float(c.get("release_cooldown_seconds", 0.8)))
        self._max_backoff = max(self._release_cooldown, float(c.get("max_backoff_seconds", 15.0)))
        self._slow_read_streak = 0
        self._slow_read_ms = max(200.0, float(c.get("slow_read_ms_threshold", 500.0)))

        self._frame_interval_ms = max(0, int(c.get("frame_interval_ms", 1000)))
        self._vision_use_img_count = max(1, int(c.get("vision_use_img_count", 3)))
        self._frame_buffer = collections.deque(maxlen=self._vision_use_img_count)
        self._last_buffer_push = 0.0
        self._infer_futures = []

        self._camera_vl_display_enabled = bool(c.get("display_enabled", 1))
        self._on_inference_done = None
        self._on_automation_trigger = on_automation_trigger
        self._loop = None
        self._discovery_service = discovery_service

        # discovery 触发节流(保留原逻辑)
        self._last_discovery_at = 0.0
        self._discovery_min_interval = 20.0
        self._open_fail_count = 0
        self._discovery_trigger_threshold = 5
```

⚠️ 检查 `camera_stream.py` 顶部 import 是否有 `collections`(原 `_frame_buffer = collections.deque` 用到)。若原本直接 `deque` import,保留原样;本步不动 import 风格。

⚠️ **不删除原全局 config 读法**,只改 `__init__` 这一处入口。其它方法(`_worker`/`mjpeg_generator` 等)读的是 `self._*` 属性,会自动指向 config 值。

- [ ] **Step 2.4: 实现 — `CameraState` 加 camera_id**

`CameraState`(`camera_stream.py:30-49`)加字段:

```python
@dataclass
class CameraState:
    camera_id: str = ""
    action: str = "idle"
    # ... 其余字段不变
```

`get_state()`(`camera_stream.py:242` 附近,用 `asdict` 的地方)确保 `camera_id` 被填:在构造 `CameraState` 时传 `camera_id=self.camera_id`。

- [ ] **Step 2.5: 实现 — display 开关改名 + 回调签名**

```python
    def set_display_enabled(self, enabled: bool) -> None:
        """开关 /camera 弹窗的视觉展示推理(原 set_camera_vl_display_enabled)。

        关掉只停展示推理,dhash 运动检测与自动化触发不受影响。
        """
        self._camera_vl_display_enabled = bool(enabled)

    def set_on_automation_trigger(self, callback) -> None:
        """注册 dhash 运动触发回调。回调签名 callback(camera_id: str)。"""
        self._on_automation_trigger = callback
```

回调**调用点**:worker 内触发运动的地方(`camera_stream.py:758-762` 附近 `self._on_automation_trigger()`)改为:

```python
if self._on_automation_trigger is not None:
    self._on_automation_trigger(self.camera_id)
```

⚠️ 调用点若在多处,统一改成传 `self.camera_id`。

- [ ] **Step 2.6: 实现 — RTSP URL 解析从 config 读**

`_resolve_rtsp_url`(`camera_stream.py:486-506`)改为读 `self._*` 属性(不读 `get_config`):

```python
    def _resolve_rtsp_url(self) -> str:
        """从 config 字段拼完整 RTSP URL(含鉴权)。"""
        base = self._rtsp_url
        if not base:
            return ""
        user = self._rtsp_username
        pwd = self._rtsp_password
        if not user or not pwd:
            return base
        if "://" not in base:
            return base
        scheme, rest = base.split("://", 1)
        return f"{scheme}://{user}:{pwd}@{rest}"
```

- [ ] **Step 2.7: 实现 — worker 内 discovery 调用带 camera_id**

worker 掉线触发发现处(`camera_stream.py:614`):

```python
                                    fut = asyncio.run_coroutine_threadsafe(
                                        self._discovery_service.find_and_apply(self.camera_id),
                                        self._loop,
                                    )
```

(注:`find_and_apply` 签名在 Step 3 改为 `find_and_apply(camera_id)`。本步先传参,Step 3 实现匹配的签名。)

- [ ] **Step 2.8: 运行 CameraStream 测试,确认通过**

Run: `pytest tests/test_camera_stream.py -v`
Expected: PASS

- [ ] **Step 2.9: 写失败测试 — VisionService per-camera focuses**

追加到 `tests/test_camera_stream.py`(或新建 `tests/test_vision_service_focus.py`):

```python
from app.services.vision_service import VisionService


class TestPerCameraFocuses:
    def test_focuses_isolated_per_camera(self):
        vs = VisionService(client=MagicMock())
        vs.add_focus("人", camera_id="cam_a")
        vs.add_focus("车", camera_id="cam_b")
        a = vs.get_vision_focuses(camera_id="cam_a")
        b = vs.get_vision_focuses(camera_id="cam_b")
        assert len(a) == 1 and a[0]["text"] == "人"
        assert len(b) == 1 and b[0]["text"] == "车"
        # 删 a 的不影响 b
        vs.delete_focus(a[0]["id"], camera_id="cam_a")
        assert vs.get_vision_focuses(camera_id="cam_a") == []
        assert len(vs.get_vision_focuses(camera_id="cam_b")) == 1
```

- [ ] **Step 2.10: 实现 — VisionService per-camera focuses**

`vision_service.py:35` 改数据结构:

```python
        self._vision_focuses: dict[str, list[dict]] = {}  # camera_id → focuses
```

方法改造(`:64-93`),全部加 `camera_id` 参数:

```python
    def get_vision_focuses(self, camera_id: str = "") -> list[dict]:
        return list(self._vision_focuses.get(camera_id, []))

    def add_focus(self, text: str, camera_id: str = "") -> dict:
        focus = {"id": uuid.uuid4().hex[:8], "text": text, "enabled": True, "camera_id": camera_id}
        self._vision_focuses.setdefault(camera_id, []).append(focus)
        return focus

    def update_focus(self, focus_id: str, *, text=None, enabled=None, camera_id: str = "") -> dict | None:
        for f in self._vision_focuses.get(camera_id, []):
            if f["id"] == focus_id:
                if text is not None:
                    f["text"] = text
                if enabled is not None:
                    f["enabled"] = enabled
                return f
        return None

    def delete_focus(self, focus_id: str, camera_id: str = "") -> bool:
        bucket = self._vision_focuses.get(camera_id, [])
        before = len(bucket)
        self._vision_focuses[camera_id] = [f for f in bucket if f["id"] != focus_id]
        return len(self._vision_focuses[camera_id]) < before

    def load_focuses(self, focuses: list[dict]) -> None:
        """从 KV 加载(每条已含 camera_id)。按 camera_id 分桶。"""
        self._vision_focuses = {}
        for f in focuses:
            cid = f.get("camera_id", "")
            self._vision_focuses.setdefault(cid, []).append(f)
```

`_get_combined_focus`(`:95-100`)改为接收 `camera_id`:

```python
    def _get_combined_focus(self, camera_id: str = "") -> str:
        texts = [f["text"] for f in self._vision_focuses.get(camera_id, []) if f.get("enabled", True)]
        return "；".join(texts)
```

所有调用 `_get_combined_focus()` 的地方加传当前 `camera_id`。

- [ ] **Step 2.11: 运行 focus 测试**

Run: `pytest tests/test_camera_stream.py tests/test_vision_service_focus.py -v`
Expected: PASS

⚠️ **回归检查**:`grep -rn "_vision_focuses\|get_vision_focuses\|add_focus\|_get_combined_focus" app/` 找出所有调用点,确保都传了 camera_id(或保持默认空串,Step 5/8/9 收口)。

- [ ] **Step 2.12: Commit**

```bash
git add app/camera_stream.py app/services/vision_service.py tests/test_camera_stream.py
git commit -m "refactor(camera): CameraStream 参数化 + per-camera vision_focuses"
```

---

## Task 3: CameraDiscoveryService 多路化(`camera_id` 参数化)

**Files:**
- Modify: `app/services/camera_discovery_service.py`(`find_camera` `:217`;`apply_found_ip` `:293`;`capture_mac_on_startup` `:345`;`find_and_apply` `:385`;9 处 `get_config("vision.*")` `:84/241/248/254/262/327/369/371/378`;PTZ 8 处)
- Modify: `app/services/ptz_service.py`(`notify_ip_changed` `:120` — 加 camera_id 参数)
- Test: `tests/test_camera_discovery_service.py`(现有,改 ONVIF mock 模式)

**Interfaces:**
- Consumes: `db.cameras_get(camera_id)` / `db.cameras_update(camera_id, {fields})`(Step 1 产出);`PtzService` per-camera(Step 5 完整去单例化,本步先加 camera_id 到 notify)。
- Produces:`find_and_apply(camera_id) -> str | None`、`find_camera(camera_id)`、`apply_found_ip(camera_id, new_ip)`、`capture_mac_on_startup(camera_id)`。Step 2 worker 已调 `find_and_apply(self.camera_id)`;Step 4 manager 用 `capture_mac_on_startup(cam.id)`;Step 7 后台遍历所有路。

- [ ] **Step 3.1: 写失败测试 — 多路独立发现**

改 `tests/test_camera_discovery_service.py`。保留现有 `_make_cam` + `patch("onvif.ONVIFCamera")` mock 模式,把"改 config"改成"喂 cameras 表行 mock":

```python
@pytest.mark.asyncio
async def test_find_camera_uses_camera_row(monkeypatch):
    """find_camera(camera_id) 从 cameras 行读 MAC/子网/凭证,不读全局 config。"""
    svc = CameraDiscoveryService()
    # 模拟 db.cameras_get 返回该路配置
    cam_row = {
        "id": "cam_a", "device_mac": "60-a3-e3-de-e0-54", "ptz_ip": "192.168.4.16",
        "ptz_port": 80, "ptz_username": "admin", "ptz_password": "pwd",
        "rtsp_url": "rtsp://192.168.4.16/stream", "rtsp_username": "admin",
        "rtsp_password": "rp", "discovery_subnet": "192.168.4.0/24",
    }
    async def fake_get(cid):
        return cam_row if cid == "cam_a" else None
    monkeypatch.setattr(svc, "_db", MagicMock(cameras_get=fake_get))

    # mock 端口探测 + ONVIF probe
    monkeypatch.setattr(svc, "_scan_ports", AsyncMock(return_value=[("192.168.4.99", 80)]))
    monkeypatch.setattr(svc, "read_device_hardware_id", AsyncMock(return_value="60-a3-e3-de-e0-54"))

    new_ip = await svc.find_camera("cam_a")
    assert new_ip == "192.168.4.99"


@pytest.mark.asyncio
async def test_find_camera_isolated_per_camera(monkeypatch):
    """两路 device_mac 不同,各自命中不同 IP。"""
    svc = CameraDiscoveryService()
    rows = {
        "cam_a": {"id": "cam_a", "device_mac": "aa-aa-aa-aa-aa-aa", "ptz_ip": "10.0.0.1", "ptz_port": 80, "ptz_username": "u", "ptz_password": "p", "rtsp_url": "", "rtsp_username": "", "rtsp_password": "", "discovery_subnet": "10.0.0.0/24"},
        "cam_b": {"id": "cam_b", "device_mac": "bb-bb-bb-bb-bb-bb", "ptz_ip": "10.0.0.2", "ptz_port": 80, "ptz_username": "u", "ptz_password": "p", "rtsp_url": "", "rtsp_username": "", "rtsp_password": "", "discovery_subnet": "10.0.0.0/24"},
    }
    async def fake_get(cid):
        return rows.get(cid)
    monkeypatch.setattr(svc, "_db", MagicMock(cameras_get=fake_get))
    # cam_a 命中 .99(返回 cam_a 的 MAC),cam_b 命中 .100
    async def fake_read(ip, *a, **k):
        return "aa-aa-aa-aa-aa-aa" if ip == "10.0.0.99" else "bb-bb-bb-bb-bb-bb"
    monkeypatch.setattr(svc, "read_device_hardware_id", fake_read)
    monkeypatch.setattr(svc, "_scan_ports", AsyncMock(return_value=[("10.0.0.99", 80), ("10.0.0.100", 80)]))

    assert await svc.find_camera("cam_a") == "10.0.0.99"
    assert await svc.find_camera("cam_b") == "10.0.0.100"
```

- [ ] **Step 3.2: 运行,确认失败**

Run: `pytest tests/test_camera_discovery_service.py -v -k "uses_camera_row or isolated_per_camera"`
Expected: FAIL — `find_camera() takes 1 positional argument but 2 were given`(签名还是无参版)。

- [ ] **Step 3.3: 实现 — 方法加 camera_id + 从 cameras 行读**

`CameraDiscoveryService.__init__` 加 `db` 注入参数(从 bootstrap 拿到 Database 单例):

```python
    def __init__(self, db=None) -> None:
        self._db = db
        # ... 保留原 PTZ 单例引用(过渡期)
```

所有公共方法加 `camera_id` 第一参数,内部先 `row = await self._db.cameras_get(camera_id)`,再用 `row["device_mac"]`/`row["ptz_ip"]`/`row["discovery_subnet"]` 等替代原来的 `get_config("vision.*")`。

`find_camera(camera_id)` 骨架(替换 `:217-285`):

```python
    async def find_camera(self, camera_id: str) -> str | None:
        row = await self._db.cameras_get(camera_id)
        if not row or not row.get("discovery_enabled", 1):
            return None
        target_mac = self.normalize_mac(str(row.get("device_mac", "")))
        if not target_mac:
            return None
        subnet = str(row.get("discovery_subnet", "")) or self._infer_subnet(str(row.get("ptz_ip", "")))
        candidates = await self._scan_ports(subnet, [80, 554])
        for ip, port in candidates:
            hw = await self.read_device_hardware_id(
                ip, int(row.get("ptz_port", 80)),
                str(row.get("ptz_username", "")),
                str(row.get("ptz_password", "")),
            )
            if hw and self._mac_match(hw, target_mac):
                return ip
        return None
```

`apply_found_ip(camera_id, new_ip)`(`:293-319`)改写:更新**该路** `rtsp_url` host + `ptz_ip`,通知该路重连:

```python
    async def apply_found_ip(self, camera_id: str, new_ip: str) -> None:
        row = await self._db.cameras_get(camera_id)
        if not row:
            return
        # rtsp_url 替 host
        old_rtsp = str(row.get("rtsp_url", ""))
        new_rtsp = self._replace_host(old_rtsp, new_ip) if old_rtsp else old_rtsp
        fields = {"ptz_ip": new_ip}
        if new_rtsp:
            fields["rtsp_url"] = new_rtsp
        await self._db.cameras_update(camera_id, fields)
        # 通知该路 CameraStream + PtzService 重连(通过 manager 回调,见 Step 4)
        if self._on_ip_changed:
            self._on_ip_changed(camera_id, new_ip)
```

加 `_on_ip_changed` 回调注入:`__init__` 加 `self._on_ip_changed = None`,提供 `set_on_ip_changed(callback)`。manager 在 Step 4 注入,负责通知该路 stream 重连 + 该路 ptz 重连。

`capture_mac_on_startup(camera_id)`(`:345`)、`find_and_apply(camera_id)`(`:385`)同理加 camera_id 参数,从 `row` 读。

纯函数(`infer_subnet`/`normalize_mac`/`_mac_match`/`_scan_ports`/`_probe_candidate`)保持不变。加一个 `_replace_host` 辅助(若原文件无):

```python
    @staticmethod
    def _replace_host(url: str, new_host: str) -> str:
        """rtsp://[user:pwd@]old/path → 替换 old 为 new_host(保留凭证)。"""
        import re
        return re.sub(r"(://(?:[^@]+@)?)([^:/]+)", lambda m: f"{m.group(1)}{new_host}", url, count=1)
```

- [ ] **Step 3.4: 运行,确认通过**

Run: `pytest tests/test_camera_discovery_service.py -v -k "uses_camera_row or isolated_per_camera"`
Expected: PASS

- [ ] **Step 3.5: 回归 + 更新既有测试**

Run: `pytest tests/test_camera_discovery_service.py -v`
Expected: 全部 PASS。既有测试里直接改 config 的用例,改喂 `cameras_get` mock(与 Step 3.1 同模式)。

⚠️ `ptz_service.notify_ip_changed`(`:120`)本步加 `camera_id` 参数(过渡):`def notify_ip_changed(self, new_ip: str, camera_id: str = "") -> None`。完整 per-camera PTZ 在 Step 5 做。

- [ ] **Step 3.6: Commit**

```bash
git add app/services/camera_discovery_service.py app/services/ptz_service.py tests/test_camera_discovery_service.py
git commit -m "refactor(discovery): ONVIF 发现按 camera_id 参数化 + 多路独立 MAC 匹配"
```

---

## Task 4: CameraManager(生命周期 + 双通道并发调度)

**Files:**
- Create: `app/services/camera_manager.py`
- Test: `tests/test_camera_manager.py`(新建)

**Interfaces:**
- Consumes: `CameraStream(camera_id, config, vision_service, on_automation_trigger=..., discovery_service=...)`(Step 2);`db.cameras_all/cameras_insert/cameras_update/cameras_delete`(Step 1);`CameraDiscoveryService`(Step 3);`AutomationService.evaluate(frames, camera_id)`(Step 5);`VisionService` 推理入口。
- Produces:`CameraManager` 单例,供 Step 7 bootstrap/container/main 接线;`enable_display/disable_display`/`request_automation_eval`/`request_tool_inference`/`list_cameras`/`get_state(camera_id)`/`mjpeg_generator(camera_id)`/`get_recent_frames(camera_id, n)`。Step 8 工具用 `list_cameras()` + `get_frame(camera_id)`。

⚠️ **实施注意 — worker→manager 跨线程桥接**:CameraStream 的 `on_automation_trigger(camera_id)` 在 worker 线程同步调用,但 `request_automation_eval` 是 async + 要拿 Semaphore。沿用现有模式 `run_coroutine_threadsafe(coro, loop)`(与 `camera_stream.py:303` 推理投递同机制)。manager 须注入 loop。

- [ ] **Step 4.1: 写失败测试 — 双通道并发上限**

新建 `tests/test_camera_manager.py`:

```python
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from app.services.camera_manager import CameraManager


def _make_stream(camera_id, online=True):
    """Mock CameraStream。"""
    s = MagicMock()
    s.camera_id = camera_id
    s.get_recent_frames = MagicMock(return_value=[b"frame"])
    s.get_latest_frame = MagicMock(return_value=b"frame")
    s.start = MagicMock()
    s.stop = MagicMock()
    s.set_event_loop = MagicMock()
    s.set_discovery_service = MagicMock()
    s.set_on_automation_trigger = MagicMock()
    s.start_display = MagicMock()
    s.stop_display = MagicMock()
    s.get_state = MagicMock(return_value={"camera_id": camera_id, "online": online})
    s.mjpeg_generator = MagicMock(return_value=iter([b"x"]))
    return s


class TestConcurrencyLimits:
    @pytest.mark.asyncio
    async def test_automation_channel_caps_at_5(self):
        """自动化通道上限 5:第 6 个排队等。"""
        mgr = CameraManager.__new__(CameraManager)
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._display_sem = asyncio.Semaphore(1)
        mgr._streams = {}
        mgr._db = MagicMock()
        mgr._loop = asyncio.get_event_loop()

        in_flight = 0
        peak = 0

        async def fake_eval(cid, frames):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1

        mgr._eval_one = fake_eval  # 绕过真实评估,直接测信号量

        # 同时发 8 个自动化请求
        tasks = [asyncio.create_task(mgr.request_automation_eval(f"cam_{i}", [b"f"])) for i in range(8)]
        await asyncio.gather(*tasks)
        assert peak == 5  # 峰值不超过上限

    @pytest.mark.asyncio
    async def test_display_channel_caps_at_1(self):
        """展示通道上限 1:两路 enable 时旧路先停。"""
        mgr = CameraManager.__new__(CameraManager)
        mgr._display_sem = asyncio.Semaphore(1)
        mgr._auto_sem = asyncio.Semaphore(5)
        mgr._streams = {"cam_a": _make_stream("cam_a"), "cam_b": _make_stream("cam_b")}
        mgr._active_display_id = None

        await mgr.enable_display("cam_a")
        assert mgr._active_display_id == "cam_a"
        await mgr.enable_display("cam_b")
        assert mgr._active_display_id == "cam_b"
        # 旧路 cam_a 停了展示
        mgr._streams["cam_a"].stop_display.assert_called_once()
        mgr._streams["cam_b"].start_display.assert_called_once()


class TestListCameras:
    @pytest.mark.asyncio
    async def test_list_cameras_returns_camera_info(self):
        mgr = CameraManager.__new__(CameraManager)
        mgr._streams = {"cam_a": _make_stream("cam_a"), "cam_b": _make_stream("cam_b", online=False)}
        mgr._db = MagicMock()
        mgr._db.cameras_all = AsyncMock(return_value=[
            {"id": "cam_a", "name": "客厅", "area": "客厅", "enabled": 1},
            {"id": "cam_b", "name": "门口", "area": "玄关", "enabled": 1},
        ])
        cams = mgr.list_cameras()
        ids = {c["id"] for c in cams}
        assert ids == {"cam_a", "cam_b"}
        # 供工具注入应含 name/area/online
        a = next(c for c in cams if c["id"] == "cam_a")
        assert a["name"] == "客厅" and a["area"] == "客厅" and a["online"] is True
```

- [ ] **Step 4.2: 运行,确认失败**

Run: `pytest tests/test_camera_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.camera_manager'`。

- [ ] **Step 4.3: 实现 — CameraManager**

新建 `app/services/camera_manager.py`:

```python
"""多路摄像头生命周期管理 + 双通道 VLM 并发调度。

双通道(Spec §2.3):
- 展示通道(上限 1):/camera 弹窗当前看的那路持续跑,切换旧停新启。
- 自动化+工具通道(上限 5):运动触发的自动化评估 + 聊天工具调用共享,
  超过上限排队。峰值 1+5=6 < glm-4v 上限 10。

为什么展示通道独立:用户看画面时若 5 个自动化名额占满,展示推理会排队卡顿。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from ..camera_stream import CameraStream

logger = logging.getLogger(__name__)


class CameraManager:
    def __init__(self, vision_service=None, motion_service=None,
                 ha_service=None, db=None, discovery_service=None,
                 automation_service=None,
                 display_concurrency: int = 1, auto_concurrency: int = 5) -> None:
        self._vision_service = vision_service
        self._motion_service = motion_service
        self._ha_service = ha_service
        self._db = db
        self._discovery_service = discovery_service
        self._automation_service = automation_service
        self._streams: dict[str, CameraStream] = {}
        self._active_display_id: str | None = None
        self._display_sem = asyncio.Semaphore(display_concurrency)
        self._auto_sem = asyncio.Semaphore(auto_concurrency)
        self._loop: asyncio.AbstractEventLoop | None = None
        # discovery 回 IP 时通知该路重连
        if discovery_service is not None:
            discovery_service.set_on_ip_changed(self._on_camera_ip_changed)

    # —— 生命周期 ——
    async def initialize(self) -> None:
        """启动时从 DB 加载所有 enabled 摄像头并各自 start。"""
        rows = await self._db.cameras_all()
        for row in rows:
            if not row.get("enabled", 1):
                continue
            await self._spawn(row)

    async def _spawn(self, row: dict) -> CameraStream:
        """根据 cameras 行构造一路并启动。"""
        cid = row["id"]
        stream = CameraStream(
            camera_id=cid, config=row, vision_service=self._vision_service,
            on_automation_trigger=self._on_automation_trigger,
            discovery_service=self._discovery_service,
        )
        if self._loop is not None:
            stream.set_event_loop(self._loop)
        if self._discovery_service is not None:
            stream.set_discovery_service(self._discovery_service)
        stream.set_on_automation_trigger(self._on_automation_trigger)
        self._streams[cid] = stream
        stream.start()
        return stream

    def set_event_loop(self, loop) -> None:
        self._loop = loop
        for s in self._streams.values():
            s.set_event_loop(loop)

    def stop(self) -> None:
        for s in self._streams.values():
            try:
                s.stop()
            except Exception:
                logger.exception("stop camera %s failed", getattr(s, "camera_id", "?"))

    # —— CRUD(转发 DB + 增删 stream)——
    async def create_camera(self, data: dict) -> dict:
        import secrets
        data.setdefault("id", f"cam_{secrets.token_hex(3)}")
        await self._db.cameras_insert(data)
        if data.get("enabled", 1):
            await self._spawn(data)
        return data

    async def update_camera(self, camera_id: str, fields: dict) -> dict:
        await self._db.cameras_update(camera_id, fields)
        # 简单策略:重建该路(参数变了)
        old = self._streams.pop(camera_id, None)
        if old:
            try:
                old.stop()
            except Exception:
                logger.exception("stop old stream %s failed", camera_id)
        row = await self._db.cameras_get(camera_id)
        if row and row.get("enabled", 1):
            await self._spawn(row)
        return row

    async def delete_camera(self, camera_id: str) -> bool:
        old = self._streams.pop(camera_id, None)
        if old:
            try:
                old.stop()
            except Exception:
                logger.exception("stop stream %s on delete failed", camera_id)
        if self._active_display_id == camera_id:
            self._active_display_id = None
        return await self._db.cameras_delete(camera_id)

    # —— 展示通道 ——
    async def enable_display(self, camera_id: str) -> None:
        """切换展示推理到指定路:旧路停,新路起。"""
        if self._active_display_id == camera_id:
            return
        old = self._streams.get(self._active_display_id) if self._active_display_id else None
        if old is not None:
            old.stop_display()
        new = self._streams.get(camera_id)
        if new is not None:
            new.start_display()
        self._active_display_id = camera_id

    async def disable_display(self, camera_id: str) -> None:
        if self._active_display_id == camera_id:
            s = self._streams.get(camera_id)
            if s is not None:
                s.stop_display()
            self._active_display_id = None

    # —— 帧访问 ——
    def get_frame(self, camera_id: str) -> Any:
        s = self._streams.get(camera_id)
        return s.get_latest_frame() if s else None

    def get_recent_frames(self, camera_id: str, n: int = 3) -> list:
        s = self._streams.get(camera_id)
        return s.get_recent_frames(n) if s else []

    def mjpeg_generator(self, camera_id: str):
        s = self._streams.get(camera_id)
        if s is None:
            return iter([])
        return s.mjpeg_generator()

    def get_state(self, camera_id: str) -> dict:
        s = self._streams.get(camera_id)
        if s is None:
            return {"camera_id": camera_id, "online": False}
        return s.get_state()

    def list_cameras(self) -> list[dict]:
        """供工具注入:含 id/name/area/online。"""
        out = []
        for cid, s in self._streams.items():
            st = s.get_state()
            out.append({
                "id": cid, "name": getattr(s, "_config", {}).get("name", cid),
                "area": getattr(s, "_config", {}).get("area", ""),
                "online": bool(st.get("online", False)),
            })
        return out

    # —— 自动化/工具通道 ——
    def _on_automation_trigger(self, camera_id: str) -> None:
        """worker 线程回调:投递自动化评估到主循环。"""
        if self._loop is None or self._loop.is_closed():
            return
        frames = self.get_recent_frames(camera_id, 3)
        asyncio.run_coroutine_threadsafe(
            self.request_automation_eval(camera_id, frames), self._loop
        )

    async def request_automation_eval(self, camera_id: str, frames: list) -> None:
        """运动触发:拿自动化通道名额 → 跑该路规则评估。"""
        async with self._auto_sem:
            await self._eval_one(camera_id, frames)

    async def _eval_one(self, camera_id: str, frames: list) -> None:
        if self._automation_service is None:
            return
        try:
            await self._automation_service.evaluate(frames=frames, camera_id=camera_id)
        except Exception:
            logger.exception("automation eval failed for %s", camera_id)

    async def request_tool_inference(self, camera_id: str, prompt: str, frames: list) -> Any:
        """工具调用:共享自动化通道。"""
        async with self._auto_sem:
            if self._vision_service is None:
                return None
            return await self._vision_service.evaluate_condition(frames, prompt)

    def _on_camera_ip_changed(self, camera_id: str, new_ip: str) -> None:
        """discovery 回 IP:通知该路 stream + ptz 重连。"""
        s = self._streams.get(camera_id)
        if s is not None and hasattr(s, "notify_ip_changed"):
            try:
                s.notify_ip_changed(new_ip)
            except Exception:
                logger.exception("stream %s reconnect failed", camera_id)
        # ptz per-camera 重连由 Step 5 的 ptz 注册表处理
```

⚠️ CameraStream 需补 `start_display()`/`stop_display()` 两个薄封装(在 Step 2.5 的 `set_display_enabled` 基础上):`start_display()` = `self.set_display_enabled(True)`、`stop_display()` = `self.set_display_enabled(False)`。在 Step 2 漏了的话在本步补到 camera_stream.py。

- [ ] **Step 4.4: 运行,确认通过**

Run: `pytest tests/test_camera_manager.py -v`
Expected: PASS

- [ ] **Step 4.5: Commit**

```bash
git add app/services/camera_manager.py tests/test_camera_manager.py app/camera_stream.py
git commit -m "feat(camera): CameraManager 双通道并发调度(展示1+自动化5)+ 生命周期管理"
```

---

## Task 5: 服务适配 camera_id(PTZ 去单例化 + Automation/Rule 透传)

**Files:**
- Modify: `app/services/ptz_service.py`(单例 `ptz_service = PtzService()` `:214` → 去单例化 + per-camera 注册表)
- Modify: `app/services/automation_service.py`(`evaluate` `:35` 加 camera_id;规则分区 `:66-92` 按摄像头过滤)
- Modify: `app/services/rule_registry_service.py`(`AutomationRule` `:18-53` 加 camera_id;`load_from_db` `:85-118`;`add_rule` `:157-179`)
- Modify: `app/services/rule_service.py`(`build_rule` `:165`;`_fallback_rule` `:288`;`setdefault` `:257-263`)
- Test: `tests/test_ptz_service.py`、`tests/test_automation_service.py`(现有,改)

**Interfaces:**
- Consumes: Step 1 `rules.camera_id` 列;Step 4 manager 回调。
- Produces:`AutomationService.evaluate(frames, camera_id="")`;`AutomationRule.camera_id`;per-camera `PtzService` 注册表 `PtzRegistry`。Step 6/7/9 依赖。

- [ ] **Step 5.1: 写失败测试 — automation_service 按摄像头过滤规则**

追加到 `tests/test_automation_service.py`:

```python
@pytest.mark.asyncio
async def test_evaluate_filters_rules_by_camera():
    """evaluate(frames, camera_id) 只评估该摄像头的规则。"""
    reg = MagicMock()
    reg.list_rules.return_value = [
        {"id": "r1", "camera_id": "cam_a", "enabled": True, "type": "time",
         "condition": "每天8点", "actions": [], "cooldown_seconds": 5, "last_triggered_at": 0},
        {"id": "r2", "camera_id": "cam_b", "enabled": True, "type": "time",
         "condition": "每天9点", "actions": [], "cooldown_seconds": 5, "last_triggered_at": 0},
        {"id": "r3", "camera_id": "", "enabled": True, "type": "time",   # 未绑定 → 归默认
         "condition": "每天10点", "actions": [], "cooldown_seconds": 5, "last_triggered_at": 0},
    ]
    svc = AutomationService(reg, tool_executor=MagicMock(), vision_service=MagicMock(), ha_service=None)
    # 设备门控:无 ha_service 时跳过门控;时间规则走 chat LLM
    svc._llm = MagicMock()
    # 让 context_only 评估不真正调外部:mock 掉
    with patch.object(svc, "_evaluate_context_only", new=AsyncMock(return_value=[])):
        await svc.evaluate(frames=None, camera_id="cam_a")
    # 只看到 cam_a + 未绑定(cam_b 被过滤掉)
    seen = [call.args[0]["id"] for call in svc._evaluate_context_only.call_args_list]
    assert "r1" in seen and "r3" in seen and "r2" not in seen
```

- [ ] **Step 5.2: 运行,确认失败**

Run: `pytest tests/test_automation_service.py::test_evaluate_filters_rules_by_camera -v`
Expected: FAIL — `evaluate() got an unexpected keyword argument 'camera_id'`。

- [ ] **Step 5.3: 实现 — automation_service 加 camera_id**

`evaluate` 签名(`:35`):

```python
    async def evaluate(self, frames: list | None = None, camera_id: str = "") -> list[dict]:
```

在规则分区循环(`:66-92`)里,过滤掉不属于该摄像头的规则。未绑定(camera_id 为空)的规则归到**所有**摄像头(向后兼容):

```python
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            if self._in_cooldown(rule, now):
                continue
            # 按摄像头过滤:规则的 camera_id 非空时必须匹配;空表示未绑定,归所有路
            rule_cam = str(rule.get("camera_id", ""))
            if rule_cam and camera_id and rule_cam != camera_id:
                continue
            # ... 后续分区逻辑不变
```

- [ ] **Step 5.4: 实现 — AutomationRule + Registry 加 camera_id**

`AutomationRule`(`rule_registry_service.py:18-53`)加字段:

```python
    camera_id: str = ""
```

`to_dict`(`:36-53`)加 `"camera_id": self.camera_id`。

`load_from_db`(`:85-118`)重建时:`camera_id=item.get("camera_id", "")`。

`add_rule`(`:157-179`):透传 rule dict 里的 camera_id(`item.get("camera_id","")`)。

- [ ] **Step 5.5: 实现 — rule_service.build_rule 带 camera_id**

`build_rule`(`rule_service.py:165`)签名加 `camera_id: str = ""`;`_fallback_rule`(`:288`)和 setdefault 块(`:257-263`)都补 `camera_id`:

```python
    parsed.setdefault("camera_id", camera_id)
```

⚠️ 保留 `from __future__ import annotations`(`:1`)——文件里 `:38/:48` 的 `Awaitable` 注解依赖它不被求值(见 explore 报告)。

- [ ] **Step 5.6: 实现 — PtzService 去单例化 + per-camera 注册表**

⚠️ **实施注意**:ptz_service 是模块级单例(`ptz_service.py:214`),`ptz_routes.py:17` 直接 import。改 per-camera 需一个注册表:

在 `ptz_service.py` 末尾加:

```python
class PtzRegistry:
    """按 camera_id 管理 PtzService 实例。每路独立连接态。"""
    def __init__(self) -> None:
        self._by_cam: dict[str, "PtzService"] = {}
        self._lock = asyncio.Lock()

    async def get(self, camera_id: str, config: dict) -> "PtzService":
        async with self._lock:
            svc = self._by_cam.get(camera_id)
            if svc is None:
                svc = PtzService(camera_id=camera_id, config=config)
                self._by_cam[camera_id] = svc
            return svc

    def notify_ip_changed(self, camera_id: str, new_ip: str) -> None:
        svc = self._by_cam.get(camera_id)
        if svc is not None:
            svc.notify_ip_changed(new_ip, camera_id=camera_id)

ptz_registry = PtzRegistry()
```

`PtzService.__init__`(`:62`)改为接收 `camera_id` + `config`,内部从 config 读 `ptz.*`(`ptz_enabled`/`ptz_ip`/...)而非 `get_config("ptz.*")`。`move`/`stop`/`step` 不变(它们读 `self._*` 属性,会指向 per-camera 配置)。

⚠️ **过渡策略**:删模块级 `ptz_service = PtzService()`(`:214`)会破坏 `ptz_routes.py` import。Step 6 把 ptz_routes 合并进 camera_routes 时统一切到 `ptz_registry`。本步保留 `ptz_service` 但标 `# deprecated`。

- [ ] **Step 5.7: 运行 + 回归**

Run: `pytest tests/test_automation_service.py tests/test_ptz_service.py tests/test_rule_service.py -v`
Expected: PASS。`test_ptz_service.py` 现有测试改用 `PtzService(camera_id="cam_test", config={...})` 构造(ONVIF mock 模式不变)。

- [ ] **Step 5.8: Commit**

```bash
git add app/services/automation_service.py app/services/rule_registry_service.py app/services/rule_service.py app/services/ptz_service.py tests/
git commit -m "refactor: automation/rule/ptz 按 camera_id 参数化 — 规则按摄像头过滤 + PtzRegistry"
```

---

## Task 6: camera_routes.py(合并 PTZ + Discovery + Focus + 状态)

**Files:**
- Create: `app/routes/camera_routes.py`
- Modify: `app/main.py`(路由注册 `:583-605`)
- Modify: `app/routes/settings_routes.py`(移除 vision-focus 6 个 handler `:717-789`)
- Modify: `app/routes/rule_routes.py`(加 `?camera_id=` 过滤)
- Delete: `app/routes/ptz_routes.py`、`app/routes/discovery_routes.py`(本步末)
- Test: `tests/test_camera_routes.py`(新建);改 `tests/test_discovery_routes.py`、`tests/test_ptz_config.py`(重定向到 camera_routes)

**Interfaces:**
- Consumes: Step 4 manager;Step 3 discovery;Step 5 ptz_registry;Step 2 vision_service focus API。
- Produces:`/api/cameras` 全套 REST。Step 11 前端调这些。

⚠️ **实施注意 — areas 端点缺口**:spec §7.1 区域下拉写「`← HA /api/areas 下拉`」,但 Aether 当前**无** areas 端点(`ha_routes.py` grep "area" 为空,`ha_service.py:68` 仅内部读 area_registry)。本步补一个 `GET /api/ha/areas`,否则区域下拉永远空。

- [ ] **Step 6.1: 写失败测试 — cameras CRUD + areas 端点**

新建 `tests/test_camera_routes.py`,沿用 `tests/test_routes_extra.py:_mock_container`(`:17`)模式:

```python
import pytest
from unittest.mock import MagicMock, AsyncMock
from fastapi.testclient import TestClient
from app.routes import camera_routes


def _mock_container():
    c = MagicMock()
    c.camera_manager = MagicMock()
    c.camera_manager.create_camera = AsyncMock(return_value={"id": "cam_new", "name": "x"})
    c.camera_manager.update_camera = AsyncMock(return_value={"id": "cam_new"})
    c.camera_manager.delete_camera = AsyncMock(return_value=True)
    c.camera_manager.list_cameras = MagicMock(return_value=[{"id": "cam_a", "name": "客厅", "area": "客厅", "online": True}])
    c.camera_manager.get_state = MagicMock(return_value={"camera_id": "cam_a", "online": True})
    c.vision_service = MagicMock()
    c.ha_service = MagicMock()
    c.ha_service.get_areas = AsyncMock(return_value=[{"area_id": "keting", "name": "客厅"}])
    c.db = MagicMock()
    return c


@pytest.fixture
def client(monkeypatch):
    cont = _mock_container()
    monkeypatch.setattr(camera_routes, "get_container", lambda: cont)
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(camera_routes.router, prefix="/api")
    return TestClient(app), cont


class TestCamerasCrud:
    def test_list_cameras(self, client):
        c, _ = client
        r = c.get("/api/cameras")
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1 and data[0]["id"] == "cam_a"

    def test_create_camera(self, client):
        c, cont = client
        r = c.post("/api/cameras", json={"name": "新摄像头", "source_type": "rtsp", "rtsp_url": "rtsp://x"})
        assert r.status_code == 200
        cont.camera_manager.create_camera.assert_called_once()

    def test_delete_camera(self, client):
        c, cont = client
        r = c.delete("/api/cameras/cam_a")
        assert r.status_code == 200
        cont.camera_manager.delete_camera.assert_called_once_with("cam_a")


class TestAreasEndpoint:
    def test_get_areas(self, client):
        c, cont = client
        r = c.get("/api/ha/areas")
        assert r.status_code == 200
        assert r.json()["data"][0]["name"] == "客厅"
```

- [ ] **Step 6.2: 运行,确认失败**

Run: `pytest tests/test_camera_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: app.routes.camera_routes`。

- [ ] **Step 6.3: 实现 — camera_routes.py**

新建 `app/routes/camera_routes.py`,路由按 spec §6.3。骨架:

```python
"""摄像头管理路由:吸收原 ptz_routes / discovery_routes / vision-focus / 状态端点。"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from app.schema.api_schemas import ApiResponse
from app.container import get_container

router = APIRouter()


@router.get("/cameras")
async def list_cameras():
    c = get_container()
    return ApiResponse(data=c.camera_manager.list_cameras())


@router.post("/cameras")
async def create_camera(body: dict):
    c = get_container()
    created = await c.camera_manager.create_camera(body)
    return ApiResponse(data=created)


@router.get("/cameras/{camera_id}")
async def get_camera(camera_id: str):
    c = get_container()
    st = c.camera_manager.get_state(camera_id)
    row = await c.db.cameras_get(camera_id)
    if row is None:
        raise HTTPException(404, "摄像头不存在")
    return ApiResponse(data={**row, "state": st})


@router.put("/cameras/{camera_id}")
async def update_camera(camera_id: str, body: dict):
    c = get_container()
    updated = await c.camera_manager.update_camera(camera_id, body)
    return ApiResponse(data=updated)


@router.delete("/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    c = get_container()
    ok = await c.camera_manager.delete_camera(camera_id)
    return ApiResponse(data={"deleted": ok}))


@router.get("/cameras/{camera_id}/video_feed")
async def video_feed(camera_id: str):
    c = get_container()
    return StreamingResponse(
        c.camera_manager.mjpeg_generator(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.post("/cameras/{camera_id}/display/enable")
async def enable_display(camera_id: str):
    c = get_container()
    await c.camera_manager.enable_display(camera_id)
    return ApiResponse(data={"ok": True}))


@router.post("/cameras/{camera_id}/display/disable")
async def disable_display(camera_id: str):
    c = get_container()
    await c.camera_manager.disable_display(camera_id)
    return ApiResponse(data={"ok": True}))


@router.get("/cameras/{camera_id}/state")
async def camera_state(camera_id: str):
    c = get_container()
    return ApiResponse(data=c.camera_manager.get_state(camera_id))


# —— PTZ(从 ptz_routes 迁入)——
@router.post("/cameras/{camera_id}/ptz/move")
async def ptz_move(camera_id: str, body: dict):
    c = get_container()
    row = await c.db.cameras_get(camera_id)
    svc = await c.ptz_registry.get(camera_id, row)
    return ApiResponse(data=await svc.move(body.get("direction", "")))


@router.post("/cameras/{camera_id}/ptz/stop")
async def ptz_stop(camera_id: str):
    c = get_container()
    row = await c.db.cameras_get(camera_id)
    svc = await c.ptz_registry.get(camera_id, row)
    return ApiResponse(data=await svc.stop())


@router.post("/cameras/{camera_id}/ptz/step")
async def ptz_step(camera_id: str, body: dict):
    c = get_container()
    row = await c.db.cameras_get(camera_id)
    svc = await c.ptz_registry.get(camera_id, row)
    return ApiResponse(data=await svc.step(body.get("direction", ""), int(body.get("duration_ms", 300))))


# —— ONVIF 发现(从 discovery_routes 迁入)——
@router.post("/cameras/{camera_id}/discovery/find")
async def discovery_find(camera_id: str):
    c = get_container()
    new_ip = await c.discovery_service.find_and_apply(camera_id)
    return ApiResponse(data={"new_ip": new_ip}))


@router.post("/cameras/{camera_id}/discovery/manual-ip")
async def discovery_manual_ip(camera_id: str, body: dict):
    c = get_container()
    await c.discovery_service.apply_found_ip(camera_id, body.get("ip", ""))
    return ApiResponse(data={"ok": True}))


# —— 视觉关注项(从 settings_routes 迁入)——
@router.get("/cameras/{camera_id}/focuses")
async def list_focuses(camera_id: str):
    c = get_container()
    return ApiResponse(data=c.vision_service.get_vision_focuses(camera_id))


@router.post("/cameras/{camera_id}/focuses")
async def add_focus(camera_id: str, body: dict):
    c = get_container()
    return ApiResponse(data=c.vision_service.add_focus(body.get("text", ""), camera_id=camera_id))


@router.put("/cameras/{camera_id}/focuses/{focus_id}")
async def update_focus(camera_id: str, focus_id: str, body: dict):
    c = get_container()
    return ApiResponse(data=c.vision_service.update_focus(
        focus_id, text=body.get("text"), enabled=body.get("enabled"), camera_id=camera_id))


@router.delete("/cameras/{camera_id}/focuses/{focus_id}")
async def delete_focus(camera_id: str, focus_id: str):
    c = get_container()
    return ApiResponse(data={"deleted": c.vision_service.delete_focus(focus_id, camera_id=camera_id)}))


# —— HA areas(补 spec §7.1 区域下拉所需,当前缺)——
@router.get("/ha/areas")
async def list_areas():
    c = get_container()
    return ApiResponse(data=await c.ha_service.get_areas())
```

⚠️ 检查 `get_container` 的实际签名(可能是函数或 Depends)。对齐 `settings_routes.py` 的用法。若 `ApiResponse` 是泛型,空 body 用 `ApiResponse(data={...})` 不带泛型参数。

⚠️ `ha_service` 需补 `get_areas()` 方法(转发 WebSocket area_registry,`ha_service.py:68` 已有内部读取,加个 public 方法)。补到 `ha_service.py`:

```python
    async def get_areas(self) -> list[dict]:
        """对外暴露 HA 区域列表(供摄像头管理页区域下拉)。"""
        # 复用 _get_area_maps_cached 的 area_id→name
        area_map = await self._get_area_maps_cached()
        return [{"area_id": aid, "name": name} for aid, name in area_map.items()]
```

- [ ] **Step 6.4: 实现 — main.py 注册 + 迁移 vision-focus**

`main.py` 路由注册区(`:583-605`):
- 删 `from .routes.ptz_routes import router as ptz_router` + `app.include_router(ptz_router, ...)`
- 删 `from .routes.discovery_routes import router as discovery_router` + 注册行
- 加 `from .routes.camera_routes import router as camera_router` + `app.include_router(camera_router, prefix="/api")`

从 `settings_routes.py:717-789` 物理移除 6 个 vision-focus handler(迁到 camera_routes 已完成)。导入镜像(Depends/get_container/Database/Vision schema)随之清理。

- [ ] **Step 6.5: 重定向旧路由测试**

`tests/test_discovery_routes.py`、`tests/test_ptz_config.py`:`from app.routes import X` + `patch.object(X, ...)` 改向 `camera_routes`;端点路径从 `/api/discovery/*`、`/api/ptz/*` 改为 `/api/cameras/{id}/discovery/*`、`/api/cameras/{id}/ptz/*`。服务级测试(`test_camera_discovery_service.py`/`test_ptz_service.py`)不动。

- [ ] **Step 6.6: 运行 + 删除旧路由文件**

Run: `pytest tests/test_camera_routes.py tests/test_discovery_routes.py tests/test_ptz_config.py -v`
Expected: PASS

确认 `grep -rn "ptz_routes\|discovery_routes" app/` 除 import 删除点外无残留后:

```bash
git rm app/routes/ptz_routes.py app/routes/discovery_routes.py
```

- [ ] **Step 6.7: Commit**

```bash
git add app/routes/camera_routes.py app/main.py app/routes/settings_routes.py app/routes/rule_routes.py app/services/ha_service.py tests/
git commit -m "feat(routes): camera_routes 合并 ptz/discovery/focus + 补 /api/ha/areas;删除旧 ptz/discovery 路由"
```

---

## Task 7: bootstrap / container / main 改造 + 后台 MAC 捕获

**Files:**
- Modify: `app/container.py`(`camera_stream` 字段 `:47` → `camera_manager`;`init_container` `:127`)
- Modify: `app/bootstrap.py`(`CameraStream(...)` `:45` → `CameraManager(...)`;discovery 注入 `:133`)
- Modify: `app/main.py`(lifespan camera 接线 `:500-514`;`camera_stream` 引用 `:430/452/470/498-507/554`;后台 MAC 捕获 `:511`)
- Modify: `tests/test_routes_extra.py`(`_mock_container` `:25` `c.camera_stream` → `c.camera_manager`)
- Modify: `app/routes/automation_routes.py`(全局滑块 `:73-125` → per-camera)

**Interfaces:**
- Consumes: Step 4 CameraManager;Step 1 db;Step 3 discovery。
- Produces:运行时 `camera_manager` 单例注入 container;lifespan 启动/停止;后台 MAC 捕获遍历所有路。

- [ ] **Step 7.1: 写失败测试 — container 字段 + bootstrap 构造**

`tests/test_container.py`(若无则新建)/或加到 `test_routes_extra.py`:

```python
def test_container_has_camera_manager_field():
    from app.container import AppContainer
    import dataclasses
    fields = {f.name for f in dataclasses.fields(AppContainer)}
    assert "camera_manager" in fields
    assert "camera_stream" not in fields   # 旧字段已移除
```

- [ ] **Step 7.2: 实现 — container 字段替换**

`container.py:47` `camera_stream: Any` → `camera_manager: Any`。`init_container`(`:127`)对应改 `camera_manager=services["camera_manager"]`。

- [ ] **Step 7.3: 实现 — bootstrap 构造 CameraManager**

`bootstrap.py:45-46` 替换:

```python
    camera_manager = CameraManager(
        vision_service=vision_service, motion_service=None,
        ha_service=None, db=None, discovery_service=discovery_service,
        automation_service=None,  # 稍后注入(automation_service 在 :90 才构造)
    )
```

⚠️ 顺序问题:`automation_service` 在 `:90` 才构造,但 `CameraManager` 在 `:45` 构造。两种方案:
- 方案A:把 CameraManager 构造移到 `:90` 之后(`automation_service` 构造后)。
- 方案B:CameraManager 先构造,`automation_service` 构造后用 setter 注入 `camera_manager.set_automation_service(automation_service)`。

选**方案B**(改动小):CameraManager 加 `set_automation_service(svc)` + `set_db(db)` + `set_ha_service(svc)` setter,在 `bootstrap.py` 后续构造完对应服务后调用。

`services["camera_manager"] = camera_manager` 替代 `services["camera_stream"] = ...`。`discovery_service` 在 `:133` 注入 `services` 字典,但 CameraManager 构造时已传入(确保 `discovery_service` 在 `:45` 之前可用——它是 import 级单例 `camera_discovery_service.py:397`,所以可用)。

- [ ] **Step 7.4: 实现 — main.py lifespan camera 接线**

替换 `:498-509` 的 camera_stream 接线块为:

```python
        # —— 多路摄像头初始化 ——
        camera_manager = _services.get("camera_manager")
        camera_manager.set_event_loop(asyncio.get_event_loop())
        camera_manager.set_db(db)                          # Database 已 init(:275)
        camera_manager.set_automation_service(automation_service)
        camera_manager.set_ha_service(ha_service)
        await camera_manager.initialize()                  # 内部遍历各路 start
```

替换 `:511-517` 后台 MAC 捕获为遍历所有路:

```python
        # 后台并发捕获所有 discovery_enabled 且 device_mac 为空的路
        async def _capture_macs():
            for row in await db.cameras_all():
                if row.get("discovery_enabled", 1) and not row.get("device_mac"):
                    try:
                        await discovery_service.capture_mac_on_startup(row["id"])
                    except Exception:
                        logger.exception("MAC capture failed for %s", row.get("id"))
        asyncio.create_task(_capture_macs())
```

shutdown 段(`:547-561`):`camera_stream.stop()` → `camera_manager.stop()`。

⚠️ `_services["camera_stream"]` 全局引用(`main.py:94`)→ `_services["camera_manager"]`。所有 `camera_stream` 引用点(`:430/452/470`)改用 `camera_manager`,其中传给 `Dispatcher`(`:454`)/`AutomationAgent`(`:471`)/`ToolDeps`(`:433`)的对象改为 manager(这些类内部调用点在 Step 9 收口)。

- [ ] **Step 7.5: 实现 — automation_routes 全局滑块 per-camera**

`automation_routes.py:73-125` 的 dhash/vision-recognizer 滑块改为 per-camera:加 `camera_id` 路径参数,调 `camera_manager` 对应路。

- [ ] **Step 7.6: 运行回归**

Run: `pytest tests/ -v -k "container or routes_extra or http_smoke"`
Expected: PASS

⚠️ `test_http_smoke.py:46` 验证 health 路由可达(Step 10 保证 health 仍返回主摄像头状态)。

- [ ] **Step 7.7: Commit**

```bash
git add app/container.py app/bootstrap.py app/main.py app/routes/automation_routes.py tests/
git commit -m "refactor(startup): bootstrap/container/main 接入 CameraManager + 后台多路 MAC 捕获"
```

---

## Task 8: tools.py vision 工具适配 + 摄像头列表注入

**Files:**
- Modify: `app/tools.py`(`vision_chat` `:66-81`;`verify_condition` `:215-242`;`ToolDeps` `:26-41`)
- Modify: `app/mcp/local_mcp_servers.py`(`create_verify_condition_handler` `:119`;`get_latest_frame` `:161`)
- Test: `tests/test_local_mcp_servers.py`(`:85/121` mock 更新)

**Interfaces:**
- Consumes: Step 4 manager(`get_frame(camera_id)` + `list_cameras()`)。
- Produces:`vision_chat`/`verify_condition` 加可选 `camera_id` 参数 + 三级 fallback;工具描述动态注入摄像头列表。

⚠️ **实施注意 — 工具描述静态绑定坑**:工具描述在 `build_chat_agent` 时静态绑定,摄像头增删后描述不会自动更新。两种方案:
- A) 增删摄像头后调 `_rebuild_agent`(main.py 现有 `_rebuild_lock` 机制)。
- B) 工具描述写静态引导文案("摄像头列表见 get_entities"),handler 内动态从 manager 读。
选**方案B**(更简单,避免 rebuild 时序问题)。

- [ ] **Step 8.1: 写失败测试 — vision_chat camera_id fallback**

改 `tests/test_local_mcp_servers.py`:

```python
@pytest.mark.asyncio
async def test_vision_chat_uses_specified_camera():
    """vision_chat 传 camera_id 时取对应路的帧。"""
    mgr = MagicMock()
    mgr.get_frame = MagicMock(return_value=b"frame_cam_a")
    vs = MagicMock()
    vs.ask_about_frame = AsyncMock(return_value="画面里有人")
    deps = ToolDeps(mcp_client_manager=MagicMock(), camera_manager=mgr,
                    vision_client=vs, ha_service=MagicMock(), ha_client_ref=[MagicMock()])
    register_all_tools(deps)
    tool = deps.mcp_client_manager.register_tool.call_args_list  # 找 vision_chat handler
    # ... 触发 vision_chat handler,parameters={"question":"有人吗","camera_id":"cam_a"}
    mgr.get_frame.assert_called_with("cam_a")


@pytest.mark.asyncio
async def test_vision_chat_camera_id_fallback_chain():
    """未传 camera_id → 取弹窗当前路 → 再取第一个 enabled。"""
    mgr = MagicMock()
    mgr._active_display_id = "cam_display"
    mgr.get_frame = MagicMock(return_value=b"f")
    # ... 未传 camera_id 时应取 "cam_display"
    mgr.get_frame.assert_called_with("cam_display")
```

- [ ] **Step 8.2: 运行,确认失败**

Run: `pytest tests/test_local_mcp_servers.py -v -k "camera_id"`
Expected: FAIL。

- [ ] **Step 8.3: 实现 — ToolDeps + vision_chat 加 camera_id**

`ToolDeps`(`tools.py:26-41`):`camera_stream` 字段 → `camera_manager`。

`vision_chat` handler(`:66-81`)改造:

```python
    async def handler(parameters: dict, session) -> dict:
        question = str(parameters.get("question", "") or "请描述画面内容。")
        camera_id = str(parameters.get("camera_id", "") or "").strip()
        # 三级 fallback:用户指定 → 弹窗当前路 → 第一个 enabled
        if not camera_id:
            camera_id = getattr(deps.camera_manager, "_active_display_id", "") or ""
        if not camera_id:
            cams = deps.camera_manager.list_cameras()
            if cams:
                camera_id = cams[0]["id"]
        frame = deps.camera_manager.get_frame(camera_id) if camera_id else None
        if frame is None:
            return {"answer": "摄像头当前没有画面,无法分析。", "question": question, "has_frame": False}
        answer = await deps.vision_client.ask_about_frame(frame, question)
        return {"answer": answer, "question": question, "has_frame": True,
                "camera_id": camera_id, "model": deps.vision_client.model}
```

parameters schema 加 camera_id:

```python
    parameters={"type": "object", "properties": {
        "question": {"type": "string"},
        "camera_id": {"type": "string", "description": "可选,指定摄像头ID;不传取当前查看路"}
    }},
```

- [ ] **Step 8.4: 实现 — verify_condition 加 camera_id**

`create_verify_condition_handler`(`local_mcp_servers.py:119`)签名加 camera_id 依赖;`:161` 的 `camera_stream.get_latest_frame()` 改 manager 按需取(同三级 fallback)。

- [ ] **Step 8.5: 实现 — 摄像头列表注入(方案B)**

工具描述改为静态引导,handler 内动态读 manager:

```python
    description="拍摄指定摄像头画面回答问题。可用摄像头列表请调用 get_entities 查看。"
```

`get_entities` 工具(若存在)在返回里附 `manager.list_cameras()`;若不存在,vision_chat handler 内可在 frame 为空时把 `list_cameras()` 写进 answer 提示。

⚠️ 不引入 rebuild(方案A),保持简单。

- [ ] **Step 8.6: 运行 + 回归**

Run: `pytest tests/test_local_mcp_servers.py tests/test_tools.py -v`
Expected: PASS

- [ ] **Step 8.7: Commit**

```bash
git add app/tools.py app/mcp/local_mcp_servers.py tests/
git commit -m "feat(tools): vision_chat/verify_condition 加 camera_id 参数 + 三级 fallback"
```

---

## Task 9: dispatcher / automation_agent 取 camera_id 上下文

**Files:**
- Modify: `app/agents/dispatcher.py`(取 focus `:376` → 带 camera_id;`get_state()` `:423/466`)
- Modify: `app/agents/automation_agent.py`(`get_recent_frames` `:189` → manager 按 camera_id 取)
- Test: `tests/test_dispatcher.py`(`:30`)、`tests/test_automation_agent.py`(`:207`)

**Interfaces:**
- Consumes: Step 2 per-camera focus;Step 4 manager。
- Produces:camera_id 在 manager→agent→dispatcher→prompt 链路一致传透。这是 Step 2/4/8 的收口。

⚠️ **实施注意 — dispatcher 取 camera_id**:dispatcher 不知道当前 camera_id。两种来源:① 从用户消息/工具调用上下文推断("看下客厅"→ 匹配 area);② 默认主摄像头。本步实现最简路径:**默认主摄像头**(第一个 enabled),自然语言推断留到工具层(Step 8 已实现 fallback)。

- [ ] **Step 9.1: 写失败测试 — automation_agent 按摄像头取帧**

改 `tests/test_automation_agent.py:207`:

```python
@pytest.mark.asyncio
async def test_agent_evaluates_per_camera():
    """AutomationAgent 遍历每路摄像头,各自 evaluate。"""
    mgr = MagicMock()
    mgr.list_cameras = MagicMock(return_value=[{"id": "cam_a"}, {"id": "cam_b"}])
    mgr.get_recent_frames = MagicMock(side_effect=lambda cid, n=3: [f"{cid}_frame".encode()])
    auto_svc = MagicMock()
    auto_svc.evaluate = AsyncMock(return_value=[])
    agent = AutomationAgent(mgr, auto_svc, tick_seconds=999)
    await agent._run_evaluation_cycle()
    # 每路调一次 evaluate,带各自 camera_id + frames
    cids = [call.kwargs.get("camera_id") for call in auto_svc.evaluate.call_args_list]
    assert cids == ["cam_a", "cam_b"]
```

- [ ] **Step 9.2: 实现 — automation_agent 遍历多路**

`automation_agent.py:179-192` `_run_evaluation_cycle` 改为遍历 manager 的所有路:

```python
    async def _run_evaluation_cycle(self) -> None:
        if self._camera_manager is None or self._automation_service is None:
            return
        for cam in self._camera_manager.list_cameras():
            cid = cam["id"]
            frames = await asyncio.to_thread(
                self._camera_manager.get_recent_frames, cid, 3
            )
            if frames:
                await self._automation_service.evaluate(frames=frames, camera_id=cid)
```

⚠️ `AutomationAgent.__init__` 字段从 `_camera_stream` 改为 `_camera_manager`(对应 bootstrap/main.py `:471` 传入的对象)。

- [ ] **Step 9.3: 实现 — dispatcher 取 focus/state 带 camera_id**

`dispatcher.py:376` 取 focus:`vision_service.get_vision_focuses(camera_id=主摄像头id)`。
`:423/466` `get_state()` → `manager.get_state(主摄像头id)`。

dispatcher 加一个取主摄像头 id 的辅助:

```python
    def _primary_camera_id(self) -> str:
        cams = self._camera_manager.list_cameras() if self._camera_manager else []
        return cams[0]["id"] if cams else ""
```

- [ ] **Step 9.4: 运行回归**

Run: `pytest tests/test_dispatcher.py tests/test_automation_agent.py -v`
Expected: PASS

- [ ] **Step 9.5: Commit**

```bash
git add app/agents/dispatcher.py app/agents/automation_agent.py tests/
git commit -m "refactor(agents): automation_agent 遍历多路 + dispatcher 按主摄像头取 focus/state"
```

---

## Task 10: `/api/health`、`/api/state` per-camera + 兼容

**Files:**
- Modify: `app/main.py`(`/api/health` `:730`;`/api/state` `:753`)
- Test: `tests/test_http_smoke.py`(`:46`)

**Interfaces:**
- Consumes: Step 4 manager。
- Produces:全局 health/state 返回主摄像头状态,兼容 `/camera` 弹窗外的前端引用。

⚠️ 选了"改 per-camera + 保留兼容":全局端点返回**主摄像头**(第一个 enabled)状态。

- [ ] **Step 10.1: 写失败测试 — health/state 返回主摄像头**

改 `tests/test_http_smoke.py:46`:

```python
def test_health_returns_primary_camera_state(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()["data"]
    # camera_id 是主摄像头(第一个 enabled),非空
    assert "camera_id" in data
    assert data["camera_id"]  # 非空字符串
```

- [ ] **Step 10.2: 实现 — main.py health/state**

`/api/health`(`:730`)、`/api/state`(`:753`)改为:

```python
    cams = camera_manager.list_cameras()
    primary_id = cams[0]["id"] if cams else ""
    state = camera_manager.get_state(primary_id) if primary_id else {}
```

`CameraStateModel` schema 加 `camera_id` 字段(Step 2.4 已加到 CameraState,这里 schema 同步)。

- [ ] **Step 10.3: 运行 + 全量后端回归**

Run: `pytest tests/ -v`
Expected: 全部 PASS(后端至此完整,前端开始前确认后端无回归)

- [ ] **Step 10.4: Commit**

```bash
git add app/main.py tests/
git commit -m "feat(api): health/state 返回主摄像头状态 + camera_id 字段(兼容 /camera 弹窗外引用)"
```

---

## Task 11: 前端 — CameraSettingsView + useCamera

**Files:**
- Create: `frontend/src/views/CameraSettingsView.vue`
- Create: `frontend/src/composables/useCamera.js`
- Modify: `frontend/src/router/index.js`(路由 `/cameras`)
- Modify: `frontend/src/components/SidebarNav.vue`(导航项)
- Modify: `frontend/src/utils/api.js`(cameraAPI)

**Interfaces:**
- Consumes: Step 6 后端 `/api/cameras` 全套 + `/api/ha/areas`。
- Produces:摄像头管理页 UI。

⚠️ **前端实现说明**:前端 SFC 较大且依赖项目既有样式/组件库。本 Task 给出**数据契约 + 组件结构骨架**,具体样式遵循项目既有 Vue 组件风格(`MonitorView.vue`/`ChatView.vue` 为参照)。不在此穷举完整模板字符串。

- [ ] **Step 11.1: useCamera.js — API 封装**

新建 `frontend/src/composables/useCamera.js`:

```javascript
import { ref } from 'vue'
import api from '@/utils/api'

export function useCamera() {
  const cameras = ref([])
  const areas = ref([])
  const loading = ref(false)

  async function loadCameras() {
    loading.value = true
    try {
      const res = await api.get('/api/cameras')
      cameras.value = res.data.data
    } finally {
      loading.value = false
    }
  }

  async function loadAreas() {
    const res = await api.get('/api/ha/areas')
    areas.value = res.data.data
  }

  async function createCamera(data) {
    const res = await api.post('/api/cameras', data)
    await loadCameras()
    return res.data.data
  }

  async function updateCamera(id, fields) {
    const res = await api.put(`/api/cameras/${id}`, fields)
    await loadCameras()
    return res.data.data
  }

  async function deleteCamera(id) {
    await api.delete(`/api/cameras/${id}`)
    await loadCameras()
  }

  async function testStream(id) {
    const res = await api.post(`/api/cameras/${id}/test-stream`)
    return res.data.data
  }

  // 关注项
  async function loadFocuses(id) {
    const res = await api.get(`/api/cameras/${id}/focuses`)
    return res.data.data
  }
  async function addFocus(id, text) {
    const res = await api.post(`/api/cameras/${id}/focuses`, { text })
    return res.data.data
  }
  async function deleteFocus(id, focusId) {
    await api.delete(`/api/cameras/${id}/focuses/${focusId}`)
  }

  // ONVIF 发现
  async function findDevice(id) {
    const res = await api.post(`/api/cameras/${id}/discovery/find`)
    return res.data.data
  }
  async function manualIp(id, ip) {
    const res = await api.post(`/api/cameras/${id}/discovery/manual-ip`, { ip })
    return res.data.data
  }

  return {
    cameras, areas, loading,
    loadCameras, loadAreas, createCamera, updateCamera, deleteCamera,
    testStream, loadFocuses, addFocus, deleteFocus, findDevice, manualIp,
  }
}
```

- [ ] **Step 11.2: CameraSettingsView.vue — 组件结构**

新建 `frontend/src/views/CameraSettingsView.vue`,结构对应 spec §7.1 mockup(卡片列表 + 编辑面板)。`<script setup>` 调用 `useCamera`,字段对应 cameras 表列(spec §3.1):

```vue
<script setup>
import { onMounted, ref } from 'vue'
import { useCamera } from '@/composables/useCamera'

const { cameras, areas, loadCameras, loadAreas, createCamera, updateCamera, deleteCamera, testStream } = useCamera()
const editing = ref(null)   // 当前编辑的摄像头对象,null=列表态

onMounted(async () => {
  await Promise.all([loadCameras(), loadAreas()])
})

function startCreate() {
  editing.value = { name: '', source_type: 'usb', area: '', enabled: 1,
    ptz_enabled: 0, discovery_enabled: 1, display_enabled: 1 }  // cameras 表字段默认值
}
function startEdit(cam) { editing.value = { ...cam } }

async function save() {
  if (editing.value.id) {
    await updateCamera(editing.value.id, editing.value)
  } else {
    await createCamera(editing.value)
  }
  editing.value = null
}

async function remove(id) {
  if (confirm('删除该摄像头?关联规则将解绑,关注项将删除。')) {
    await deleteCamera(id)
  }
}
</script>

<template>
  <!-- 卡片列表:遍历 cameras,每张显示 name/area/online/source_type + [配置][删除] -->
  <!-- [+ 添加摄像头] 按钮 → startCreate -->
  <!-- 编辑面板(v-if="editing"):分块 基本信息/PTZ/ONVIF发现/高级参数/关注项/规则,
       字段绑定 editing.xxx,区域下拉遍历 areas,来源单选 usb|rtsp,保存调 save -->
</template>
```

字段分区严格对应 cameras 表列(spec §3.1):基本信息(name/area/source_type/rtsp_*/usb_index)、PTZ(ptz_*)、ONVIF(device_mac 只读/discovery_enabled)、高级参数(motion_*/vision_*/frame_interval_ms)、display_enabled。区域下拉数据源 `areas`(Step 6 `/api/ha/areas`)。

- [ ] **Step 11.3: 路由 + 导航**

`router/index.js` 加路由:

```javascript
{
  path: '/cameras',
  name: 'cameras',
  component: () => import('@/views/CameraSettingsView.vue'),
},
```

`SidebarNav.vue` 加导航项"摄像头管理"→ `/cameras`(参照现有导航项样式)。

- [ ] **Step 11.4: 前端构建验证**

Run: `cd frontend && npm run build`
Expected: 构建成功无报错。`npm run lint`(若有)通过。

- [ ] **Step 11.5: Commit**

```bash
git add frontend/src/views/CameraSettingsView.vue frontend/src/composables/useCamera.js frontend/src/router/index.js frontend/src/components/SidebarNav.vue frontend/src/utils/api.js
git commit -m "feat(frontend): 摄像头管理页 CameraSettingsView + useCamera composable"
```

---

## Task 12: 前端 /camera 弹窗切换 + MonitorView 适配 + 联调

**Files:**
- Modify: `frontend/src/views/ChatView.vue`(`/camera` 弹窗顶部切换标签)
- Modify: `frontend/src/views/MonitorView.vue`(多路适配)
- Test: 联调(spec §13 验收标准)

**Interfaces:**
- Consumes: Step 11 useCamera;后端 display/enable|disable。
- Produces:`/camera` 弹窗多路切换;全链路联调通过。

⚠️ **前端实现说明**:同 Task 11,给数据流和事件契约,样式参照 ChatView 既有结构。

- [ ] **Step 12.1: ChatView.vue — 弹窗切换**

`ChatView.vue` `/camera` 弹窗加顶部切换标签(spec §7.2 mockup)。核心响应式状态:

```vue
<script setup>
import { ref, watch } from 'vue'
import { useCamera } from '@/composables/useCamera'
import api from '@/utils/api'

const { cameras, loadCameras } = useCamera()
const activeCameraId = ref('')
const videoFeedUrl = ref('')

// 弹窗打开
async function openCameraModal() {
  await loadCameras()
  if (cameras.value.length) {
    await switchCamera(cameras.value[0].id)
  }
}

// 切换:旧路 disable,新路 enable + 换 video_feed URL
async function switchCamera(id) {
  if (activeCameraId.value && activeCameraId.value !== id) {
    await api.post(`/api/cameras/${activeCameraId.value}/display/disable`)
  }
  activeCameraId.value = id
  videoFeedUrl.value = `/api/cameras/${id}/video_feed`
  await api.post(`/api/cameras/${id}/display/enable`)
}

// 弹窗关闭
async function closeCameraModal() {
  if (activeCameraId.value) {
    await api.post(`/api/cameras/${activeCameraId.value}/display/disable`)
  }
  activeCameraId.value = ''
}
</script>

<template>
  <!-- 弹窗顶部:[客厅 ▾ 卧室 门口] 切换标签,遍历 cameras,click 调 switchCamera -->
  <!-- <img :src="videoFeedUrl" /> 随 activeCameraId 变化 -->
  <!-- PTZ 按钮调 /api/cameras/{activeCameraId}/ptz/move -->
  <!-- 状态区调 /api/cameras/{activeCameraId}/state -->
</template>
```

- [ ] **Step 12.2: MonitorView.vue — 多路适配**

`MonitorView.vue` 适配多路:摄像头选择器 + video_feed 按 activeCameraId 切换。若原为单路硬编码,改为遍历 `/api/cameras`。

- [ ] **Step 12.3: 前端构建**

Run: `cd frontend && npm run build`
Expected: 成功

- [ ] **Step 12.4: 联调验证(对照 spec §13 验收标准)**

逐项验证(需要真机/模拟环境):

1. **迁移无回归**:老部署(有 config.json vision/ptz 段)升级 → 自动迁移成 cameras 表一条记录(ID `cam_*`、名"默认摄像头"),RTSP/PTZ/运动/视觉参数全部保留。
2. **多路互不干扰**:摄像头管理页添加第 2/3/4 路,每路独立配置来源/参数/PTZ。
3. **弹窗切换**:`/camera` 弹窗切换查看不同摄像头,同一时刻只跑当前路展示推理;切换时旧路停、新路启。
4. **ONVIF 找回**:RTSP 路摄像头 DHCP 换 IP 后,自动触发该路发现找回新 IP,RTSP+PTZ 同步恢复;其他路不受影响。
5. **⚠️ VLM 并发实测**:四路同时触发运动自动化时,VLM 并发不超过 5,第 6 个排队;展示推理不受自动化配额影响。观察云端账单/429 日志。
6. **工具匹配**:`vision_chat`/`verify_condition` 按 camera_id 取正确路;未传时三级 fallback。
7. **删除解绑**:删除摄像头时关联规则 `camera_id` 置空、关注项删除。

- [ ] **Step 12.5: ⚠️ GIL/FPS 实测**

4 路全开时观察采集 FPS(spec §9 + Step 4 留的验证点)。N 路 OpenCV 解码抢 GIL,若采集饿死(FPS 过低),考虑:`_worker` 的 imencode 工作下沉线程池,或降帧(frame_interval_ms 调大)。无问题则跳过。

- [ ] **Step 12.6: 配置清理(spec §8)**

联调通过后,清理 `config.json` / `config.example.json`:

```bash
# 删除 vision 段、ptz 段、automation.camera_vl_display_enabled
# (人工编辑或脚本,迁移已把数据写入 cameras 表)
```

`app/core/config.py` 中 `vision.*`/`ptz.*` 属性访问同步删除(改为从 cameras 表读)。从 `.env` 删除 `RTSP_PASSWORD`/`PTZ_PASSWORD`。

- [ ] **Step 12.7: Commit**

```bash
git add frontend/src/views/ChatView.vue frontend/src/views/MonitorView.vue config.json config.example.json app/core/config.py
git commit -m "feat(frontend): /camera 弹窗多路切换 + MonitorView 适配 + 配置清理(迁移收尾)"
```

---

## Self-Review 结论

**1. Spec coverage(决策覆盖):**
- 决策 1(ID 方案 cam_<6位> + name + sort_order)→ Step 1 DDL + Step 4 `_spawn` ✅
- 决策 2(MJPEG)→ Step 6 `video_feed` StreamingResponse multipart ✅
- 决策 3(VLM 并发 1+5)→ Step 4 双 Semaphore + Step 12.4 验收 5 ✅
- 决策 4(推理分工:展示只跑当前路)→ Step 4 enable_display 切换 + Step 12.1 ✅
- 决策 5(ONVIF 多路,每路 MAC)→ Step 3 find_camera(camera_id) + Step 1 device_mac 列 ✅
- 决策 6(迁移 + 删 config)→ Step 1 迁移 + Step 12.6 配置清理 ✅
- 决策 7(规则/关注项绑 camera_id)→ Step 1 rules.camera_id + Step 2 per-camera focuses ✅
- 决策 8(定时任务不绑)→ 全程未碰 scheduled_tasks ✅
- 决策 9(前端全量)→ Step 11/12 ✅

**2. Placeholder scan:**
- Step 11.2/12.1 的 Vue `<template>` 部分标注"参照既有组件风格"而非穷举完整模板——这是**前端样式的合理省略**(数据契约/事件/字段绑定都已给出),非占位符。所有后端 Task 的代码块完整。
- 所有 file:line 引用基于 explore 实测的真实行号。
- 无 "TBD/TODO/类似 Task N" 占位。

**3. Type consistency(类型一致性):**
- `CameraStream(camera_id, config, vision_service, on_automation_trigger=, discovery_service=)` 在 Step 2 定义,Step 4 `_spawn` 调用一致 ✅
- `find_and_apply(camera_id)` Step 3 定义,Step 2.7 worker 调用一致 ✅
- `evaluate(frames, camera_id="")` Step 5 定义,Step 4 `_eval_one` + Step 9.2 agent 调用一致 ✅
- `get_vision_focuses(camera_id="")` Step 2 定义,Step 6 routes + Step 9.3 dispatcher 调用一致 ✅
- `cameras_get(camera_id) -> dict | None` Step 1 定义,Step 3/5/6 调用一致 ✅
- `notify_ip_changed(new_ip, camera_id="")` Step 3 定义,Step 4 `_on_camera_ip_changed` + Step 5 PtzRegistry 调用一致 ✅

**4. 风险与前置(已在相应 Step 标注):**
- 迁移幂等用 KV 标记(非 spec "表非空")→ Step 1.8 ✅
- 全局端点保留兼容 → Step 10 ✅
- 工具描述静态绑定 → Step 8.5 方案B ✅
- 3 个测试文件重定向 → Step 6.5 ✅
- GIL 竞争实测 → Step 4.4(实现层)+ Step 12.5(验证层)✅
- bootstrap 构造顺序(automation_service 晚于 CameraManager)→ Step 7.3 方案B setter ✅
- ptz 单例去化过渡 → Step 5.6 保留 deprecated + Step 6 统一切换 ✅

**5. 范围:** 后端 10 步(Task 1-10)+ 前端 2 步(Task 11-12),前后端可分两个 PR(后端先合,前端跟进)。录像/分布式/同屏显式列为非目标(spec §10),本计划不涉及。

