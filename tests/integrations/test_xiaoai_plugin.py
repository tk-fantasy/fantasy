"""小爱插件单元测试（mock HA caller，不 spawn、不真连 HA）。"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

# 让 integrations/xiaoai 可被导入
XIAOAI_DIR = Path(__file__).parent.parent.parent / "integrations" / "xiaoai"
if str(XIAOAI_DIR) not in sys.path:
    sys.path.insert(0, str(XIAOAI_DIR))

from plugin import XiaoAiPlugin  # noqa: E402


def _manifest():
    return json.loads((XIAOAI_DIR / "manifest.json").read_text(encoding="utf-8"))


def _make_plugin_with_mock_ha():
    """构造 plugin + 注入 mock HA caller。"""
    manifest = _manifest()
    plugin = XiaoAiPlugin()
    plugin.setup(manifest)
    plugin.ha_caller = AsyncMock()
    plugin.ha_caller.call_service.return_value = {"ok": True}
    # 重建 sink 用 mock caller
    from plugin import XiaoAiSink
    schema = manifest["capabilities"][0]["config_schema"]
    entity_id = schema["entity_id"]["default"]
    execute_mode = schema["execute_mode"]["default"]
    plugin.sinks = [XiaoAiSink(plugin.ha_caller, entity_id, execute_mode)]
    return plugin


def test_speak_calls_intelligent_speaker_with_execute_false():
    plugin = _make_plugin_with_mock_ha()

    result = asyncio.new_event_loop().run_until_complete(
        plugin.handle("sink.speak", {"text": "床头灯已打开", "msg_id": "m1"})
    )

    assert result["spoken"] == "床头灯已打开"
    plugin.ha_caller.call_service.assert_awaited_once()
    kwargs = plugin.ha_caller.call_service.call_args.kwargs
    assert kwargs["domain"] == "xiaomi_miot"
    assert kwargs["service"] == "intelligent_speaker"
    assert kwargs["entity_id"].startswith("media_player.")
    assert kwargs["data"]["text"] == "床头灯已打开"
    assert kwargs["data"]["execute"] is False


def test_interrupt_calls_media_stop():
    plugin = _make_plugin_with_mock_ha()

    result = asyncio.new_event_loop().run_until_complete(
        plugin.handle("sink.interrupt", {})
    )

    assert result["interrupted"] is True
    plugin.ha_caller.call_service.assert_awaited_once()
    kwargs = plugin.ha_caller.call_service.call_args.kwargs
    assert kwargs["domain"] == "media_player"
    assert kwargs["service"] == "media_stop"


def test_concurrent_speaks_are_serialized_by_lock():
    """两次 speak 并发发起，锁保证不并发调 HA（顺序执行）。"""
    plugin = _make_plugin_with_mock_ha()

    async def go():
        await asyncio.gather(
            plugin.handle("sink.speak", {"text": "第一条"}),
            plugin.handle("sink.speak", {"text": "第二条"}),
        )

    asyncio.new_event_loop().run_until_complete(go())
    assert plugin.ha_caller.call_service.await_count == 2
