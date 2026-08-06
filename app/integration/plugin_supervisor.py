"""插件进程生命周期管理：启动 / 退避重启 / 熔断。

崩溃后按指数退避重启，max_restarts 次后熔断（不进 running 列表）。
单个插件失败不阻塞其他插件启动。
"""

import asyncio
import logging

from .plugin_process import PluginProcess
from .schema import Manifest

logger = logging.getLogger(__name__)


class PluginSupervisor:
    """管理多个插件进程的启动与停止。

    职责单一：只管 spawn/stop/重试。崩溃检测的主动监控（心跳熔断）留给 Phase 5。
    """

    def __init__(self, rpc_timeout: float = 30.0, max_restarts: int = 3,
                 env_per_plugin: dict[str, dict[str, str]] | None = None) -> None:
        self._rpc_timeout = rpc_timeout
        self._max_restarts = max_restarts
        # 每个插件的环境变量注入（如小爱需要 HA 凭证）
        self._env_per_plugin = env_per_plugin or {}
        self._processes: dict[str, PluginProcess] = {}  # plugin_id → process
        self._manifests: dict[str, Manifest] = {}

    async def start_all(self, manifests: list[Manifest], plugin_dir: str) -> None:
        """启动给定清单列表对应的进程。失败的跳过（不阻塞其他）。"""
        for manifest in manifests:
            try:
                await self._start_with_retries(manifest, plugin_dir)
            except Exception as exc:
                logger.error("插件 %s 启动失败（已重试 %d 次，已熔断）: %s",
                             manifest.id, self._max_restarts, exc)

    async def _start_with_retries(self, manifest: Manifest, plugin_dir: str) -> None:
        """指数退避重试启动单个插件。超过 max_restarts 抛异常（由调用方记录）。"""
        backoff = 1.0
        attempts = 0
        env = self._env_per_plugin.get(manifest.id)
        # attempts 上限 = max_restarts（首次 + 重试次数 = max_restarts 次尝试）
        while attempts <= self._max_restarts:
            proc = PluginProcess(
                manifest=manifest,
                plugin_root=f"{plugin_dir}/{manifest.id}",
                rpc_timeout=self._rpc_timeout,
                env=env,
            )
            try:
                await proc.start()
                self._processes[manifest.id] = proc
                self._manifests[manifest.id] = manifest
                return
            except Exception as exc:
                attempts += 1
                logger.warning("插件 %s 启动失败（第 %d 次尝试）: %s",
                               manifest.id, attempts, exc)
                if attempts > self._max_restarts:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop_all(self) -> None:
        """停止所有进程。"""
        procs = list(self._processes.values())
        self._processes.clear()
        self._manifests.clear()
        for proc in procs:
            try:
                await proc.stop()
            except Exception as exc:
                logger.warning("停止插件 %s 出错: %s", proc.manifest.id, exc)

    async def stop_one(self, plugin_id: str) -> bool:
        """停止单个插件进程（用于运行时禁用）。

        返回 True 表示有进程被停止，False 表示该插件未运行。
        """
        proc = self._processes.pop(plugin_id, None)
        self._manifests.pop(plugin_id, None)
        if proc is None:
            return False
        try:
            await proc.stop()
            return True
        except Exception as exc:
            logger.warning("停止插件 %s 出错: %s", plugin_id, exc)
            return False

    def get_process(self, plugin_id: str) -> PluginProcess | None:
        return self._processes.get(plugin_id)

    def get_running_manifests(self) -> list[Manifest]:
        return list(self._manifests.values())
