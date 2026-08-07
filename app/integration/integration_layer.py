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

        注意：此方法只持久化状态，不立即停止/启动进程。
        要立即生效用 stop_plugin / 需重启启动。
        """
        from .config_helper import set_plugin_disabled
        set_plugin_disabled(plugin_id, not enabled)

    async def stop_plugin(self, plugin_id: str) -> bool:
        """运行时停止某插件进程（禁用时调用，立即生效）。

        持久化禁用状态 + 停止运行中的进程。
        返回是否有进程被停止。
        """
        self.set_plugin_enabled(plugin_id, enabled=False)
        return await self._supervisor.stop_one(plugin_id)

    async def start_plugin(self, plugin_id: str) -> bool:
        """运行时启动某插件进程（热启动：启用已禁用的插件）。

        持久化启用状态 + 热启动进程。
        子进程天然隔离，无需 OpenClaw 那样的原子注册表交换。
        返回是否启动成功。
        """
        # 找 manifest（从全部已安装的里找，含禁用的）
        from .manifest_loader import load_all_manifests
        manifests = load_all_manifests(self._plugin_dir, api_version=self._api_version)
        target = next((m for m in manifests if m.id == plugin_id), None)
        if target is None:
            return False
        self.set_plugin_enabled(plugin_id, enabled=True)
        return await self._supervisor.start_one(target, self._plugin_dir)

    async def route_inbound(self, text: str, mode: str) -> dict:
        """将入站文字路由到声明 inbound_router 的插件（通用，不硬编码插件名）。

        找第一个声明了 inbound_router 且存活的插件，RPC 调 router.handle。
        无插件 / 全禁用 → 返回 {ok: False, error: ...}。
        V1 只有一个 inbound_router（小爱），直接调第一个匹配。
        """
        from .config_helper import get_disabled_plugins
        from .manifest_loader import load_manifests
        from .rpc_protocol import METHOD_ROUTE
        from .schema import CapabilityType

        disabled = get_disabled_plugins()
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version,
                                   disabled=disabled)
        for manifest in manifests:
            if manifest.has_capability(CapabilityType.INBOUND_ROUTER):
                proc = self._supervisor.get_process(manifest.id)
                if proc and proc.is_alive:
                    try:
                        return await proc.call(METHOD_ROUTE, {"text": text, "mode": mode})
                    except Exception as exc:
                        logger.warning("路由到插件 %s 失败: %s", manifest.id, exc)
                        return {"ok": False, "error": f"插件 {manifest.id} 路由失败"}
        return {"ok": False, "error": "no inbound router available"}
