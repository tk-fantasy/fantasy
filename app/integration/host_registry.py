"""宿主反向方法注册表（方向 2：插件 → Aether）。

IntegrationLayer 在构造时把宿主能力注册为 (method → handler, required_permission)；
plugin_process 收到插件发来的反向请求时，按 manifest.permissions 校验权限后 dispatch。

handler 签名统一为 ``async def handler(params: dict) -> dict``，由 IntegrationLayer
用闭包绑定具体的 ha_client / ha_service / llm_chat_client / sink_manager。
"""

import logging

logger = logging.getLogger(__name__)


class HostMethodRegistry:
    """method → (handler, required_permission)。

    - 未知方法 → ``RuntimeError``（回传 JSON-RPC error）。
    - ``required_permission`` 不在 ``manifest.permissions`` 中 → ``PermissionError``
      （回传权限错误，防越权：插件只能调声明了权限的宿主能力）。
    - 否则 ``await handler(params)``，结果回传给插件。
    """

    def __init__(self) -> None:
        self._methods: dict[str, tuple] = {}

    def register(
        self,
        method: str,
        handler,
        required_permission: str | None = None,
    ) -> None:
        """注册一个反向方法。

        Args:
            method: 方法名（如 rpc_protocol.METHOD_HOST_HA_CALL）。
            handler: ``async def handler(params: dict) -> dict``。
            required_permission: 调用此方法需要的权限标识（如 "ha"/"llm"/"broadcast"）；
                None 表示无需权限校验。
        """
        self._methods[method] = (handler, required_permission)

    async def dispatch(self, manifest, method: str, params: dict) -> dict:
        entry = self._methods.get(method)
        if entry is None:
            raise RuntimeError(f"未知反向方法: {method}")
        handler, required_perm = entry
        if required_perm is not None:
            granted = set(manifest.permissions or [])
            if required_perm not in granted:
                raise PermissionError(
                    f"插件 {manifest.id} 未声明权限 '{required_perm}'，拒绝 {method}"
                )
        return await handler(params or {})
