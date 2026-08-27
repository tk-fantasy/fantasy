"""虚拟摄像头注册生命周期 e2e：真实子进程 + 真实 CameraManager。

覆盖用户实际操作的完整闭环：
- 启用插件 → camera.register 反向 RPC → 虚拟摄像头出现在 manager（/camera、
  设置页看到的那一路）
- 禁用插件（stop_plugin）→ 进程停止 → on_stopped 回调 → 摄像头注销消失
- 再启用 → 重新注册出现（幂等，不留双份）

专门回归 PluginProcess.stop() 的 on_stopped 早退 bug：守规矩的插件在 stdin
EOF 后毫秒级自行退出，旧代码 terminate() 对已死进程抛 OSError 提前 return，
跳过 on_stopped → 摄像头永远不注销（"禁用插件开关没反应"的元凶）。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integration.integration_layer import IntegrationLayer
from app.services.camera_manager import CameraManager

# 真实拉起插件子进程，默认跳过（pytest -m slow 显式运行）
pytestmark = pytest.mark.slow

PLUGINS_DIR = "tests/integrations"
CID = "vcam_camreg"


def _make_manager() -> CameraManager:
    db = MagicMock()
    db.cameras_all = AsyncMock(side_effect=lambda: [])
    db.cameras_update = AsyncMock(return_value=True)
    return CameraManager(vision_service=None, db=db)


async def _wait_for(predicate, timeout=10.0, what=""):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    raise AssertionError(f"等待超时: {what}")


def test_plugin_toggle_registers_and_unregisters_virtual_camera():
    """启用→注册；禁用→注销；再启用→重新注册（全链路真实子进程）。"""
    manager = _make_manager()
    layer = IntegrationLayer(
        plugin_dir=PLUGINS_DIR,
        api_version="1", rpc_timeout=15.0, max_restarts=0,
        host_deps={"camera_manager": manager},
    )

    async def go():
        manager.set_event_loop(asyncio.get_running_loop())
        try:
            # 1) 启动 → camreg setup 反向注册 → 虚拟摄像头出现
            await layer.start()
            await _wait_for(lambda: manager.is_virtual_camera(CID),
                            what="插件启动后虚拟摄像头注册")
            assert CID in manager._streams

            # 2) 禁用（与 /plugin 页 toggle-enabled 同路径）→ 注销消失
            assert await layer.stop_plugin("camreg") is True
            await _wait_for(lambda: not manager.is_virtual_camera(CID),
                            what="插件禁用后虚拟摄像头注销")
            assert CID not in manager._streams

            # 3) 再启用 → 重新注册（幂等，不残留）
            assert await layer.start_plugin("camreg") is True
            await _wait_for(lambda: manager.is_virtual_camera(CID),
                            what="插件再启用后虚拟摄像头重新注册")
            vrows = [r for r in manager._virtual_rows()]
            assert len(vrows) == 1
        finally:
            await layer.stop()
            manager.stop()

    asyncio.new_event_loop().run_until_complete(go())
