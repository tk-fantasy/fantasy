"""请求追踪 ID 管理。

为每个请求生成唯一 ID，贯穿日志和跨服务调用，便于问题排查。
"""
from __future__ import annotations

import contextvars
import logging
import uuid

# 全局 ContextVar，存储当前请求的 request_id
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdFilter(logging.Filter):
    """日志过滤器：给每条日志注入 request_id 字段。

    用法：
        logger = logging.getLogger(__name__)
        logger.addFilter(RequestIdFilter())
        # 日志格式中使用 %(request_id)s
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")  # type: ignore[attr-defined]
        return True


def new_request_id() -> str:
    """生成新的请求 ID（8 位 UUID 前缀）。"""
    return str(uuid.uuid4())[:8]


def set_request_id(rid: str) -> None:
    """设置当前请求的 request_id。"""
    request_id_var.set(rid)
