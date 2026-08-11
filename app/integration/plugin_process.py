"""单个插件子进程的 stdio JSON-RPC 连接（方向 1: Aether → 插件）。

复用 MCP ExternalMCPServer 的 stdio 模式：spawn 子进程，通过 stdin/stdout
交换 JSON-RPC，用 pending futures map 做请求-响应配对。
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .rpc_protocol import (
    METHOD_HANDSHAKE,
    build_request,
    parse_message,
)
from .schema import Manifest

logger = logging.getLogger(__name__)

# 子进程沙箱：只继承运行必需的系统级环境变量，排除宿主密钥。
# 这是最小必需集——少了 Python 起不来（PATH/SYSTEMROOT）或 IO 异常（TEMP）。
# 凭证性变量（JWT_SECRET/RTSP_PASSWORD 等宿主密钥）刻意不在此列，
# 由 _build_plugin_env 按 manifest.secrets 白名单注入。
# 注意：PYTHONPATH 不在此列——start() 会动态注入项目根，不继承宿主的。
_SANDBOX_ALLOWED_ENV = frozenset({
    "PATH",                           # 解释器找依赖
    "SYSTEMROOT",                     # Windows 必需（Win32 API）
    "TEMP", "TMP", "TMPDIR",          # 临时目录
    "LANG", "LC_ALL", "LC_CTYPE",     # 区域（影响日志/编码）
    "HOME", "USERPROFILE",            # 用户目录（部分库读 ~/.cache）
    "APPDATA", "LOCALAPPDATA",        # Windows 应用数据
})


def _sandbox_env() -> dict[str, str]:
    """构造子进程沙箱环境：白名单继承宿主变量，排除全部密钥。

    只保留 _SANDBOX_ALLOWED_ENV 中的变量；宿主的 JWT_SECRET /
    RTSP_PASSWORD / PTZ_PASSWORD 等敏感变量不会进入子进程。
    """
    return {k: v for k, v in os.environ.items() if k in _SANDBOX_ALLOWED_ENV}


class PluginProcess:
    """一个插件进程的连接器。

    负责 spawn 子进程、握手、请求-响应配对、优雅关闭。
    不负责重启（那是 PluginSupervisor 的职责）。
    """

    def __init__(
        self,
        manifest: Manifest,
        plugin_root: str,
        rpc_timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.manifest = manifest
        self._plugin_root = plugin_root
        self._rpc_timeout = rpc_timeout
        # 子进程环境沙箱：只白名单继承子进程运行必需的系统变量，
        # 不全量继承宿主 os.environ——否则插件能读走 JWT_SECRET /
        # RTSP_PASSWORD / PTZ_PASSWORD 等宿主密钥（开放第三方插件时的安全边界）。
        # 凭证通过 env 参数按 manifest.secrets 声明白名单注入（_build_plugin_env）。
        self._env: dict[str, str] = _sandbox_env()
        if env:
            self._env.update({k: str(v) for k, v in env.items()})
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def start(self) -> None:
        """spawn 子进程并完成握手。"""
        entry = self._resolve_entry()
        manifest_path = str(Path(self._plugin_root) / "manifest.json")
        cmd = [sys.executable, entry, manifest_path]

        # 子进程 sys.path 不含项目根（脚本目录 ≠ cwd，且无 PYTHONPATH 时
        # import app.* 失败）。把包含 app/ 的祖先目录注入子进程 PYTHONPATH，
        # 保证本地开发/CI 与容器（Dockerfile 显式设 PYTHONPATH）行为一致。
        root = self._find_project_root()
        if root:
            old = self._env.get("PYTHONPATH", "")
            self._env["PYTHONPATH"] = str(root) + (os.pathsep + old if old else "")

        logger.info("启动插件 %s: %s", self.manifest.id, " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # 握手失败必须清理已 spawn 的子进程 + reader/stderr task，
        # 否则 supervisor 重试会累积存活子进程（每次失败泄漏一个）。
        try:
            await self._handshake()
        except Exception:
            await self.stop()
            raise
        self._alive = True
        logger.info("插件 %s 已启动 (pid=%s)", self.manifest.id, self._process.pid)

    def _resolve_entry(self) -> str:
        """插件入口脚本路径。"""
        return str(Path(self._plugin_root) / self.manifest.entry)

    @staticmethod
    def _find_project_root() -> Path | None:
        """向上找到包含 app/ 包的项目根目录（用于注入子进程 PYTHONPATH）。"""
        from ..core.config import BASE_DIR
        return BASE_DIR

    async def _handshake(self) -> None:
        result = await self.call(METHOD_HANDSHAKE, {
            "aether_api_version": "1",
            "capabilities_expected": [c.type.value for c in self.manifest.capabilities],
        })
        if not result.get("ready"):
            raise RuntimeError(f"插件 {self.manifest.id} 握手失败: {result}")
        logger.info("插件 %s 握手成功: %s", self.manifest.id, result)

    async def call(self, method: str, params: dict | None = None) -> dict:
        """发 JSON-RPC 请求，等响应。

        超时或进程未运行时抛 RuntimeError。
        """
        if self._process is None or self._process.stdin.is_closing():
            raise RuntimeError(f"插件 {self.manifest.id} 未运行")

        self._next_id += 1  # 从 2 开始的偶数 id（Aether 侧）
        rid = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[rid] = future

        payload = build_request(rid, method, params)
        line = json.dumps(payload, ensure_ascii=False)
        try:
            assert self._process.stdin is not None
            self._process.stdin.write((line + "\n").encode("utf-8"))
            await asyncio.wait_for(self._process.stdin.drain(), timeout=self._rpc_timeout)
            result = await asyncio.wait_for(future, timeout=self._rpc_timeout)
            return result
        except asyncio.TimeoutError:
            raise RuntimeError(f"插件 {self.manifest.id} 调用 {method} 超时")
        finally:
            self._pending.pop(rid, None)

    async def _read_stdout(self) -> None:
        """读取子进程 stdout，按 id 配对响应到 pending future。"""
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            msg = parse_message(line.decode("utf-8", errors="replace"))
            if msg is None:
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if not fut.done():
                    fut.set_result(msg.get("result", {}))

    async def _drain_stderr(self) -> None:
        """把插件 stderr 当日志（带 plugin_id 前缀）。"""
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            logger.debug("[%s] %s", self.manifest.id,
                         line.decode("utf-8", errors="replace").rstrip())

    async def stop(self) -> None:
        """优雅停止：shutdown 通知 → terminate → kill。"""
        self._alive = False
        if self._process is None:
            return

        # 尝试发 shutdown 通知（不强制等响应）
        try:
            await asyncio.wait_for(self.call("shutdown", {}), timeout=3.0)
        except (RuntimeError, asyncio.TimeoutError):
            pass

        # 失败所有未完成请求
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("plugin stopping"))
        self._pending.clear()

        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

        try:
            self._process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()
