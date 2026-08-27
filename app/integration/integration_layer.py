"""IntegrationLayer —— 集成平台门面。

组装 manifest_loader + plugin_supervisor + sink_manager。
挂到 AppContainer，由 main.py lifespan 启停。
"""

import logging

from .host_registry import HostMethodRegistry
from .manifest_loader import load_manifests
from .plugin_supervisor import PluginSupervisor
from .rpc_protocol import (
    METHOD_HOST_BROADCAST,
    METHOD_HOST_CAM_PUSH,
    METHOD_HOST_CAM_REGISTER,
    METHOD_HOST_CAM_SET_FLAGS,
    METHOD_HOST_CAM_UNREGISTER,
    METHOD_HOST_HA_CALL,
    METHOD_HOST_HA_DEVICES,
    METHOD_HOST_HA_STATES,
    METHOD_HOST_LLM_CHAT,
)
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
        host_deps: dict | None = None,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._api_version = api_version
        # 方向 2 反向方法注册表：先建空表传给 supervisor（PluginProcess 持同一引用），
        # sink_manager 构造后再注册 handler（broadcast handler 需要 sink_manager）。
        self._host_registry = HostMethodRegistry()
        camera_manager = (host_deps or {}).get("camera_manager")

        # 注销任务强引用（loop.create_task 只持弱引用，防 GC 中途回收）
        unregister_tasks: set = set()

        def _on_plugin_stopped(plugin_id: str) -> None:
            """插件进程停止（含崩溃回收）→ 注销其注册的虚拟摄像头。

            崩溃重启时插件 setup 会重新 register，注销-重注册闭环。
            """
            if camera_manager is None:
                return
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                t = loop.create_task(camera_manager.unregister_plugin_cameras(plugin_id))
                unregister_tasks.add(t)
                t.add_done_callback(unregister_tasks.discard)
            except Exception:  # noqa: BLE001
                logger.warning("注销插件 %s 虚拟摄像头失败", plugin_id, exc_info=True)

        self._supervisor = PluginSupervisor(
            rpc_timeout=rpc_timeout, max_restarts=max_restarts,
            env_per_plugin=env_per_plugin,
            host_registry=self._host_registry,
            on_plugin_stopped=_on_plugin_stopped,
        )
        self.sink_manager = SinkManager(self._supervisor,
                                        broadcast_enabled=broadcast_enabled)
        # 保留 host_deps 供 HA 配置热替换后重绑（update_ha_refs）
        self._host_deps = host_deps or {}
        self._register_host_methods(host_deps)
        # 宿主侧集成注册表（非子进程插件，如飞书 WebSocket 长连接）
        # key=集成名, value={"name","description","alive"}
        self.host_integrations: dict[str, dict] = {}

    def update_ha_refs(self, new_client, new_service) -> None:
        """HA 配置热替换后重绑反向 RPC 的 ha handler（main.sync_ha_runtime_refs 调用）。

        _register_host_methods 的闭包捕获构造时的 client/service 对象，
        旧对象被 close 后插件反向调用会持续失败。重跑一遍注册（register
        为字典覆盖语义，幂等）让闭包捕获新对象。
        """
        if not self._host_deps:
            return
        self._host_deps["ha_client"] = new_client
        self._host_deps["ha_service"] = new_service
        self._register_host_methods(self._host_deps)

    def _register_host_methods(self, host_deps: dict | None) -> None:
        """注册方向 2 宿主能力到 host_registry。

        插件在 manifest.permissions 声明对应权限（ha/llm/broadcast）后，才能反向调用。
        host_deps=None（如 e2e 测试）→ 不注册任何 handler，反向调用回 "未知方法" 错误。
        """
        if not host_deps:
            return
        ha_client = host_deps.get("ha_client")
        ha_service = host_deps.get("ha_service")
        llm_chat_client = host_deps.get("llm_chat_client")
        camera_manager = host_deps.get("camera_manager")
        reg = self._host_registry

        if ha_client is not None:
            async def _ha_call(params: dict) -> dict:
                return await ha_client.call_service(
                    params.get("domain"), params.get("service"),
                    params.get("entity_id"), params.get("data"),
                )
            reg.register(METHOD_HOST_HA_CALL, _ha_call, required_permission="ha")

        if ha_service is not None:
            async def _ha_states(params: dict) -> dict:
                return {"states": await ha_service.get_states_snapshot()}
            async def _ha_devices(params: dict) -> dict:
                return await ha_service.get_all_devices_grouped()
            reg.register(METHOD_HOST_HA_STATES, _ha_states, required_permission="ha")
            reg.register(METHOD_HOST_HA_DEVICES, _ha_devices, required_permission="ha")

        if llm_chat_client is not None:
            async def _llm_chat(params: dict) -> dict:
                text = await llm_chat_client.chat(
                    params.get("messages") or [], params.get("timeout") or 120,
                )
                return {"text": text}
            reg.register(METHOD_HOST_LLM_CHAT, _llm_chat, required_permission="llm")

        async def _broadcast(params: dict) -> dict:
            await self.sink_manager.broadcast(
                params.get("text", ""), params.get("msg_id", ""),
            )
            return {"ok": True}
        reg.register(METHOD_HOST_BROADCAST, _broadcast, required_permission="broadcast")

        if camera_manager is not None:
            async def _cam_register(params: dict) -> dict:
                # plugin_id 由 PluginProcess 分发时注入（params["_plugin_id"]），
                # 插件无法伪造他人 id 注册。
                pid = str(params.get("_plugin_id") or "")
                if not pid:
                    raise RuntimeError("camera.register: missing plugin identity")
                spec = params.get("spec") or {}
                return await camera_manager.register_virtual_camera(pid, spec)

            async def _cam_push(params: dict) -> dict:
                return camera_manager.push_frame(
                    str(params.get("camera_id", "")), str(params.get("jpeg_b64", ""))
                )

            async def _cam_unregister(params: dict) -> dict:
                pid = str(params.get("_plugin_id") or params.get("plugin_id") or "")
                if not pid:
                    raise RuntimeError("camera.unregister: missing plugin identity")
                ok = await camera_manager.unregister_plugin_cameras(pid)
                return {"ok": ok}

            async def _cam_set_flags(params: dict) -> dict:
                ok = camera_manager.set_virtual_flag(
                    str(params.get("camera_id", "")),
                    str(params.get("key", "")),
                    params.get("value"),
                )
                return {"ok": ok}

            reg.register(METHOD_HOST_CAM_REGISTER, _cam_register, required_permission="camera")
            reg.register(METHOD_HOST_CAM_PUSH, _cam_push, required_permission="camera")
            reg.register(METHOD_HOST_CAM_UNREGISTER, _cam_unregister, required_permission="camera")
            reg.register(METHOD_HOST_CAM_SET_FLAGS, _cam_set_flags, required_permission="camera")

    async def start(self) -> None:
        """加载清单 + 启动所有插件进程（跳过禁用的）。

        进程内能力插件（model_adapter 等，manifest.needs_subprocess=False）
        不 spawn 子进程——其能力由宿主在本进程内加载
        （model_family_adapters 懒加载），这里只拉需要子进程的。
        """
        from .config_helper import get_disabled_plugins
        disabled = get_disabled_plugins()
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version,
                                   disabled=disabled)
        subprocess_plugins = [m for m in manifests if m.needs_subprocess]
        logger.info("发现 %d 个集成插件（%d 个禁用，%d 个进程内）: %s",
                    len(manifests), len(disabled),
                    len(manifests) - len(subprocess_plugins),
                    [m.id for m in manifests])
        await self._supervisor.start_all(subprocess_plugins, self._plugin_dir)

    async def stop(self) -> None:
        """停止所有插件进程。"""
        await self._supervisor.stop_all()

    def list_plugins(self) -> list[dict]:
        """返回插件状态摘要（供 API 查询，含禁用态 + 宿主侧集成）。"""
        from .manifest_loader import load_all_manifests
        from .config_helper import get_disabled_plugins, get_host_config
        manifests = load_all_manifests(self._plugin_dir, api_version=self._api_version)
        disabled = set(get_disabled_plugins())
        result = []
        # 子进程插件
        for m in manifests:
            proc = self._supervisor.get_process(m.id)
            # 汇总各 capability 的 config_schema（管理页弹窗渲染配置表单用）
            schema: dict = {}
            for cap in m.capabilities:
                if cap.config_schema:
                    schema.update(cap.config_schema)
            result.append({
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "description": m.description,
                "capabilities": [c.type.value for c in m.capabilities],
                "alive": proc.is_alive if proc is not None else False,
                "enabled": m.id not in disabled,  # 禁用态
                "config_schema": schema,
                "has_config_set": bool(get_host_config(m.id)),
            })
        # 宿主侧集成（非子进程，如飞书长连接）
        for integ_id, info in self.host_integrations.items():
            result.append({
                "id": integ_id,
                "name": info.get("name", integ_id),
                "version": info.get("version", ""),
                "description": info.get("description", ""),
                "capabilities": info.get("capabilities", []),
                "alive": info.get("alive", False),
                "enabled": True,  # 宿主侧集成的启停通过凭证控制
                "config_schema": info.get("config_schema", {}),
                "has_config_set": bool(get_host_config(integ_id)),
            })
        return result

    def register_host_integration(self, integ_id: str, info: dict) -> None:
        """注册一个宿主侧集成（供 list_plugins 显示）。

        Args:
            integ_id: 集成名（如 "feishu"）
            info: {"name","description","alive","capabilities",...}
        """
        self.host_integrations[integ_id] = info

    async def restart_subprocess_plugin(self, plugin_id: str) -> bool:
        """停止并重启一个子进程插件（管理页改配置后让它按新配置 setup）。

        禁用态的插件只保证停（不启动）。返回 False=插件不存在。
        进程内能力插件无进程可重启，直接返回（其行为经注册表懒加载）。
        """
        from .manifest_loader import load_all_manifests
        from .config_helper import get_disabled_plugins
        manifests = {m.id: m for m in
                     load_all_manifests(self._plugin_dir, api_version=self._api_version)}
        if plugin_id not in manifests:
            return False
        await self._supervisor.stop_one(plugin_id)
        if plugin_id in set(get_disabled_plugins()):
            return True
        if not manifests[plugin_id].needs_subprocess:
            return True
        return await self._supervisor.start_one(manifests[plugin_id], self._plugin_dir)

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
        进程内能力插件（model_adapter）无进程可停启，
        启停即重扫适配器注册表（热生效，无需重启 Aether）。
        """
        from .config_helper import set_plugin_disabled
        set_plugin_disabled(plugin_id, not enabled)
        try:
            from ..agents.model_family_adapters import refresh_plugin_adapters
            refresh_plugin_adapters()
        except Exception:
            logger.debug("刷新模型家族适配器失败（可能未启用该能力）",
                         exc_info=True)

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
        进程内能力插件（model_adapter）无进程可启动——启用即重扫
        适配器注册表（set_plugin_enabled 内已热刷新），直接返回成功。
        返回是否启动成功。
        """
        # 找 manifest（从全部已安装的里找，含禁用的）
        from .manifest_loader import load_all_manifests
        manifests = load_all_manifests(self._plugin_dir, api_version=self._api_version)
        target = next((m for m in manifests if m.id == plugin_id), None)
        if target is None:
            return False
        self.set_plugin_enabled(plugin_id, enabled=True)
        if not target.needs_subprocess:
            return True
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
