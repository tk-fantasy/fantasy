"""IntegrationPlugin 基类 —— 插件进程内继承。"""

from typing import Any

from ..rpc_protocol import (
    METHOD_HOST_BROADCAST,
    METHOD_HOST_CAM_PUSH,
    METHOD_HOST_CAM_REGISTER,
    METHOD_HOST_CAM_SET_FLAGS,
    METHOD_HOST_CAM_UNREGISTER,
    METHOD_HOST_HA_CALL,
    METHOD_HOST_HA_DEVICES,
    METHOD_HOST_HA_STATES,
    METHOD_HOST_LLM_CHAT,
    METHOD_INTERRUPT,
    METHOD_ROUTE,
    METHOD_SHUTDOWN,
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


class _HostCamera:
    """host.camera 子代理：虚拟摄像头注册/帧推送反向调用。

    manifest 声明 permissions=["camera"] 后可用。plugin_id 由宿主注入
    （插件不传、也无法伪造），register 返回确定性 camera_id（vcam_<plugin_id>）。
    """

    def __init__(self, host_call) -> None:
        self._call = host_call

    async def register(self, spec: dict | None = None) -> dict:
        return await self._call(METHOD_HOST_CAM_REGISTER, {"spec": spec or {}})

    async def push_frame(self, camera_id: str, jpeg_b64: str) -> dict:
        return await self._call(METHOD_HOST_CAM_PUSH, {
            "camera_id": camera_id, "jpeg_b64": jpeg_b64,
        })

    async def unregister(self) -> dict:
        return await self._call(METHOD_HOST_CAM_UNREGISTER, {})

    async def set_flags(self, camera_id: str, flags: dict) -> dict:
        return await self._call(METHOD_HOST_CAM_SET_FLAGS, {
            "camera_id": camera_id, "flags": flags or {},
        })


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
        self.camera = _HostCamera(host_call)

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
        # 插件自定义 RPC 方法表（method → handler）。
        # 宿主经 POST /api/integrations/{id}/method/{method} 转发面板交互，
        # 插件在 setup 里 register_method 注册处理函数（签名 async fn(params)->dict）。
        self._custom_methods: dict[str, Any] = {}

    def register_method(self, method: str, handler) -> None:
        """注册一个自定义 RPC 方法（宿主经通用 method 路由转发调用）。

        method 建议带插件前缀命名空间（如 "videos.list"），避免与框架方法撞名。
        """
        self._custom_methods[method] = handler

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        """子类实现：解析 manifest_dict，构建 sinks/routers 等。"""
        self.manifest = manifest_dict

    async def on_shutdown(self) -> None:
        """优雅停止钩子：宿主 plugin_process.stop 发 shutdown 通知时被调（默认 no-op）。

        子类可覆写做清理（冲刷缓冲、断开长连接等）。随后的 stdin 关闭才是
        真正的停止信号——清理应快速完成，不要长时间阻塞。
        """

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

        # 插件自定义方法优先（面板交互经宿主通用 method 路由转发到此）
        custom = self._custom_methods.get(method)
        if custom is not None:
            return await custom(params or {})

        if method == METHOD_SHUTDOWN:
            # 停止通知：调 on_shutdown 钩子后应答 ok。宿主 stop 流程先发本方法再关
            # stdin，子类覆写 on_shutdown 做快速清理；stdin 关闭才是真正停止信号。
            try:
                await self.on_shutdown()
            except Exception as exc:  # 清理失败不阻塞停止流程，stderr 已有栈
                import sys
                print(f"[{self.manifest.get('id', '?')}] on_shutdown error: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return {"ok": True}

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
        if method == METHOD_SHUTDOWN:
            # 优雅停止通知（无需 capability）。插件可用 register_method 覆盖
            # 自定义行为（custom 分支在上面已优先返回）。
            await self.on_shutdown()
            return {"ok": True}
        return {"error": f"unknown method: {method}"}
