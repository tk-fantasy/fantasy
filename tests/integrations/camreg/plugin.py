"""camreg e2e fixture：setup 时经 camera.register 反向 RPC 注册一路虚拟摄像头。

验证链路：插件子进程 → 反向 RPC → 宿主 CameraManager.register_virtual_camera；
停止（stop_plugin / 禁用）→ on_stopped 回调 → unregister_plugin_cameras。
"""
import asyncio
import sys

from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.stdio_runtime import run_stdio_plugin


class CamRegPlugin(IntegrationPlugin):
    def setup(self, manifest_dict: dict) -> None:
        self.manifest = manifest_dict
        self.camera_id = ""
        # setup 在事件循环内被调（run_stdio_plugin），注册需 await → 拉任务
        asyncio.get_running_loop().create_task(self._register())

    async def _register(self) -> None:
        try:
            result = await self.host.camera.register({"name": "e2e虚拟摄像头"})
            self.camera_id = str(result.get("camera_id", ""))
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(CamRegPlugin, _manifest_path))
