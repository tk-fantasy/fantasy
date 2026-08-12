"""小爱音箱插件 —— 经反向 RPC 调宿主 HA（Phase 3）。

通过 xiaomi_home 集成暴露的 notify 实体做 TTS：
  play_text notify 实体：notify.send_message(message=文字) → 小爱念字
  execute_text_directive notify 实体：Phase 2 直通模式用（execute=true 语义）

软件串行锁：Aether 自己的多次 speak 排队，不并发占用小爱。
外部程序（米家/HA 自动化）对小爱的控制不在此锁范围。

Phase 3：不再自建 HA HTTP client 直连，改经 self.host.ha.call_service 反向 RPC
调宿主 ha_client（凭证不出宿主进程，权限由 manifest.permissions=["ha"] 声明）。
"""

import asyncio
import sys
from typing import Any

# 插件进程能 import app.* 依赖 PYTHONPATH 包含项目根（容器内 /aether）
from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.router_base import InboundRouter
from app.integration.sdk.sink_base import OutputSink


class XiaoAiSink(OutputSink):
    """小爱输出 sink。

    软件串行锁 + 队列：Aether 多条 speak 排队，Aether 主动 interrupt 可清队列。

    HA 调用方式：xiaomi_home 集成把小爱暴露为 notify 实体：
      - play_text notify 实体：notify.send_message(message=文字) → 小爱念字
      - execute_text_directive notify 实体：直通模式用（Phase 2）
    media_player 实体只用于 media_stop 打断。
    """

    # 默认 notify 实体后缀（拼在 device 段后）
    PLAY_TEXT_SUFFIX = "play_text_a_5_1"
    EXECUTE_DIRECTIVE_SUFFIX = "execute_text_directive_a_5_5"

    def __init__(self, ha_caller, media_player_entity: str, execute_mode: str = "speak") -> None:
        self._ha = ha_caller
        self._media_player = media_player_entity
        self._execute = (execute_mode == "execute")
        self._seq_lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()

    def _play_text_entity(self) -> str:
        """从 media_player entity_id 推导 play_text notify 实体 id。

        media_player.xiaomi_cn_2166464483_lx06 → notify.xiaomi_cn_2166464483_lx06_play_text_a_5_1
        """
        # 去掉 domain 前缀，加 notify. + suffix
        dev = self._media_player.split(".", 1)[-1]  # xiaomi_cn_2166464483_lx06
        return f"notify.{dev}_{self.PLAY_TEXT_SUFFIX}"

    async def speak(self, text: str, msg_id: str = "") -> dict:
        await self._queue.put(text)
        async with self._seq_lock:
            spoken_all: list[str] = []
            while not self._queue.empty():
                msg = await self._queue.get()
                play_text = self._play_text_entity()
                await self._ha.call_service(
                    domain="notify",
                    service="send_message",
                    data={"entity_id": play_text, "message": msg},
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
        # 小爱的 media_player 支持 media_stop
        await self._ha.call_service(
            domain="media_player",
            service="media_stop",
            entity_id=self._media_player,
            data={},
        )
        return {"interrupted": True}


class XiaoAiRouter(InboundRouter):
    """小爱直通路由：文字原样转小爱原生执行（execute=true 语义）。

    调 notify.send_message 到 execute_text_directive notify 实体，
    小爱原生执行（播放音乐/讲笑话等），不进 LLM。
    """

    EXECUTE_DIRECTIVE_SUFFIX = "execute_text_directive_a_5_5"

    def __init__(self, ha_caller, media_player_entity: str) -> None:
        self._ha = ha_caller
        self._media_player = media_player_entity

    def _execute_entity(self) -> str:
        """从 media_player entity_id 推导 execute_text_directive notify 实体 id。

        media_player.xiaomi_cn_2166464483_lx06
        → notify.xiaomi_cn_2166464483_lx06_execute_text_directive_a_5_5
        """
        dev = self._media_player.split(".", 1)[-1]
        return f"notify.{dev}_{self.EXECUTE_DIRECTIVE_SUFFIX}"

    async def route(self, text: str) -> dict:
        entity = self._execute_entity()
        await self._ha.call_service(
            domain="notify",
            service="send_message",
            data={"entity_id": entity, "message": text},
        )
        return {"ok": True, "executed": text}


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

        # Phase 3：HA 调用经反向 RPC 走宿主 ha_client（runtime 在 setup 前注入 host）。
        # 凭证不再进插件进程；权限由 manifest permissions=["ha"] 声明，宿主校验。
        self.ha_caller = self.host.ha

        self.sinks = [XiaoAiSink(self.ha_caller, entity_id, execute_mode)]
        self.routers = [XiaoAiRouter(self.ha_caller, entity_id)]


if __name__ == "__main__":
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(XiaoAiPlugin, _manifest_path))
