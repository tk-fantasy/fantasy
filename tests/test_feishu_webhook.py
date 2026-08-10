"""飞书 webhook 路由测试。

测试策略：直接调路由函数（不启动 FastAPI app，避免触发完整 lifespan）。
mock container 的 dispatcher 和 integration_layer。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.routes.feishu_routes import feishu_webhook


def _mock_request(body: dict) -> MagicMock:
    """构造 mock Request 对象。"""
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return request


def _mock_container(dispatch_result=None, speak_to_result=None, no_layer=False):
    """构造 mock container。"""
    container = MagicMock()
    container.dispatcher = MagicMock()
    container.dispatcher.dispatch = AsyncMock(
        return_value=dispatch_result or [])
    if no_layer:
        container.integration_layer = None
    else:
        container.integration_layer = MagicMock()
        container.integration_layer.speak_to = AsyncMock(
            return_value=speak_to_result or {"ok": True})
    return container


def _inst(namespace, name, **payload_attrs):
    """构造一个模拟 Instruction 的 MagicMock。

    注意：MagicMock 构造器的 name 是特殊参数（设置 mock 自身的 repr 名），
    不会创建 .name 子属性，故必须构造后显式赋值。
    """
    inst = MagicMock()
    header = MagicMock()
    header.namespace = namespace
    header.name = name  # 构造后赋值才生效
    inst.header = header
    payload = MagicMock()
    for k, v in payload_attrs.items():
        setattr(payload, k, v)
    inst.payload = payload
    return inst


def test_challenge_verification():
    """飞书配 webhook 时发 challenge，原样返回。"""
    request = _mock_request({"challenge": "ajkdslfjksdljf", "token": "xxx"})

    async def go():
        with patch("app.routes.feishu_routes.get_container",
                   return_value=_mock_container()):
            return await feishu_webhook(request)

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["challenge"] == "ajkdslfjksdljf"


def test_text_message_dispatched_and_sent():
    """文本消息 → dispatch → speak_to 发飞书。"""
    feishu_event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user123"}},
            "message": {
                "chat_id": "oc_test_chat",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
            },
        }
    }

    dispatch_result = [
        _inst("Template", "ToastStream", stream="你好！有什么可以帮你的？"),
        _inst("Dialog", "Finish", success=True, message=""),
    ]

    container = _mock_container(dispatch_result=dispatch_result)
    request = _mock_request(feishu_event)

    async def go():
        with patch("app.routes.feishu_routes.get_container", return_value=container):
            return await feishu_webhook(request)

    result = asyncio.new_event_loop().run_until_complete(go())

    container.dispatcher.dispatch.assert_called_once()
    call_args = container.dispatcher.dispatch.call_args
    event_arg = call_args[0][0]
    assert event_arg.header.session_id == "feishu_oc_test_chat"

    container.integration_layer.speak_to.assert_called_once()
    speak_args = container.integration_layer.speak_to.call_args
    assert speak_args[0][0] == "feishu"
    assert speak_args[0][1] == "你好！有什么可以帮你的？"
    assert speak_args[0][2] == {"chat_id": "oc_test_chat"}

    assert result["ok"] is True


def test_non_text_message_ignored():
    """非文本消息忽略。"""
    feishu_event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_xxx"}},
            "message": {
                "chat_id": "oc_xxx",
                "message_type": "image",
                "content": "{}",
            },
        }
    }

    container = _mock_container()
    request = _mock_request(feishu_event)

    async def go():
        with patch("app.routes.feishu_routes.get_container", return_value=container):
            return await feishu_webhook(request)

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True
    container.dispatcher.dispatch.assert_not_called()


def test_at_mention_stripped():
    """群聊 @机器人 的消息去掉 @mention 后取纯文本。"""
    feishu_event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_xxx"}},
            "message": {
                "chat_id": "oc_group",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 打开床头灯"}),
            },
        }
    }

    container = _mock_container(dispatch_result=[
        _inst("Template", "ToastStream", stream="已打开"),
    ])
    request = _mock_request(feishu_event)

    async def go():
        with patch("app.routes.feishu_routes.get_container", return_value=container):
            return await feishu_webhook(request)

    asyncio.new_event_loop().run_until_complete(go())

    call_args = container.dispatcher.dispatch.call_args
    event_arg = call_args[0][0]
    assert event_arg.payload["query"] == "打开床头灯"


def test_no_integration_layer_still_returns_ok():
    """无集成平台时 webhook 也不崩。"""
    feishu_event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_xxx"}},
            "message": {
                "chat_id": "oc_xxx",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
            },
        }
    }

    container = _mock_container(dispatch_result=[
        _inst("Template", "ToastStream", stream="你好"),
    ], no_layer=True)
    request = _mock_request(feishu_event)

    async def go():
        with patch("app.routes.feishu_routes.get_container", return_value=container):
            return await feishu_webhook(request)

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True
