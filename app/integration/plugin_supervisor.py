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
                 env_per_plugin: dict[str, dict[str, str]] | None = None,
                 host_registry=None, on_plugin_stopped=None) -> None:
        self._rpc_timeout = rpc_timeout
        self._max_restarts = max_restarts
        # 每个插件的环境变量注入（如小爱需要 HA 凭证）
        self._env_per_plugin = env_per_plugin or {}
        # 方向 2 反向方法注册表（Phase 3）：透传给每个 PluginProcess
        self._host_registry = host_registry
        # 插件停止回调：透传给 PluginProcess，宿主据此清理插件注册的资源
        self._on_plugin_stopped = on_plugin_stopped
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
                # 熔断告警（内部吞异常；插件功能静默失效是家庭产品最典型的
                # "坏了没人知道"场景）
                try:
                    from ..services.alert_service import alert_service
                    await alert_service.notify(
                        f"plugin:{manifest.id}",
                        f"插件「{manifest.name or manifest.id}」多次启动失败已熔断，"
                        f"其功能（如语音播报/消息推送）将不可用")
                except Exception:  # noqa: BLE001
                    pass

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
                host_registry=self._host_registry,
                on_stopped=self._on_plugin_stopped,
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

    async def start_one(self, manifest: Manifest, plugin_dir: str) -> bool:
        """运行时启动单个插件进程（用于热启动：启用已禁用的插件）。

        与 _start_with_retries 同样的退避重试逻辑，但只针对一个插件。
        返回 True 成功，False 失败（重试耗尽）。
        借鉴 OpenClaw：启用=热加载，子进程天然隔离，无需原子交换注册表。
        """
        try:
            await self._start_with_retries(manifest, plugin_dir)
            return True
        except Exception as exc:
            logger.error("插件 %s 热启动失败（已重试 %d 次）: %s",
                         manifest.id, self._max_restarts, exc)
            return False

    def get_process(self, plugin_id: str) -> PluginProcess | None:
        return self._processes.get(plugin_id)

    def get_running_manifests(self) -> list[Manifest]:
        return list(self._manifests.values())
