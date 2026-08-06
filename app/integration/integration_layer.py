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
        broadcast_enabled: bool = True,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._api_version = api_version
        self._supervisor = PluginSupervisor(
            rpc_timeout=rpc_timeout, max_restarts=max_restarts,
            env_per_plugin=env_per_plugin,
        )
        self.sink_manager = SinkManager(self._supervisor,
                                        broadcast_enabled=broadcast_enabled)
        self._started = False

    async def start(self) -> None:
        """加载清单 + 启动所有插件进程（跳过禁用的）。"""
        from .config_helper import get_disabled_plugins
        disabled = get_disabled_plugins()
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version,
                                   disabled=disabled)
        logger.info("发现 %d 个集成插件（%d 个禁用）: %s",
                    len(manifests), len(disabled),
                    [m.id for m in manifests])
        await self._supervisor.start_all(manifests, self._plugin_dir)
        self._started = True

    async def stop(self) -> None:
        """停止所有插件进程。"""
        self._started = False
        await self._supervisor.stop_all()

    def list_plugins(self) -> list[dict]:
        """返回插件状态摘要（供 API 查询，含禁用态）。"""
        from .manifest_loader import load_all_manifests
        from .config_helper import get_disabled_plugins
        manifests = load_all_manifests(self._plugin_dir, api_version=self._api_version)
        disabled = set(get_disabled_plugins())
        result = []
        for m in manifests:
            proc = self._supervisor.get_process(m.id)
            result.append({
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "description": m.description,
                "capabilities": [c.type.value for c in m.capabilities],
                "alive": proc.is_alive if proc is not None else False,
                "enabled": m.id not in disabled,  # 禁用态
            })
        return result

    def set_broadcast_enabled(self, enabled: bool) -> None:
        """运行时切换全局广播开关（同时写 config 持久化）。"""
        self.sink_manager.broadcast_enabled = bool(enabled)
        try:
            from .config_helper import set_broadcast_enabled as persist
            persist(bool(enabled))
        except Exception as exc:
            logger.warning("广播开关持久化失败（内存状态已更新）: %s", exc)

    def list_ui_contributions(self) -> list[dict]:
        """扫描所有插件的 ui_contribution，合并返回（带 plugin_id）。

        跳过禁用插件（禁用的不贡献 UI）。没插件或全禁用 → 空列表 → 前端无 UI。
        """
        from .config_helper import get_disabled_plugins
        disabled = set(get_disabled_plugins())
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version,
                                   disabled=list(disabled))
        result = []
        for manifest in manifests:
            for ui in manifest.ui_contributions:
                result.append({
                    "plugin_id": manifest.id,
                    "slot": ui.slot,
                    "type": ui.type,
                    "props": ui.props,
                    "state_key": ui.state_key,
                    "action": ui.action,
                })
        return result

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> None:
        """启用/禁用插件（持久化到 config）。

        禁用 = 下次启动不加载该插件进程。本次运行中已启动的进程不停（需重启生效）。
        """
        from .config_helper import set_plugin_disabled
        set_plugin_disabled(plugin_id, not enabled)
