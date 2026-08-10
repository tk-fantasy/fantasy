"""IntegrationLayer.speak_to 定向发送测试。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.integration.integration_layer import IntegrationLayer


def test_speak_to_no_plugin_returns_error():
    """无指定插件时返回 error。"""
    layer = IntegrationLayer(plugin_dir="nonexistent_dir")

    async def go():
        result = await layer.speak_to("feishu", "hello", {"chat_id": "oc_xxx"})
        assert result["ok"] is False
        assert "未运行" in result["error"]

    asyncio.new_event_loop().run_until_complete(go())


def test_speak_to_calls_plugin_sink_speak():
    """speak_to 定向调指定插件的 sink.speak，传 text + chat_id 作 msg_id。"""
    layer = IntegrationLayer(plugin_dir="nonexistent_dir")

    # mock supervisor.get_process 返回一个有 call 方法的 mock
    mock_proc = MagicMock()
    mock_proc.is_alive = True
    mock_proc.call = AsyncMock(return_value={"ok": True, "chat_id": "oc_xxx"})
    layer._supervisor.get_process = MagicMock(return_value=mock_proc)

    async def go():
        result = await layer.speak_to("feishu", "你好", {"chat_id": "oc_xxx"})
        assert result["ok"] is True
        assert result["chat_id"] == "oc_xxx"
        # 验证 call 传了正确参数
        mock_proc.call.assert_called_once()
        call_args = mock_proc.call.call_args
        # call(METHOD_SPEAK, params) 位置参数
        args = call_args[0]
        assert args[0] == "sink.speak"
        assert args[1]["text"] == "你好"
        assert args[1]["msg_id"] == "oc_xxx"  # chat_id 作为 msg_id 传入

    asyncio.new_event_loop().run_until_complete(go())


def test_speak_to_dead_plugin_returns_error():
    """插件进程不存活时返回 error。"""
    layer = IntegrationLayer(plugin_dir="nonexistent_dir")

    mock_proc = MagicMock()
    mock_proc.is_alive = False
    layer._supervisor.get_process = MagicMock(return_value=mock_proc)

    async def go():
        result = await layer.speak_to("feishu", "hello", {"chat_id": "oc_xxx"})
        assert result["ok"] is False

    asyncio.new_event_loop().run_until_complete(go())
