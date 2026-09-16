"""设备状态事件流 — 订阅 HA state_changed，事件总线分发 + 节流聚合落 family_events。

补齐状态变化事件链路：系统对 HA 是纯拉取（REST 快照 + 5s 缓存），物理侧
真实状态变化（开关翻转、传感器波动、设备掉线）原本不可见。本服务经 HA
WebSocket subscribe_events 订阅 state_changed，成为 HA→Aether 的唯一推送入口，
四个消费方向：
- 落库：过滤/节流后走 alert_service.record() 进 family_events（周报/时间线数据源）；
- states 缓存增量更新：事件原地刷新 ha_service._states_cache，省一次全量拉取；
- 前端实时推送：/ws/events 通道广播（sensor 数值类不推，交给兜底轮询）；
- 内部订阅者：subscribe() 注册的组件回调（摄像头 HA 触发、presence/sun/helper
  规则评估等），是阶段化接入 HA 事件的统一挂点。

节流策略（高频传感器会稀释事件流——周报 LLM 输入与前端时间线都只取
最近 500 条，不节流会把告警挤出窗口）：
- 控制类 domain（light/switch/lock...）与 binary_sensor/person：每次真实
  状态翻转记 1 条（低频，家庭语义强）
- sensor 数值类：同实体按窗口（默认 1 小时）聚合成 1 条「变化 N 次（min~max）」
- 任何实体变 unavailable/unknown 即时记 1 条（设备掉线价值高）

开关：device_events.enabled（默认 true）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from ..core.config import get_config

logger = logging.getLogger(__name__)

# 即时记录的 domain：状态翻转低频且有家庭语义（谁开了灯/门锁了/到家了）
# sun 每天两次翻转（日间/夜间）家庭语义强（阶段4）；calendar 的日程驱动走
# EventTriggerService 轮询，state 翻转噪声大不落时间线
_INSTANT_DOMAINS = frozenset({
    "light", "switch", "cover", "lock", "fan", "climate", "humidifier",
    "media_player", "vacuum", "water_heater", "button", "input_boolean",
    "siren", "valve", "remote", "number", "select",
    "binary_sensor", "person", "device_tracker", "sun",
})
_SENSOR_DOMAIN = "sensor"

_STATE_ZH = {
    "on": "开", "off": "关",
    "open": "打开", "closed": "关闭", "opening": "正在打开", "closing": "正在关闭",
    "locked": "已上锁", "unlocked": "已开锁", "locking": "上锁中", "unlocking": "开锁中",
    "unavailable": "不可用", "unknown": "未知",
    "home": "在家", "not_home": "离家",
    "idle": "空闲", "standby": "待机", "paused": "暂停", "playing": "播放中",
    "heat": "制热", "cool": "制冷", "auto": "自动", "dry": "除湿",
    "active": "工作中", "cleaning": "清扫中", "returning": "回充中",
    "triggered": "已触发", "problem": "异常", "ok": "正常", "detected": "检测到",
    "clear": "无异常", "connected": "已连接", "disconnected": "已断开",
    "above_horizon": "日间", "below_horizon": "夜间",
}


def _state_zh(value: str) -> str:
    return _STATE_ZH.get(value, value)


# 主控操作的常用 service 中译（其余原样显示，如 set_temperature）
_SERVICE_ZH = {
    "turn_on": "打开", "turn_off": "关闭", "toggle": "切换",
    "open_cover": "打开", "close_cover": "关闭", "stop_cover": "停止",
    "open_valve": "打开", "close_valve": "关闭",
    "lock": "上锁", "unlock": "开锁",
}


async def record_device_op(
    entity_ids: list[str], service: str, actor: str = "AI",
    name_of: dict[str, str] | None = None,
) -> None:
    """主控设备操作落 device_op 事件（AI 工具与 UI 路由共用插桩）。

    与被动 state_changed 区分开：周报可统计「AI 帮你操作设备 N 次」。
    单实体一条（source=device:{entity_id}），与事件时间线粒度一致；
    actor 同时写进结构化字段（统计图分组用），message 前缀保留兼容存量读取。
    任何失败只记日志，绝不影响控制主流程。
    """
    if not entity_ids:
        return
    try:
        from .alert_service import alert_service
        label = _SERVICE_ZH.get(service, service)
        for e in entity_ids[:10]:
            name = (name_of or {}).get(e) or e
            await alert_service.record(
                "device_op", f"device:{e}", f"{actor}将「{name}」执行 {label}", actor)
    except Exception:
        logger.warning("record_device_op failed (service=%s)", service, exc_info=True)


class _Subscriber:
    """内部订阅者：按 domain / entity_id / event_type 过滤的回调注册项。"""

    __slots__ = ("callback", "domains", "entity_ids", "event_types", "name")

    def __init__(
        self,
        callback: Callable[..., Any],
        domains: frozenset[str] | set[str] | None,
        entity_ids: frozenset[str] | set[str] | None,
        event_types: frozenset[str] | set[str] | None,
        name: str,
    ) -> None:
        self.callback = callback
        self.domains = frozenset(domains) if domains else None
        self.entity_ids = frozenset(entity_ids) if entity_ids else None
        self.event_types = frozenset(event_types) if event_types else None
        self.name = name

    def matches(self, event_type: str, entity_id: str, domain: str) -> bool:
        if self.event_types is not None and event_type not in self.event_types:
            return False
        if self.entity_ids is not None and entity_id and entity_id not in self.entity_ids:
            return False
        if self.domains is not None and domain and domain not in self.domains:
            return False
        return True


class DeviceEventService:
    def __init__(self, ha_service: Any = None) -> None:
        self._ha_service = ha_service
        self._task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        # entity_id -> {name, count, min, max, last, unit, flush_at}
        self._sensor_buffer: dict[str, dict[str, Any]] = {}
        # 内部订阅者（事件总线挂点）：组件经 subscribe() 注册回调
        self._subscribers: list[_Subscriber] = []

    def set_ha_service(self, ha_service: Any) -> None:
        self._ha_service = ha_service

    # ------------------------------------------------------------------
    # 事件总线：内部订阅 API
    # ------------------------------------------------------------------

    def subscribe(
        self,
        callback: Callable[..., Any],
        *,
        domains: set[str] | frozenset[str] | None = None,
        entity_ids: set[str] | frozenset[str] | None = None,
        event_types: set[str] | frozenset[str] | None = None,
        name: str = "",
    ) -> Callable[[], None]:
        """注册事件回调，返回取消订阅函数。callback 收到 event dict：
        {"event_type": "state_changed", "entity_id", "domain", "old_state", "new_state"}
        或 {"event_type": "timer.finished", ...原始 data 字段}。

        过滤参数 None 表示不限定；回调可以是同步或异步函数，异常各自隔离。
        """
        sub = _Subscriber(callback, domains, entity_ids, event_types, name or getattr(callback, "__name__", "sub"))
        self._subscribers.append(sub)
        logger.info("device event subscriber registered: %s", sub.name)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass

        return _unsubscribe

    async def _dispatch(self, event: dict) -> None:
        """分发给所有匹配的内部订阅者。单个订阅者异常不影响其余。"""
        event_type = str(event.get("event_type", ""))
        entity_id = str(event.get("entity_id", ""))
        domain = str(event.get("domain", "") or entity_id.split(".", 1)[0])
        for sub in list(self._subscribers):
            if not sub.matches(event_type, entity_id, domain):
                continue
            try:
                result = sub.callback(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("event subscriber %s failed", sub.name)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self._is_enabled():
            logger.info("Device event service disabled (device_events.enabled=false)")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="device-events")
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop(), name="device-events-flush")
        logger.info("Device event service started")

    async def stop(self) -> None:
        for t in (self._task, self._flush_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._task = self._flush_task = None
        # 尽力把缓冲中的传感器聚合落库（DB 关闭前调用才有效，失败静默）
        try:
            await self._flush_sensors(force=True)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 订阅主循环
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """常驻订阅循环。断线指数退避重连（每次重连重新取 token，过期自愈）。"""
        import websockets

        backoff = 5.0
        while True:
            try:
                client = getattr(self._ha_service, "_client", None)
                base_url = str(getattr(client, "base_url", "") or "")
                token = str(getattr(client, "token", "") or "")
                if not base_url or not token:
                    await asyncio.sleep(60)  # HA 未配置：慢轮询等待配置出现
                    continue
                ws_url = base_url.replace("http", "ws") + "/api/websocket"
                async with websockets.connect(
                    ws_url, additional_headers={"Authorization": f"Bearer {token}"}
                ) as ws:
                    await self._subscribe(ws)
                    backoff = 5.0
                    logger.info("HA state_changed subscription started")
                    await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "HA event stream down, retrying in %.0fs", backoff, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    async def _subscribe(self, ws: Any) -> None:
        """认证 + 订阅 state_changed 与 timer.finished（握手模板同 ha_service._refresh_registry）。

        timer.finished 是 HA 事件（非实体状态），input_timer「N 秒自动关」类规则
        靠它触发；订阅不存在的事件类型 HA 也接受，无 timer 实体时只是收不到帧。
        """
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected HA handshake: {hello.get('type')}")
        token = str(getattr(getattr(self._ha_service, "_client", None), "token", "") or "")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        result = json.loads(await ws.recv())
        if result.get("type") != "auth_ok":
            raise RuntimeError(f"HA WebSocket auth failed: {result}")
        subscriptions = [
            (1, "state_changed"),
            (2, "timer.finished"),
        ]
        acked: set[int] = set()
        for sid, event_type in subscriptions:
            await ws.send(json.dumps({
                "id": sid, "type": "subscribe_events", "event_type": event_type,
            }))
        while len(acked) < len(subscriptions):  # 等全部订阅 ack（HA 可能先推送缓存事件）
            msg = json.loads(await ws.recv())
            sid = msg.get("id")
            if sid in {s for s, _ in subscriptions}:
                if not msg.get("success", False):
                    raise RuntimeError(f"subscribe_events failed: {msg.get('error')}")
                acked.add(sid)

    async def _consume(self, ws: Any) -> None:
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "event":
                continue
            ev = msg.get("event") or {}
            event_type = ev.get("event_type", "")
            data = ev.get("data") or {}
            try:
                if event_type == "state_changed":
                    await self._on_state_changed(
                        str(data.get("entity_id", "")),
                        data.get("old_state"), data.get("new_state"),
                    )
                elif event_type == "timer.finished":
                    await self._on_timer_finished(data)
            except Exception:
                logger.exception("%s handling failed", event_type or "event")

    # ------------------------------------------------------------------
    # 事件处理：过滤 + 分类节流
    # ------------------------------------------------------------------

    async def _on_state_changed(
        self, entity_id: str, old_state: Any, new_state: Any,
    ) -> None:
        if not entity_id or new_state is None:
            return  # 实体被删除不记
        new_value = str(new_state.get("state", ""))
        # old_state 为 None（新实体首见 / HA 重启全量重发）按不变处理，避免噪音
        old_value = str(old_state.get("state", "")) if old_state else new_value
        if new_value == old_value:
            return  # 仅 attribute 变化（亮度微调等）不算家庭事件
        domain = entity_id.split(".", 1)[0]
        # 事件总线：缓存增量 + 前端推送 + 内部订阅者（摄像头触发/规则触发挂点）
        await self._fanout_state_change(entity_id, domain, old_state, new_state)
        name = self._friendly_name(new_state, entity_id)
        if new_value in ("unavailable", "unknown"):
            await self._record_state(entity_id, f"{name} 变为不可用")
            return
        if domain in ("person", "device_tracker"):
            # 在场事件动态文案：到家/离家（比「在家」更贴近事件语义，周报可读性更好）
            verb = "到家" if new_value == "home" else ("离家" if new_value == "not_home" else _state_zh(new_value))
            await self._record_state(entity_id, f"{name} {verb}")
            return
        if domain in _INSTANT_DOMAINS:
            await self._record_state(entity_id, f"{name} {_state_zh(new_value)}")
            return
        if domain == _SENSOR_DOMAIN:
            self._buffer_sensor(entity_id, name, new_value, new_state.get("attributes") or {})

    async def _on_timer_finished(self, data: dict) -> None:
        """timer 倒计时结束事件：只进事件总线（订阅者），不落 family_events。"""
        entity_id = str(data.get("entity_id", ""))
        await self._dispatch({
            "event_type": "timer.finished",
            "entity_id": entity_id,
            "domain": "timer",
            **data,
        })

    async def _fanout_state_change(
        self, entity_id: str, domain: str, old_state: Any, new_state: Any,
    ) -> None:
        """真实状态变化的三路分发：states 缓存增量 / 前端 /ws/events / 内部订阅者。"""
        # 1) states 缓存增量更新：缓存有效时原地替换，事件持续流入则免全量拉取
        try:
            apply = getattr(self._ha_service, "apply_state_change", None)
            if apply is not None:
                apply(entity_id, new_state)
        except Exception:  # noqa: BLE001
            logger.debug("apply_state_change failed for %s", entity_id, exc_info=True)
        # 2) 前端实时推送：sensor 数值类不推（高频噪声），交给兜底轮询
        if domain != _SENSOR_DOMAIN:
            try:
                from ..core.ws_registry import push_to_events
                await push_to_events({
                    "type": "entity_state",
                    "entity_id": entity_id,
                    "domain": domain,
                    "state": str(new_state.get("state", "")),
                    "attributes": new_state.get("attributes") or {},
                })
            except Exception:  # noqa: BLE001
                logger.debug("entity_state push failed for %s", entity_id, exc_info=True)
        # 3) 内部订阅者（摄像头 HA 触发、presence/sun/helper 规则评估等）
        await self._dispatch({
            "event_type": "state_changed",
            "entity_id": entity_id,
            "domain": domain,
            "old_state": old_state,
            "new_state": new_state,
        })

    def _buffer_sensor(
        self, entity_id: str, name: str, value: str, attributes: dict,
    ) -> None:
        buf = self._sensor_buffer.get(entity_id)
        try:
            num = float(value)
        except (TypeError, ValueError):
            num = None
        if buf is None:
            self._sensor_buffer[entity_id] = {
                "name": name, "count": 1, "min": num, "max": num,
                "last": value,
                "unit": str(attributes.get("unit_of_measurement", "") or ""),
                "flush_at": time.time() + self._flush_seconds(),
            }
            return
        buf["count"] += 1
        buf["last"] = value
        if name != entity_id:  # 只在拿到友好名时刷新（后续事件可能没带 attributes）
            buf["name"] = name
        if num is not None:
            buf["min"] = num if buf["min"] is None else min(buf["min"], num)
            buf["max"] = num if buf["max"] is None else max(buf["max"], num)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._flush_sensors()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sensor flush failed")

    async def _flush_sensors(self, force: bool = False) -> None:
        """把窗口到期的传感器缓冲落成聚合事件（每实体每窗口最多 1 条）。"""
        now = time.time()
        for entity_id, buf in list(self._sensor_buffer.items()):
            if not force and now < buf["flush_at"]:
                continue
            self._sensor_buffer.pop(entity_id, None)
            rng = ""
            if buf["min"] is not None and buf["max"] is not None \
                    and buf["max"] != buf["min"]:
                rng = f"（{buf['min']:g}~{buf['max']:g}{buf['unit']}）"
            await self._record_state(
                entity_id, f"{buf['name']} 1 小时内变化 {buf['count']} 次{rng}")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _record_state(self, entity_id: str, message: str) -> None:
        from .alert_service import alert_service
        await alert_service.record("device_state", f"device:{entity_id}", message)

    @staticmethod
    def _friendly_name(state: dict, entity_id: str) -> str:
        attrs = state.get("attributes") or {}
        return str(attrs.get("friendly_name") or entity_id)

    @staticmethod
    def _is_enabled() -> bool:
        try:
            return bool(get_config("device_events.enabled", True))
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _flush_seconds() -> float:
        try:
            return max(60.0, float(get_config("device_events.sensor_flush_seconds", 3600.0)))
        except Exception:  # noqa: BLE001
            return 3600.0
