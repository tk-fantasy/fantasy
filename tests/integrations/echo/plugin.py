"""测试用 echo 插件：把 speak 的文本回显到 stderr，并返回 {spoken: text}。

可作为 PluginProcess / Supervisor / SinkManager 的真实子进程被 spawn 测试。
"""

import asyncio
import sys

# 插件进程能 import app.* 依赖 PYTHONPATH 包含 /aether（容器内由 Dockerfile 设置）
from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.sink_base import OutputSink


class EchoSink(OutputSink):
    async def speak(self, text: str, msg_id: str = "") -> dict:
        print(f"[echo] speak: {text}", file=sys.stderr)
        return {"spoken": text, "msg_id": msg_id}

    async def interrupt(self) -> dict:
        print("[echo] interrupt", file=sys.stderr)
        return {"interrupted": True}


class EchoPlugin(IntegrationPlugin):
    def setup(self, manifest_dict: dict) -> None:
        super().setup(manifest_dict)  # 存 manifest，供 handle 的 capability 校验
        self.sinks = [EchoSink()]


if __name__ == "__main__":
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(EchoPlugin, _manifest_path))
