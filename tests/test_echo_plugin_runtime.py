"""echo 插件 runtime 单元测试（不 spawn 子进程，直接调用 plugin 对象）。"""

import asyncio
import json
import sys
from pathlib import Path

# 让 tests/integrations/echo 可被导入
ECHO_DIR = Path(__file__).parent / "integrations" / "echo"
if str(ECHO_DIR) not in sys.path:
    sys.path.insert(0, str(ECHO_DIR))

from plugin import EchoPlugin  # noqa: E402


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
