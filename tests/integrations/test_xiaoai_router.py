"""XiaoAiRouter 直通逻辑测试（不 spawn，mock HA caller）。

直通消息必须是 JSON 列表 [文本, 静默执行]：xiaiaomi_home 把 message 按
YAML 解析成 action 参数，纯文本会因参数个数不符被静默丢弃。
"""

import asyncio
import json
from unittest.mock import AsyncMock

from integrations.xiaoai.plugin import XiaoAiResolver, XiaoAiRouter

SLUG = "xiaomi_cn_2166464483_lx06"
EXECUTE = f"notify.{SLUG}_execute_text_directive_a_5_5"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_router(states=None, call_results=None):
    ha = AsyncMock()
    ha.get_states.return_value = states if states is not None else {
        "states": [
            {"entity_id": f"notify.{SLUG}_play_text_a_5_1"},
            {"entity_id": EXECUTE},
            {"entity_id": f"media_player.{SLUG}"},
        ]
    }
    if call_results is None:
        ha.call_service.return_value = [{"entity_id": "notify.ok"}]
    else:
        ha.call_service.side_effect = call_results
    return XiaoAiRouter(ha_caller=ha, resolver=XiaoAiResolver(ha, "")), ha


def test_route_sends_json_list_to_execute_entity():
    """route(text) 调 execute_text_directive 实体，消息为 [文本, False] 列表。"""
    router, ha = _make_router()

    result = _run(router.route("播放周杰伦的歌"))

    assert result["ok"] is True
    assert result["executed"] == "播放周杰伦的歌"
    assert result["speaker"] == SLUG
    ha.call_service.assert_called_once_with(
        domain="notify",
        service="send_message",
        data={
            "entity_id": EXECUTE,
            "message": json.dumps(["播放周杰伦的歌", False], ensure_ascii=False),
        },
    )


def test_route_escapes_special_text_safely():
    """含引号/换行的文本经 JSON 转义后仍是合法 YAML 参数列表。"""
    router, ha = _make_router()

    result = _run(router.route('来一首"晴天"\n周杰伦的'))

    assert result["ok"] is True
    sent = ha.call_service.call_args.kwargs["data"]["message"]
    assert json.loads(sent) == ['来一首"晴天"\n周杰伦的', False]


def test_route_no_speaker_returns_error():
    """没有小爱实体：返回可见错误而非假成功。"""
    router, ha = _make_router(states={"states": []})

    result = _run(router.route("播放音乐"))

    assert "未发现小爱音箱" in result["error"]
    ha.call_service.assert_not_awaited()


def test_route_empty_result_guard():
    """call_service 返回 []：实体失联，返回错误并失效缓存。"""
    router, ha = _make_router(call_results=[[], [{"entity_id": "notify.ok"}]])

    first = _run(router.route("播放音乐"))
    assert "实体不存在" in first["error"]

    second = _run(router.route("播放音乐"))
    assert second["ok"] is True
    # 守卫触发后重新扫描
    assert ha.get_states.await_count == 2
