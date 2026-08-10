"""Shared fixtures for all tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path so `app` is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 测试环境禁用冷启动进度端口（8011）：app.main 模块级会调 _startup_progress.start()
# 开一个 daemon HTTP 线程，在 pytest 进程退出时触发 excepthook 崩溃。
# 必须在 app.main 被导入前 patch（app.__init__ 不触发，但任何路由 import 会间接触发）。
from app import startup_progress as _startup_progress_mod  # noqa: E402

_startup_progress_mod.startup_progress.start = lambda: None
_startup_progress_mod.startup_progress.stop = lambda: None
_startup_progress_mod.startup_progress.mark_ready = lambda: None
_startup_progress_mod.startup_progress.set = lambda *a, **kw: None

# app.main 的模块级副作用已在装修层收敛：RAG 后台线程移入 lifespan，不再
# 在 import 时启动。服务初始化（initialize_services）仍在模块级执行，但测试
# 环境可承受（轻量、不连真实外部服务）。故无需再注入 MagicMock 桩模块。


@pytest.fixture(scope="session", autouse=True)
def _close_database_at_session_end():
    """pytest 退出前关闭全局 SQLite 连接。

    aiosqlite 0.22.1 的连接 worker 线程是非 daemon 线程：只要有一个连接
    没被 close，解释器退出时会在 threading._shutdown 永久挂起（测试全过
    但进程不退出，CI 会超时）。测试里的 Database 单例只 init 不 close，
    故在 session 末尾统一收尾。
    """
    yield
    import asyncio

    from app.core.database import Database

    if Database._db is not None:
        asyncio.run(Database.close())


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch, tmp_path, request):
    """Auto-patch app.config so tests don't read real config.json / .env."""
    import app.core.config as cfg

    # 让 update_config_section 写到临时文件，避免 rename 真实 config.json
    # （Docker bind-mount 下 os.rename 会报 Device or resource busy）
    tmp_config = tmp_path / "config.json"
    tmp_config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_config)

    test_config = {
        "llm": {
            "enabled": True,
            "base_url": "http://127.0.0.1:11434",
            "chat_model": "test-chat",
            "vision_model": "test-vision",
            "embed_model": "test-embed",
            "think": False,
            "intent_timeout_seconds": 5,
            "summaryEnabled": False,
        },
        "automation": {
            "eval_interval_seconds": 10,
            "silent_eval_enabled": True,
            "silent_eval_interval_seconds": 60,
            "default_cooldown_seconds": 5,
            "camera_vl_display_enabled": True,
        },
        "storage": {},
        "providers": {},
        "vision": {
            "rtsp_url": "rtsp://192.168.1.50:554/stream2",
            "rtsp_username": "admin",
            "rtsp_password_env": "RTSP_PASSWORD",
            "device_mac": "",
            "discovery_enabled": True,
            "discovery_timeout_seconds": 30,
            "discovery_subnet": "",
        },
        "ptz": {
            "enabled": True,
            "ip": "192.168.1.50",
            "port": 80,
            "username": "admin",
            "password_env": "PTZ_PASSWORD",
            "speed": 0.5,
            "step_ms": 300,
        },
        "chat_assistant": {},
        "rag": {},
        "logging": {},
        "llm_keys": [],
    }
    monkeypatch.setattr(cfg, "CONFIG", test_config)

    # D6:默认禁用 legacy→cameras 迁移。conftest 注入了 vision/ptz 段,若不禁用,
    # 任何调 Database.init() 的测试(rules/sessions/kv)都会被触发"默认摄像头"插入,
    # 污染全量回归。仅 @pytest.mark.migration 标记的测试自己 patch
    # _legacy_camera_config 喂数据开启迁移。
    import app.core.database as _db_mod
    if not request.node.get_closest_marker("migration"):
        monkeypatch.setattr(_db_mod, "_legacy_camera_config", lambda: None)

    yield test_config
