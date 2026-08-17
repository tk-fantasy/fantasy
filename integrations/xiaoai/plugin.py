"""小爱音箱插件 —— 经反向 RPC 调宿主 HA（Phase 3）。

通过 xiaomi_home 集成暴露的 notify 实体做 TTS 与指令直通：
  play_text notify 实体：notify.send_message(message=[文本]) → 小爱念字
  execute_text_directive notify 实体：notify.send_message(message=[文本, 静默执行])
    → 小爱原生执行（播放音乐/查天气等），不进 LLM

实体解析（XiaoAiResolver，懒检测）：
  管理页配置了 entity_id → 显式指定（校验失败报错，不静默回退）；
  未配置 → 自动扫描 HA 实体，按 MIoT 规格后缀 _play_text_a_5_1 /
  _execute_text_directive_a_5_5 识别（跨型号稳定，米家改名不影响）。
  恰好一台自动接入；多台报错列出候选，引导用户配置。

message 必须是 JSON 列表字符串（JSON 是合法 YAML）：xiaomi_home notify 把
message 按 YAML 解析成 action 参数列表，execute_text_directive 有两个参数
[文本(str), 静默执行(bool)]，纯文本会因参数个数不符被静默丢弃（HA 仍返回
200 假成功）——2026-08-17 直通失效事故根因。

软件串行锁：Aether 自己的多次 speak 排队，不并发占用小爱。
外部程序（米家/HA 自动化）对小爱的控制不在此锁范围。
"""

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any

# 插件进程能 import app.* 依赖 PYTHONPATH 包含项目根（容器内 /aether）
from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.router_base import InboundRouter
from app.integration.sdk.sink_base import OutputSink

_log = logging.getLogger("xiaoai")

# MIoT 规格 action 后缀（siid=5/aiid=1 播放文本、siid=5/aiid=5 执行文本指令），
# 所有小爱音箱型号一致，与实体命名无关。
PLAY_TEXT_SUFFIX = "_play_text_a_5_1"
EXECUTE_DIRECTIVE_SUFFIX = "_execute_text_directive_a_5_5"


@dataclass
class ResolvedSpeaker:
    """一台小爱音箱的关联实体。media_player 可缺失（interrupt 降级）。"""
    slug: str                  # 如 xiaomi_cn_2166464483_lx06
    media_player: str | None   # media_player.<slug>
    play_text: str             # notify.<slug>_play_text_a_5_1
    execute: str               # notify.<slug>_execute_text_directive_a_5_5


class XiaoAiResolveError(Exception):
    """实体解析失败。message 面向用户（中文），经 {"error": ...} 透出到聊天界面。"""


class XiaoAiResolver:
    """懒解析小爱实体：显式配置优先，否则按 MIoT 规格后缀自动检测。

    成功结果缓存（resolve 内 asyncio.Lock 防并发重复扫描）；
    invalidate() 后下次调用重扫（应对换音箱/实体变更）。
    """

    def __init__(self, ha_caller, configured_entity: str = "") -> None:
        self._ha = ha_caller
        self._configured = configured_entity.strip()
        self._lock = asyncio.Lock()
        self._cached: ResolvedSpeaker | None = None

    def invalidate(self) -> None:
        """守卫触发（实体失联/调用异常）后清缓存，下次调用重新解析。"""
        self._cached = None

    async def resolve(self) -> ResolvedSpeaker:
        """返回已解析的音箱实体；失败抛 XiaoAiResolveError（不抛其他异常）。"""
        if self._cached is not None:
            return self._cached
        async with self._lock:
            if self._cached is not None:
                return self._cached
            states = await self._ha.get_states()
            entities = {s.get("entity_id", "") for s in states.get("states", [])}
            resolved = (
                self._resolve_configured(entities)
                if self._configured else self._resolve_auto(entities)
            )
            self._cached = resolved
            return resolved

    def _resolve_configured(self, entities: set[str]) -> ResolvedSpeaker:
        """显式配置路径：从 media_player entity 推导 notify 实体并校验存在。"""
        slug = self._configured.split(".", 1)[-1]
        resolved = ResolvedSpeaker(
            slug=slug,
            media_player=self._configured if self._configured in entities else None,
            play_text=f"notify.{slug}{PLAY_TEXT_SUFFIX}",
            execute=f"notify.{slug}{EXECUTE_DIRECTIVE_SUFFIX}",
        )
        missing = [e for e in (resolved.play_text, resolved.execute) if e not in entities]
        if missing:
            raise XiaoAiResolveError(
                f"配置的小爱实体不存在: {', '.join(missing)}"
                "（请到集成管理页修正 entity_id，或留空自动检测）")
        return resolved

    def _resolve_auto(self, entities: set[str]) -> ResolvedSpeaker:
        """自动检测路径：扫成对的 MIoT 规格 notify 实体。"""
        slugs = set()
        for eid in entities:
            if not (eid.startswith("notify.") and eid.endswith(EXECUTE_DIRECTIVE_SUFFIX)):
                continue
            slug = eid[len("notify."):-len(EXECUTE_DIRECTIVE_SUFFIX)]
            if slug and f"notify.{slug}{PLAY_TEXT_SUFFIX}" in entities:
                slugs.add(slug)
        if not slugs:
            raise XiaoAiResolveError("未发现小爱音箱（需要 xiaomi_home 集成接入小爱音箱）")
        if len(slugs) > 1:
            candidates = "、".join(f"media_player.{s}" for s in sorted(slugs))
            raise XiaoAiResolveError(
                f"发现 {len(slugs)} 台小爱音箱: {candidates}；"
                "请在集成管理页的 entity_id 里指定一台")
        slug = slugs.pop()
        _log.info("自动检测到小爱音箱: %s", slug)
        return ResolvedSpeaker(
            slug=slug,
            media_player=f"media_player.{slug}" if f"media_player.{slug}" in entities else None,
            play_text=f"notify.{slug}{PLAY_TEXT_SUFFIX}",
            execute=f"notify.{slug}{EXECUTE_DIRECTIVE_SUFFIX}",
        )


