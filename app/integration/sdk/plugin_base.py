"""IntegrationPlugin 基类 —— 插件进程内继承。"""

from typing import Any

from ..rpc_protocol import (
    METHOD_HOST_BROADCAST,
    METHOD_HOST_HA_CALL,
    METHOD_HOST_HA_DEVICES,
    METHOD_HOST_HA_STATES,
    METHOD_HOST_LLM_CHAT,
    METHOD_INTERRUPT,
    METHOD_ROUTE,
    METHOD_SPEAK,
)
from .sink_base import OutputSink

# JSON-RPC method → 需要的 capability 类型映射。
# handle() 调用前校验 manifest 是否声明了对应 capability，
# 防止插件执行未声明的能力（capability 弱强制）。
_METHOD_CAPABILITY: dict[str, str] = {
    METHOD_SPEAK: "output_sink",
    METHOD_INTERRUPT: "output_sink",
    METHOD_ROUTE: "inbound_router",
}


class _HostHA:
    """host.ha 子代理：设备控制类反向调用。"""

    def __init__(self, host_call) -> None:
        self._call = host_call

    async def call_service(self, domain: str, service: str,
                           entity_id: str | None = None, data: dict | None = None) -> dict:
        return await self._call(METHOD_HOST_HA_CALL, {
            "domain": domain, "service": service,
            "entity_id": entity_id, "data": data,
        })

    async def get_states(self) -> dict:
        return await self._call(METHOD_HOST_HA_STATES, {})

    async def get_devices_grouped(self) -> dict:
        return await self._call(METHOD_HOST_HA_DEVICES, {})


class _HostLLM:
    """host.llm 子代理：LLM 对话反向调用。"""

    def __init__(self, host_call) -> None:
        self._call = host_call

    async def chat(self, messages: list, timeout: float | None = None) -> dict:
        return await self._call(METHOD_HOST_LLM_CHAT, {"messages": messages, "timeout": timeout})


class HostProxy:
    """插件反向调用宿主能力的代理。

    runtime 在 ``setup`` **之前** 注入到 ``plugin.host``，插件在 setup 里即可用
    ``self.host.ha.call_service(...)`` 等（小爱 setup 据此构造 sink）。方法名映射到
    ``rpc_protocol`` 的方向 2 METHOD 常量，宿主 HostMethodRegistry 按权限校验后 dispatch。
    """

    def __init__(self, host_call) -> None:
        self._call = host_call
        self.ha = _HostHA(host_call)
        self.llm = _HostLLM(host_call)

    async def broadcast(self, text: str, msg_id: str = "") -> dict:
        return await self._call(METHOD_HOST_BROADCAST, {"text": text, "msg_id": msg_id})


class IntegrationPlugin:
    """插件基类。

    子类在 setup() 里根据 manifest 构建 sinks（output_sink）和
    routers（inbound_router）。
    handle() 按 JSON-RPC method 路由到对应能力，并校验 manifest 是否
    声明了该方法需要的 capability（未声明则拒绝，防越权）。
    """

    def __init__(self) -> None:
        self.manifest: dict[str, Any] = {}
        self.sinks: list[OutputSink] = []
        self.routers: list[Any] = []  # list[InboundRouter]，用 Any 避免循环导入
        # 宿主反向调用代理：runtime 在 setup 前注入；未注入时为 None（旧部署兼容）。
        self.host: HostProxy | None = None

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        """子类实现：解析 manifest_dict，构建 sinks/routers 等。"""
        self.manifest = manifest_dict

    def _declared_capabilities(self) -> set[str]:
        """从 manifest 提取已声明的 capability 类型集合。"""
        caps = self.manifest.get("capabilities", []) or []
        return {c.get("type", "") for c in caps if isinstance(c, dict)}

    async def handle(self, method: str, params: dict[str, Any]) -> dict:
        """按 method 分发到对应能力。未知方法返回 error。

        先校验 manifest 是否声明了该方法需要的 capability（弱强制），
        未声明则拒绝——防止插件执行越权操作。
        """
        required_cap = _METHOD_CAPABILITY.get(method)
        if required_cap and required_cap not in self._declared_capabilities():
            return {"error": f"capability '{required_cap}' not declared in manifest"}

        if method == METHOD_SPEAK:
            if not self.sinks:
                return {"error": "no sink registered"}
            sink = self.sinks[0]
            return await sink.speak(
                text=params.get("text", ""),
                msg_id=params.get("msg_id", ""),
            )
        if method == METHOD_INTERRUPT:
            if not self.sinks:
                return {"error": "no sink registered"}
            sink = self.sinks[0]
            return await sink.interrupt()
        if method == METHOD_ROUTE:
            if not self.routers:
                return {"error": "no router registered"}
            router = self.routers[0]
            return await router.route(text=params.get("text", ""))
        return {"error": f"unknown method: {method}"}
