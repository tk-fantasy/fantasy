"""反向 RPC 测试插件（Phase 3 死锁专项用）。

收到 ``test.call_host`` 时，在 handle 内反向调宿主 ``ha.call_service`` 并回传结果。
旧串行 runtime（read→handle→write）在此会死锁：handle await 宿主响应时无人读 stdin；
新并发 runtime（后台 reader + writer + 反向 future map）正常完成。用真实子进程跑此插件，
配合宿主侧 mock ha_client，验证插件→宿主→ha_client→宿主→插件的全链路不死锁。
"""

import asyncio
import sys

# 插件进程能 import app.* 依赖 PYTHONPATH 包含项目根（由 PluginProcess 注入）
from app.integration.sdk.plugin_base import IntegrationPlugin


class ReverseTestPlugin(IntegrationPlugin):
    """handle 内发起反向调用的测试插件。"""

    async def handle(self, method: str, params: dict) -> dict:
        if method == "test.call_host":
            # 关键：handle 内反向调宿主——死锁触发点
            res = await self.host.ha.call_service(
                params.get("domain", "light"),
                params.get("service", "turn_on"),
                entity_id=params.get("entity_id"),
            )
            return {"reversed": True, "host_result": res}
        if method == "test.broadcast":
            await self.host.broadcast(params.get("text", "hi"), params.get("msg_id", ""))
            return {"broadcasted": True}
        return {"error": f"unknown method: {method}"}


if __name__ == "__main__":
    from app.integration.sdk.stdio_runtime import run_stdio_plugin

    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(ReverseTestPlugin, _manifest_path))
