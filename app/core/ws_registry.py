"""在线聊天 WS 连接注册表 — user_id → sockets，及独立的事件通道。

定时任务（message/reminder）执行后把回复推给该用户当前在线的连接，让文字
与小爱语音同步出现。聊天 WS 只在 ChatView 存活期间存在（前端现状）；用户
不在线时回复仍写进会话历史，回聊天页可见。

事件通道（/ws/events）与聊天通道分离：设备页等长驻页面订阅 HA 实体状态
推送，语义是「广播给所有事件订阅者」而非「推给某个用户」，混进 user 维度
会让告警广播误触设备页、设备状态推送误触聊天流。两套注册表互不相通。

只做注册/注销/推送，不带业务语义；单个连接推送失败不影响其余连接。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_sockets: dict[str, set[Any]] = {}

# /ws/events 通道的连接集合（无 user 维度，纯广播）
_event_sockets: set[Any] = set()


def register(user_id: str, websocket: Any) -> None:
    """连接建立（chat_ws 认证通过后）调用。"""
    _sockets.setdefault(user_id, set()).add(websocket)


def unregister(user_id: str, websocket: Any) -> None:
    """连接断开（finally）调用。空集合自动清理，防泄漏。"""
    conns = _sockets.get(user_id)
    if conns is None:
        return
    conns.discard(websocket)
    if not conns:
        _sockets.pop(user_id, None)


async def push_to_user(user_id: str, payload: dict) -> None:
    """把一条 JSON 推给该用户所有在线 socket。"""
    for ws in list(_sockets.get(user_id, ())):
        try:
            await ws.send_json(payload)
        except Exception:
            # 连接刚好关闭等场景：注册表清理由 chat_ws 的 finally 负责
            logger.debug("ws_registry push 失败（连接可能已断开）", exc_info=True)


async def push_to_all(payload: dict) -> None:
    """把一条 JSON 推给所有在线用户（告警广播用）。"""
    for user_id in list(_sockets.keys()):
        await push_to_user(user_id, payload)


def register_events(websocket: Any) -> None:
    """事件通道连接建立（/ws/events 认证通过后）调用。"""
    _event_sockets.add(websocket)


def unregister_events(websocket: Any) -> None:
    """事件通道连接断开（finally）调用。"""
    _event_sockets.discard(websocket)


async def push_to_events(payload: dict) -> None:
    """把一条 JSON 广播给所有事件通道连接（HA 实体状态实时推送用）。"""
    for ws in list(_event_sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            logger.debug("events push 失败（连接可能已断开）", exc_info=True)