def _payload_message(text: str, *, directive: bool) -> str:
    """序列化 xiaomi_home notify 的 action 参数列表。

    JSON 是合法 YAML 且安全转义任意引号/换行；纯文本会被 YAML 强转
    （"on"/"yes"→bool）或因参数个数不符被静默丢弃。
    directive=True 对应 execute_text_directive 两参数 [文本, 静默执行]，
    静默=False：小爱有声执行（像对话一样有反馈）。
    """
    return json.dumps([text, False] if directive else [text], ensure_ascii=False)


async def _notify_send(ha_caller, resolver: XiaoAiResolver,
                       entity: str, text: str, *, directive: bool) -> dict:
    """调 notify.send_message 并守卫两类静默失败（实体不存在 / HA 异常）。"""
    try:
        result = await ha_caller.call_service(
            domain="notify",
            service="send_message",
            data={"entity_id": entity, "message": _payload_message(text, directive=directive)},
        )
    except Exception:
        resolver.invalidate()
        _log.warning("HA 调用失败（%s），缓存已失效待重检", entity, exc_info=True)
        return {"error": "小爱 HA 调用失败，已触发重新检测"}
    # HA 对不存在的 notify 实体返回 200 空列表——假成功（2026-08-17 事故）
    if result == []:
        resolver.invalidate()
        return {"error": f"小爱 notify 实体不存在: {entity}，已触发重新检测"}
    return {"ok": True}


class XiaoAiSink(OutputSink):
    """小爱输出 sink。

    软件串行锁 + 队列：Aether 多条 speak 排队，Aether 主动 interrupt 可清队列。
    execute_mode="speak" → play_text 念字；"execute" → execute_text_directive 原生执行。
    """

    def __init__(self, ha_caller, resolver: XiaoAiResolver,
                 execute_mode: str = "speak") -> None:
        self._ha = ha_caller
        self._resolver = resolver
        self._execute = (execute_mode == "execute")
        self._seq_lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()

    async def speak(self, text: str, msg_id: str = "") -> dict:
        await self._queue.put(text)
        async with self._seq_lock:
            spoken_all: list[str] = []
            while not self._queue.empty():
                msg = await self._queue.get()
                try:
                    resolved = await self._resolver.resolve()
                except XiaoAiResolveError as exc:
                    return {"error": str(exc)}
                entity = resolved.execute if self._execute else resolved.play_text
                result = await _notify_send(
                    self._ha, self._resolver, entity, msg, directive=self._execute)
                if "error" in result:
                    return result
                spoken_all.append(msg)
            return {"spoken": " | ".join(spoken_all), "msg_id": msg_id}

    async def interrupt(self) -> dict:
        # 清空排队中的消息
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            resolved = await self._resolver.resolve()
        except XiaoAiResolveError as exc:
            return {"error": str(exc)}
        if resolved.media_player is None:
            _log.warning("小爱 %s 无 media_player 实体，跳过 media_stop", resolved.slug)
            return {"interrupted": True, "note": "no media_player"}
        try:
            await self._ha.call_service(
                domain="media_player",
                service="media_stop",
                entity_id=resolved.media_player,
                data={},
            )
        except Exception:
            _log.warning("media_stop 失败", exc_info=True)
        return {"interrupted": True}


class XiaoAiRouter(InboundRouter):
    """小爱直通路由：文字原样转小爱原生执行（execute_text_directive，非静默）。"""

    def __init__(self, ha_caller, resolver: XiaoAiResolver) -> None:
        self._ha = ha_caller
        self._resolver = resolver

    async def route(self, text: str) -> dict:
        try:
            resolved = await self._resolver.resolve()
        except XiaoAiResolveError as exc:
            return {"error": str(exc)}
        result = await _notify_send(
            self._ha, self._resolver, resolved.execute, text, directive=True)
        if "error" in result:
            return result
        return {"ok": True, "executed": text, "speaker": resolved.slug}


class XiaoAiPlugin(IntegrationPlugin):
    """小爱插件。setup 时读 manifest config_schema（经 AETHER_PLUGIN_CONFIG
    合并后的值：entity_id 非空=用户显式配置，空=自动检测）。"""

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        self.manifest = manifest_dict

        # 从 manifest config_schema 提取配置（宿主已把管理页配置合并进 default）
        cap = manifest_dict["capabilities"][0]
        schema = cap.get("config_schema", {})
        entity_id = str(schema.get("entity_id", {}).get("default", "") or "")
        execute_mode = schema.get("execute_mode", {}).get("default", "speak")

        # Phase 3：HA 调用经反向 RPC 走宿主 ha_client（runtime 在 setup 前注入 host）。
        # 凭证不再进插件进程；权限由 manifest permissions=["ha"] 声明，宿主校验。
        self.ha_caller = self.host.ha

        self.resolver = XiaoAiResolver(self.ha_caller, entity_id)
        self.sinks = [XiaoAiSink(self.ha_caller, self.resolver, execute_mode)]
        self.routers = [XiaoAiRouter(self.ha_caller, self.resolver)]


if __name__ == "__main__":
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(XiaoAiPlugin, _manifest_path))
