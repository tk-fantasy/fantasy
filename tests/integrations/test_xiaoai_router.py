"""XiaoAiRouter 直通逻辑测试（不 spawn，mock HA caller）。"""

import asyncio
from unittest.mock import AsyncMock

from integrations.xiaoai.plugin import XiaoAiRouter


def test_execute_entity_derivation():
    """从 media_player entity 推导 execute_text_directive notify 实体。"""
    router = XiaoAiRouter(
        ha_caller=None,
        media_player_entity="media_player.xiaomi_cn_2166464483_lx06",
    )
    entity = router._execute_entity()
    assert entity == "notify.xiaomi_cn_2166464483_lx06_execute_text_directive_a_5_5"


def test_route_calls_ha_notify_send_message():
    """route(text) 调 HA notify.send_message 到 execute_text_directive 实体。"""
    ha_caller = AsyncMock()
    router = XiaoAiRouter(
        ha_caller=ha_caller,
        media_player_entity="media_player.xiaomi_cn_2166464483_lx06",
    )

    async def go():
        result = await router.route("播放周杰伦的歌")
        return result

    result = asyncio.new_event_loop().run_until_complete(go())

    assert result["ok"] is True
    assert result["executed"] == "播放周杰伦的歌"
    ha_caller.call_service.assert_called_once_with(
        domain="notify",
        service="send_message",
        data={
            "entity_id": "notify.xiaomi_cn_2166464483_lx06_execute_text_directive_a_5_5",
            "message": "播放周杰伦的歌",
        },
    )


def test_route_returns_ok_even_with_different_text():
    """不同文字都能路由。"""
    ha_caller = AsyncMock()
    router = XiaoAiRouter(
        ha_caller=ha_caller,
        media_player_entity="media_player.xiaomi_cn_123_lx06",
    )

    async def go():
        return await router.route("讲个笑话")

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True
    assert "笑话" in result["executed"]
