"""OutputSink 广播管理 —— 把 Aether 回复 fan-out 到所有启用的 sink。

并发调用（asyncio.gather），单个 sink 失败不影响其他。
"""

import asyncio
import logging

from .plugin_supervisor import PluginSupervisor
from .rpc_protocol import METHOD_INTERRUPT, METHOD_SPEAK
from .schema import CapabilityType

logger = logging.getLogger(__name__)


class SinkManager:
    """广播助手回复到所有 output_sink 插件进程。

    并发 fan-out，单个 sink 失败仅记录警告、不阻塞其他 sink 也不阻塞主流程。
    """

    def __init__(self, supervisor: PluginSupervisor,
                 broadcast_enabled: bool = True) -> None:
        self._supervisor = supervisor
        # 运行时广播开关：False 时 broadcast/interrupt 直接返回，不触达任何 sink。
        # 由 IntegrationLayer 从 config 读取初始化，toggle API 运行时切换。
        self.broadcast_enabled = broadcast_enabled

    def _collect_sink_processes(self) -> list[tuple[str, object]]:
        """收集所有声明了 output_sink 能力的运行中进程。"""
        result = []
        for manifest in self._supervisor.get_running_manifests():
            if manifest.has_capability(CapabilityType.OUTPUT_SINK):
                proc = self._supervisor.get_process(manifest.id)
                if proc is not None and proc.is_alive:
                    result.append((manifest.id, proc))
        return result

    async def broadcast(self, text: str, msg_id: str = "") -> None:
        """并发广播文本到所有 sink。单个失败不阻塞其他。"""
        # 全局广播开关：关闭时静默跳过（interrupt 仍可用于清队列/停止硬件）
        if not self.broadcast_enabled:
            return
        sinks = self._collect_sink_processes()
        if not sinks:
            return

        async def _send(plugin_id: str, proc) -> None:
            try:
                await proc.call(METHOD_SPEAK, {"text": text, "msg_id": msg_id})
            except Exception as exc:
                logger.warning("广播到 sink %s 失败: %s", plugin_id, exc)

        await asyncio.gather(*[_send(pid, proc) for pid, proc in sinks])

    async def interrupt_all(self) -> None:
        """并发中断所有 sink 当前播报。"""
        sinks = self._collect_sink_processes()
        if not sinks:
            return

        async def _stop(plugin_id: str, proc) -> None:
            try:
                await proc.call(METHOD_INTERRUPT, {})
            except Exception as exc:
                logger.warning("中断 sink %s 失败: %s", plugin_id, exc)

        await asyncio.gather(*[_stop(pid, proc) for pid, proc in sinks])
