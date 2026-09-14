"""测试用 echo 插件：把 speak 的文本回显到 stderr，并返回 {spoken: text}。

可作为 PluginProcess / Supervisor / SinkManager 的真实子进程被 spawn 测试。
另声明 agent_tools（工具注入）能力 + mode 权限：提供 echo_tool（tools.list/call
与 mode.set/mode.get 全链路 e2e 验证工具，见 test_plugin_agent_tools_e2e.py）。
"""

import asyncio
import sys

# 插件进程能 import app.* 依赖 PYTHONPATH 包含 /aether（容器内由 Dockerfile 设置）
from app.integration.sdk.plugin_base import IntegrationPlugin, ToolDefinition
from app.integration.sdk.sink_base import OutputSink


class EchoSink(OutputSink):
    async def speak(self, text: str, msg_id: str = "") -> dict:
        print(f"[echo] speak: {text}", file=sys.stderr)
        return {"spoken": text, "msg_id": msg_id}

    async def interrupt(self) -> dict:
        print("[echo] interrupt", file=sys.stderr)
        return {"interrupted": True}


class EchoPlugin(IntegrationPlugin):
    async def _echo_tool(self, arguments: dict, context: dict) -> dict:
        """agent_tools e2e 用的宿主工具：回显参数 + 会话上下文 + 当前全局模式。"""
        mode = await self.host.mode.get()
        return {
            "echo": arguments.get("text", ""),
            "user_id": context.get("user_id", ""),
            "mode": mode,
        }

    async def _flip_mode(self, arguments: dict, context: dict) -> dict:
        """切模式工具：验证插件经 mode.set 反向 RPC 改全局模式后再读回。"""
        await self.host.mode.set(arguments.get("mode", "aether"))
        return {"mode": await self.host.mode.get()}

    def setup(self, manifest_dict: dict) -> None:
        super().setup(manifest_dict)  # 存 manifest，供 handle 的 capability 校验
        self.sinks = [EchoSink()]
        self.tools = [
            ToolDefinition(
                name="echo_tool",
                description="回显文本与会话上下文，并读当前模式",
                parameters={"type": "object", "properties": {
                    "text": {"type": "string"}}},
                handler=self._echo_tool,
            ),
            ToolDefinition(
                name="flip_mode",
                description="把全局聊天模式切到指定值",
                parameters={"type": "object", "properties": {
                    "mode": {"type": "string"}},
                    "required": ["mode"]},
                handler=self._flip_mode,
            ),
        ]


if __name__ == "__main__":
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(EchoPlugin, _manifest_path))
