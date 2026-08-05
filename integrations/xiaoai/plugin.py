"""小爱音箱插件 —— Phase 1 直连 HA 实现。

通过 xiaomi_miot.intelligent_speaker 服务做 TTS：
  execute=False → 念文字（默认 speak 模式）
  execute=True  → 当语音指令执行（Phase 2 直通模式用）

软件串行锁：Aether 自己的多次 speak 排队，不并发占用小爱。
外部程序（米家/HA 自动化）对小爱的控制不在此锁范围。

Phase 1 反向 RPC 未实现，插件进程内自建轻量 HA HTTP client 直连。
Phase 3 会替换为反向 RPC 调 aether.ha.call_service。
"""

import asyncio
import os
import sys
from typing import Any

# 插件进程能 import app.* 依赖 PYTHONPATH 包含项目根（容器内 /aether）
from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.sink_base import OutputSink


class HAHttpCaller:
    """轻量 HA HTTP 调用器（插件进程内自用，Phase 1）。

    Phase 3 会替换为反向 RPC 调 aether.ha.call_service。
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    async def call_service(
        self, domain: str, service: str,
        entity_id: str | None = None, data: dict | None = None,
    ) -> dict:
        import httpx
        payload: dict[str, Any] = {}
        if entity_id:
            payload["entity_id"] = entity_id
        if data:
            payload.update(data)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base_url}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {self._token}",
                         "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            return {"ok": True, "status": resp.status_code}


class XiaoAiSink(OutputSink):
    """小爱输出 sink。

    软件串行锁 + 队列：Aether 多条 speak 排队，Aether 主动 interrupt 可清队列。
    """

    def __init__(self, ha_caller, entity_id: str, execute_mode: str = "speak") -> None:
        self._ha = ha_caller
        self._entity_id = entity_id
        self._execute = (execute_mode == "execute")
        self._seq_lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()

    async def speak(self, text: str, msg_id: str = "") -> dict:
        await self._queue.put(text)
        async with self._seq_lock:
            spoken_all: list[str] = []
            while not self._queue.empty():
                msg = await self._queue.get()
                await self._ha.call_service(
                    domain="xiaomi_miot",
                    service="intelligent_speaker",
                    entity_id=self._entity_id,
                    data={"text": msg, "execute": self._execute, "silent": False},
                )
                spoken_all.append(msg)
            return {"spoken": " | ".join(spoken_all), "msg_id": msg_id}

    async def interrupt(self) -> dict:
        # 清空排队中的消息
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self._ha.call_service(
            domain="media_player",
            service="media_stop",
            entity_id=self._entity_id,
            data={},
        )
        return {"interrupted": True}


class XiaoAiPlugin(IntegrationPlugin):
    """小爱插件。setup 时读 manifest config_schema 默认值 + 环境变量凭证。"""

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        self.manifest = manifest_dict

        # 从 manifest config_schema 提取默认配置
        cap = manifest_dict["capabilities"][0]
        schema = cap.get("config_schema", {})
        entity_id = schema.get("entity_id", {}).get(
            "default", "media_player.xiaoai_pro")
        execute_mode = schema.get("execute_mode", {}).get("default", "speak")

        # HA 凭证从环境变量（由宿主 spawn 时注入）
        ha_url = os.environ.get("XIAOAI_HA_URL", "")
        ha_token = os.environ.get("XIAOAI_HA_TOKEN", "")
        if ha_url and ha_token:
            self.ha_caller = HAHttpCaller(ha_url, ha_token)
        else:
            self.ha_caller = None  # 无凭证时 sink 调用会失败，但不崩 setup

        self.sinks = [XiaoAiSink(self.ha_caller, entity_id, execute_mode)]


if __name__ == "__main__":
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(XiaoAiPlugin, _manifest_path))
