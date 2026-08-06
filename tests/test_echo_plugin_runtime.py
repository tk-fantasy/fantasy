"""echo 插件 runtime 单元测试（不 spawn 子进程，直接调用 plugin 对象）。"""

import asyncio
import importlib.util
import json
from pathlib import Path

# 用 importlib 按绝对路径加载，避免 sys.path 污染（echo/xiaoai 都有 plugin.py）
ECHO_DIR = Path(__file__).parent / "integrations" / "echo"
ECHO_PLUGIN_PATH = ECHO_DIR / "plugin.py"

_spec = importlib.util.spec_from_file_location("echo_plugin", ECHO_PLUGIN_PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
EchoPlugin = _module.EchoPlugin


def test_echo_plugin_handles_speak():
    manifest = json.loads((ECHO_DIR / "manifest.json").read_text(encoding="utf-8"))
    plugin = EchoPlugin()
    plugin.setup(manifest)

    result = asyncio.new_event_loop().run_until_complete(
        plugin.handle("sink.speak", {"text": "你好", "msg_id": "m1"})
    )
    assert result == {"spoken": "你好", "msg_id": "m1"}


def test_echo_plugin_handles_interrupt():
    manifest = json.loads((ECHO_DIR / "manifest.json").read_text(encoding="utf-8"))
    plugin = EchoPlugin()
    plugin.setup(manifest)

    result = asyncio.new_event_loop().run_until_complete(
        plugin.handle("sink.interrupt", {})
    )
    assert result == {"interrupted": True}


def test_echo_plugin_unknown_method_returns_error():
    manifest = json.loads((ECHO_DIR / "manifest.json").read_text(encoding="utf-8"))
    plugin = EchoPlugin()
    plugin.setup(manifest)

    result = asyncio.new_event_loop().run_until_complete(
        plugin.handle("nope.nope", {})
    )
    assert "error" in result
