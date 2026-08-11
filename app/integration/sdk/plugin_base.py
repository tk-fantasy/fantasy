"""IntegrationPlugin 基类 —— 插件进程内继承。"""

from typing import Any

from ..rpc_protocol import METHOD_INTERRUPT, METHOD_ROUTE, METHOD_SPEAK
from .sink_base import OutputSink

# JSON-RPC method → 需要的 capability 类型映射。
# handle() 调用前校验 manifest 是否声明了对应 capability，
# 防止插件执行未声明的能力（capability 弱强制）。
_METHOD_CAPABILITY: dict[str, str] = {
    METHOD_SPEAK: "output_sink",
    METHOD_INTERRUPT: "output_sink",
    METHOD_ROUTE: "inbound_router",
}


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
