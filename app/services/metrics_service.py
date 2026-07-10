"""轻量内存 metrics 服务 — 零外部依赖。

记录请求计数、延迟、工具调用、LLM 调用等指标，
通过 /api/metrics 端点暴露 JSON 快照。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MetricsService:
    """线程安全的内存指标收集器。"""

    # HTTP 请求
    request_count: int = 0
    request_errors: int = 0
    _latencies: deque[float] = field(default_factory=lambda: deque(maxlen=200))

    # MCP 工具调用
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_errors: dict[str, int] = field(default_factory=dict)

    # LLM 调用
    llm_calls: int = 0
    llm_errors: int = 0

    # 自动化
    automation_evals: int = 0

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── 记录方法 ──

    def record_request(self, latency: float, error: bool = False) -> None:
        with self._lock:
            self.request_count += 1
            if error:
                self.request_errors += 1
            self._latencies.append(latency)

    def record_tool_call(self, tool_name: str, error: bool = False) -> None:
        with self._lock:
            self.tool_calls[tool_name] = self.tool_calls.get(tool_name, 0) + 1
            if error:
                self.tool_errors[tool_name] = self.tool_errors.get(tool_name, 0) + 1

    def record_llm_call(self, error: bool = False) -> None:
        with self._lock:
            self.llm_calls += 1
            if error:
                self.llm_errors += 1

    def record_automation_eval(self) -> None:
        with self._lock:
            self.automation_evals += 1

    # ── 快照 ──

    def snapshot(self) -> dict:
        """返回所有指标的 JSON 快照。"""
        with self._lock:
            lats = list(self._latencies)

        avg_latency = sum(lats) / len(lats) if lats else 0.0
        p95_latency = 0.0
        if lats:
            sorted_lats = sorted(lats)
            idx = int(len(sorted_lats) * 0.95)
            p95_latency = sorted_lats[min(idx, len(sorted_lats) - 1)]

        return {
            "http": {
                "total": self.request_count,
                "errors": self.request_errors,
                "avg_latency_s": round(avg_latency, 4),
                "p95_latency_s": round(p95_latency, 4),
                "latency_samples": len(lats),
            },
            "tools": {
                "calls": dict(self.tool_calls),
                "errors": dict(self.tool_errors),
            },
            "llm": {
                "calls": self.llm_calls,
                "errors": self.llm_errors,
            },
            "automation": {
                "evals": self.automation_evals,
            },
        }
