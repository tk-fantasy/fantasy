"""IntegrationLayer —— 集成平台门面。

组装 manifest_loader + plugin_supervisor + sink_manager。
挂到 AppContainer，由 main.py lifespan 启停。
"""

import logging

from .manifest_loader import load_manifests
from .plugin_supervisor import PluginSupervisor
from .sink_manager import SinkManager

logger = logging.getLogger(__name__)


class IntegrationLayer:
    """集成平台门面。

    start() 加载清单并启动所有插件进程；stop() 停止所有进程。
    sink_manager 暴露给 Dispatcher 做广播钩子。
    """

    def __init__(
        self,
        plugin_dir: str,
        api_version: str = "1",
        rpc_timeout: float = 30.0,
        max_restarts: int = 3,
        env_per_plugin: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._api_version = api_version
        self._supervisor = PluginSupervisor(
            rpc_timeout=rpc_timeout, max_restarts=max_restarts,
            env_per_plugin=env_per_plugin,
        )
        self.sink_manager = SinkManager(self._supervisor)
        self._started = False

    async def start(self) -> None:
        """加载清单 + 启动所有插件进程。"""
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version)
        logger.info("发现 %d 个集成插件: %s",
                    len(manifests), [m.id for m in manifests])
        await self._supervisor.start_all(manifests, self._plugin_dir)
        self._started = True

    async def stop(self) -> None:
        """停止所有插件进程。"""
        self._started = False
        await self._supervisor.stop_all()

    def list_plugins(self) -> list[dict]:
        """返回插件状态摘要（供 API 查询）。"""
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version)
        result = []
        for m in manifests:
            proc = self._supervisor.get_process(m.id)
            result.append({
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "capabilities": [c.type.value for c in m.capabilities],
                "alive": proc.is_alive if proc is not None else False,
            })
        return result
