"""飞书 webhook 路由。

挂在 /webhook/feishu（不走 /api 前缀），利用现有 api_token_guard 中间件的
not request.url.path.startswith("/api") 逻辑自动绕过鉴权。

飞书事件回调流程：
  1. challenge 验证（飞书配 webhook 时）
  2. 解析事件（im.message.receive_v1）
  3. session 映射 chat_id → "feishu_{chat_id}"
  4. Dispatcher.dispatch() 拿回复（REST 同步版）
  5. 提取 ToastStream final_content
  6. speak_to 定向发到飞书
"""

import json
import logging
import re

from fastapi import APIRouter, Request

from ..container import get_container
from ..core.tracing import new_request_id
from ..schema.chat_schema import Event, Nlp

logger = logging.getLogger(__name__)

router = APIRouter()

# 去掉 @mention 的正则（群聊消息含 @_user_1）
_AT_MENTION_RE = re.compile(r"@_user_\d+")


@router.post("/webhook/feishu")
async def feishu_webhook(request: Request):
    """飞书事件回调入口。"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    # 1. URL 验证 challenge（飞书配 webhook 时发）
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    # 2. 解析事件
    event = body.get("event", {})
    header = body.get("header", {})
    event_type = header.get("event_type", "")

    # 只处理消息事件
    if event_type != "im.message.receive_v1" and "message" not in event:
        return {"ok": True}

    message = event.get("message", {})
    msg_type = message.get("message_type")
    if msg_type != "text":
        return {"ok": True}  # 非文本消息忽略

    # 3. 提取消息内容 + chat_id + user_id
    chat_id = message.get("chat_id", "")
    user_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    try:
        raw_content = json.loads(message.get("content", "{}")).get("text", "")
    except (json.JSONDecodeError, TypeError):
        raw_content = ""

    # 4. 去掉 @mention（群聊时消息含 @_user_1）
    query = _AT_MENTION_RE.sub("", raw_content).strip()
    if not query:
        return {"ok": True}

    # 5. 调 Dispatcher（复用 REST dispatch 同步版）
    container = get_container()
    session_id = f"feishu_{chat_id}"
    rid = new_request_id()
    event_obj = Event.build_event(
        Nlp.Request(query=query),
        request_id=rid,
        session_id=session_id,
    )

    try:
        instructions = await container.dispatcher.dispatch(
            event_obj, user_id=f"feishu_{user_id}"
        )
    except Exception as exc:
        logger.warning("飞书 webhook dispatch 失败: %s", exc)
        return {"ok": True}

    # 6. 提取 final_content（ToastStream）
    final_content = _extract_final_content(instructions)
    if not final_content:
        return {"ok": True}

    # 7. 定向发到飞书
    integration_layer = getattr(container, "integration_layer", None)
    if integration_layer is not None:
        try:
            await integration_layer.speak_to(
                "feishu", final_content, {"chat_id": chat_id}
            )
        except Exception as exc:
            logger.warning("飞书发消息失败: %s", exc)

    return {"ok": True}


def _extract_final_content(instructions: list) -> str:
    """从 Instruction 列表提取 ToastStream final_content。

    兼容三类对象：
      - Instruction pydantic 对象（.header.namespace/.name 为 str，.payload 为 dict）
      - 测试用 MagicMock（属性访问）
      - dict（.get 访问）
    """
    for inst in instructions:
        # 兼容 Instruction 对象和 dict
        header = getattr(inst, "header", inst.get("header") if isinstance(inst, dict) else {})
        payload = getattr(inst, "payload", inst.get("payload") if isinstance(inst, dict) else {})
        ns = getattr(header, "namespace", header.get("namespace", "") if isinstance(header, dict) else "")
        name = getattr(header, "name", header.get("name", "") if isinstance(header, dict) else "")
        if ns == "Template" and name == "ToastStream":
            return getattr(payload, "stream", payload.get("stream", "") if isinstance(payload, dict) else "")
    return ""
