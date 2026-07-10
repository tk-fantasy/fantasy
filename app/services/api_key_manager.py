from __future__ import annotations

import asyncio
import logging
import time

from ..core.config import get_config
from ..core.key_resolver import get_keys_for_role

logger = logging.getLogger(__name__)

# 故障熔断参数：单个 key 连续失败达到阈值后临时移出轮转，冷却期过后放回重试
_FAIL_THRESHOLD = 3          # 连续失败次数
_COOLDOWN_SECONDS = 60.0      # 熔断冷却时长


class ApiKeyManager:
    """Key 池管理。

    从 config.json 的 llm_keys 段加载，按 type 过滤。
    并发上限从 providers.<role>.max_concurrency 读取。
    调用失败可经 report_failure 上报，连续失败达阈值的 key 临时熔断（冷却期内
    acquire 跳过），避免故障 key 持续毒化请求。
    """

    def __init__(self, role: str) -> None:
        self._role = role
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._entries: list[dict] = []
        self._cursor = 0
        self._load_sync()

    def _load_sync(self) -> None:
        """同步加载配置（构造时调用，此时可能没有事件循环）。"""
        keys = get_keys_for_role(self._role)
        concurrency = int(get_config(f"providers.{self._role}.max_concurrency", 8))

        self._entries = [
            {
                **key,
                "concurrency": max(1, concurrency),
                "in_use": 0,
                "fail_count": 0,
                "cooldown_until": 0.0,
            }
            for key in keys
        ]
        self._cursor = 0
        logger.info(
            "ApiKeyManager loaded",
            extra={"role": self._role, "keys": len(self._entries), "total_concurrency": self.total_concurrency_sync},
        )

    def reload(self) -> None:
        """UI 改了 key 配置后重建（密钥从 .env 重新读）。

        保留在途 key 的运行时状态（in_use/fail_count/cooldown_until），
        按 id 合并到新 entries，避免在途请求 release 时改到孤儿对象、
        导致新列表并发名额永不释放。
        """
        # 以 id 为键保留旧运行时状态
        old_state: dict[str, dict] = {}
        for e in self._entries:
            old_state[e.get("id", "")] = {
                "in_use": e.get("in_use", 0),
                "fail_count": e.get("fail_count", 0),
                "cooldown_until": e.get("cooldown_until", 0.0),
            }

        keys = get_keys_for_role(self._role)
        concurrency = int(get_config(f"providers.{self._role}.max_concurrency", 8))

        self._entries = []
        for key in keys:
            entry = {
                **key,
                "concurrency": max(1, concurrency),
                "in_use": 0,
                "fail_count": 0,
                "cooldown_until": 0.0,
            }
            # 合并同 id 旧状态
            saved = old_state.get(key.get("id", ""))
            if saved:
                entry["in_use"] = min(saved["in_use"], entry["concurrency"])
                entry["fail_count"] = saved["fail_count"]
                entry["cooldown_until"] = saved["cooldown_until"]
            self._entries.append(entry)
        self._cursor = 0
        logger.info(
            "ApiKeyManager reloaded",
            extra={"role": self._role, "keys": len(self._entries), "total_concurrency": self.total_concurrency_sync},
        )

    @property
    def total_concurrency_sync(self) -> int:
        return sum(e["concurrency"] for e in self._entries)

    @property
    def available(self) -> bool:
        return len(self._entries) > 0

    def _is_available(self, entry: dict, now: float) -> bool:
        """entry 是否可分配：有并发余量 且 未在冷却期。"""
        return entry["in_use"] < entry["concurrency"] and entry["cooldown_until"] <= now

    async def acquire(self, timeout: float = 30.0) -> dict | None:
        """异步获取一个有空闲名额且未熔断的 key 条目。全占满则等待，超时返回 None。"""
        deadline = time.time() + timeout
        async with self._cond:
            if not self._entries:
                return None
            while True:
                now = time.time()
                n = len(self._entries)
                for _ in range(n):
                    entry = self._entries[self._cursor % n]
                    self._cursor = (self._cursor + 1) % n
                    if self._is_available(entry, now):
                        entry["in_use"] += 1
                        return entry
                remaining = deadline - time.time()
                if remaining <= 0:
                    logger.warning("ApiKeyManager acquire timeout", extra={"role": self._role})
                    return None
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    logger.warning("ApiKeyManager acquire timeout", extra={"role": self._role})
                    return None

    async def release(self, entry: dict, success: bool | None = None) -> None:
        """归还 key 条目。

        Args:
            entry: acquire 返回的条目。
            success: 调用结果。True 清零失败计数；False 计入失败，达阈值则熔断。
                     None（默认）仅归还并发名额，不改动故障状态（向后兼容）。
        """
        async with self._cond:
            if entry.get("in_use", 0) > 0:
                entry["in_use"] -= 1
            if success is True:
                if entry["fail_count"] > 0:
                    entry["fail_count"] = 0
            elif success is False:
                entry["fail_count"] += 1
                if entry["fail_count"] >= _FAIL_THRESHOLD:
                    entry["cooldown_until"] = time.time() + _COOLDOWN_SECONDS
                    logger.warning(
                        "ApiKeyManager key circuit-opened (role=%s, id=%s, fails=%d, cooldown=%.0fs)",
                        self._role, entry.get("id", "?"), entry["fail_count"], _COOLDOWN_SECONDS,
                    )
            self._cond.notify()

    async def report_failure(self, entry: dict) -> None:
        """上报调用失败（等价于 release(entry, success=False)）。"""
        await self.release(entry, success=False)

    async def report_success(self, entry: dict) -> None:
        """上报调用成功（等价于 release(entry, success=True)）。"""
        await self.release(entry, success=True)
