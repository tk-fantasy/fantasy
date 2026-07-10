"""Simple in-memory rate limiter using sliding window."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class RateLimiter:
    """Sliding window rate limiter.

    Usage:
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        if not limiter.check("user_123"):
            raise AppException("Too many requests", code="rate_limit", http_status=429)
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        """Check if request is allowed. Returns True if allowed, False if rate limited."""
        now = time.time()
        window_start = now - self._window_seconds

        # Clean old entries
        self._requests[key] = [t for t in self._requests[key] if t > window_start]

        # Check limit
        if len(self._requests[key]) >= self._max_requests:
            return False

        # Record this request
        self._requests[key].append(now)
        return True


# 全局速率限制器：按 client IP 限制每分钟请求数，防止内网滥用 LLM API。
# 阈值较宽（120 次/分钟 ≈ 每秒 2 次），正常浏览和聊天不会触发；
# WebSocket 路径在中间件层豁免，不经过此 limiter。
global_limiter = RateLimiter(max_requests=120, window_seconds=60)
