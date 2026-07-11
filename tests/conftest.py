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


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch, tmp_path):
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
        "automation": {"eval_interval_seconds": 10},
        "storage": {},
        "providers": {},
        "chat_assistant": {},
        "rag": {},
        "logging": {},
        "llm_keys": [],
    }
    monkeypatch.setattr(cfg, "CONFIG", test_config)
    yield test_config
