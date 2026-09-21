"""事件驱动规则触发 — presence/sun/helper 订阅 + calendar 轮询（阶段4）。

time/weather 由 AutomationAgent 静默循环兜底评估；presence/sun/calendar/helper
四类规则的触发源是 HA 事件/日程，等到事件发生才评估（事件即触发，condition
仍可走 chat LLM 组合判定，如「到家且天黑」）。三条事件通路：

- state_changed（person/device_tracker → presence；sun → sun；input_boolean → helper）
  经 DeviceEventService 事件总线订阅；
- timer.finished（→ helper）：「计时器 10 秒结束后关灯」；
- calendar：REST 轮询（默认 5 分钟）对齐日程 start/end 时刻——calendar 实体的
  state_changed 不可靠（多数集成只在 attribute 里带事件，state 长期为 off）。

评估统一汇入 automation_service.evaluate(rule_types=(单类型,), event_context=…)，
冷却/设备门控/动作执行与既有管道完全一致。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from ..core.config import get_config

logger = logging.getLogger(__name__)

# 事件订阅的 domain 集（timer.finished 是独立事件类型，不在此列）
_EVENT_DOMAINS = frozenset({"person", "device_tracker", "sun", "input_boolean"})

_CALENDAR_POLL_SECONDS = 300.0  # 5 分钟对齐日程时刻


class EventTriggerService:
    def __init__(self, ha_service: Any = None, automation_service: Any = None) -> None:
        self._ha_service = ha_service
        self._automation_service = automation_service
        self._unsub: Any = None
        self._calendar_task: asyncio.Task | None = None
        # (entity_id, uid, phase) 已触发去重；防止同一日程事件被两个轮询周期重复触发
        self._fired_calendar: set[tuple[str, str, str]] = set()

    def set_ha_service(self, ha_service: Any) -> None:
        self._ha_service = ha_service

    def set_automation_service(self, automation_service: Any) -> None:
        self._automation_service = automation_service

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._calendar_task = asyncio.create_task(
            self._calendar_loop(), name="event-trigger-calendar")
        logger.info("Event trigger service started (calendar poll %.0fs)", _CALENDAR_POLL_SECONDS)

    async def stop(self) -> None:
        if self._calendar_task is not None:
            self._calendar_task.cancel()
            self._calendar_task = None
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def bind(self, des: Any) -> None:
        """挂接设备事件总线（幂等）。"""
        if des is None or self._unsub is not None:
            return
        self._unsub = des.subscribe(
            self._on_event,
            event_types={"state_changed", "timer.finished"},
            domains=_EVENT_DOMAINS | {"timer"},
            name="event-trigger")

    # ------------------------------------------------------------------
    # 事件通路：state_changed + timer.finished
    # ------------------------------------------------------------------

    async def _on_event(self, event: dict) -> None:
        event_type = str(event.get("event_type", ""))
        entity_id = str(event.get("entity_id", ""))
        domain = str(event.get("domain", "") or entity_id.split(".", 1)[0])
        new_state = event.get("new_state") or {}
        attrs = new_state.get("attributes") or {}
        name = str(attrs.get("friendly_name") or entity_id)
        value = str(new_state.get("state", ""))

        if event_type == "timer.finished":
            ctx = f"事件：计时器「{name}」倒计时结束"
            await self._fire("helper", ctx)
            return
        if domain in ("person", "device_tracker"):
            verb = "到家" if value == "home" else "离家"
            ctx = f"事件：{name} {verb}（当前状态：{value}）"
            await self._fire("presence", ctx)
        elif domain == "sun":
            direction = "日出" if value == "above_horizon" else "日落"
            ctx = f"事件：{direction}（太阳{'升到地平线上' if value == 'above_horizon' else '落到地平线下'}）"
            await self._fire("sun", ctx)
        elif domain == "input_boolean":
            ctx = f"事件：虚拟开关「{name}」切换为 {'开' if value == 'on' else '关'}"
            await self._fire("helper", ctx)
        # 其余 domain（订阅过滤器已限定，防御性兜底）静默忽略

    async def _fire(self, rule_type: str, event_ctx: str) -> None:
        """触发单类型规则评估。异常隔离：评估失败不逃逸回事件总线。"""
        if self._automation_service is None:
            return
        try:
            applied = await self._automation_service.evaluate(
                frames=None, camera_id="",
                rule_types=(rule_type,), event_context={"event": event_ctx},
            )
            if applied:
                logger.info("Event-triggered rules applied: type=%s count=%d",
                            rule_type, len(applied))
        except Exception:
            logger.exception("event-trigger evaluate failed (type=%s)", rule_type)

    # ------------------------------------------------------------------
    # calendar 轮询通路
    # ------------------------------------------------------------------

    async def _calendar_loop(self) -> None:
        await asyncio.sleep(60)  # 启动缓冲：等规则/HA 就绪
        while True:
            try:
                await self._poll_calendars()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("calendar poll failed", exc_info=True)
            await asyncio.sleep(_CALENDAR_POLL_SECONDS)

    async def _poll_calendars(self, now: datetime | None = None) -> None:
        """拉取各日历的日程，对齐到点的 start/end 触发 calendar 规则。

        now 参数仅测试注入用。窗口 = (now - 轮询周期 - 60s, now]：多留 60s 容忍
        轮询抖动，配合 _fired_calendar 去重保证每事件每相位只触发一次。
        """
        if self._ha_service is None or self._automation_service is None:
            return
        client = getattr(self._ha_service, "_client", None)
        if client is None or not hasattr(client, "calendar_events"):
            return
        # 全程 tz-aware：HA calendar 接口要求带时区 ISO，moment 对比也用 aware
        now = now or datetime.now().astimezone()
        if now.tzinfo is None:
            now = now.astimezone()
        window_start = now - timedelta(seconds=_CALENDAR_POLL_SECONDS + 60)
        entities = self._calendar_entities()
        for entity_id in entities:
            try:
                events = await client.calendar_events(
                    entity_id,
                    start=window_start.isoformat(),
                    end=(now + timedelta(seconds=1)).isoformat(),
                )
            except Exception:
                logger.debug("calendar_events failed for %s", entity_id, exc_info=True)
                continue
            for ev in events or []:
                await self._maybe_fire_calendar_event(entity_id, ev, now)

    def _calendar_entities(self) -> list[str]:
        """参与触发的日历实体：automation.calendar_entities 显式配置优先。

        返回空列表时跳过——自动探测所有 calendar 实体容易把「垃圾日历」也接进来，
        日历驱动是显式 opt-in 语义（与摄像头 ha_entity 的显式绑定同哲学）。
        """
        configured = get_config("automation.calendar_entities", []) or []
        return [str(e).strip() for e in configured if str(e).strip()]

    async def _maybe_fire_calendar_event(
        self, entity_id: str, ev: dict, now: datetime,
    ) -> None:
        """单个日程事件的 start/end 到点判定 + 去重触发。"""
        summary = str(ev.get("summary", "") or "日程")
        uid = str(ev.get("uid") or f"{summary}@{ev.get('start')}")
        name = entity_id.split(".", 1)[-1]
        window = _CALENDAR_POLL_SECONDS + 60  # 容忍轮询抖动
        for phase in ("start", "end"):
            marker = (ev.get(phase) or {}).get("dateTime")
            if not marker:
                continue
            try:
                moment = datetime.fromisoformat(str(marker))
            except ValueError:
                continue
            if moment.tzinfo is None:  # HA dateTime 正常带时区；缺时区按本地补齐
                moment = moment.astimezone()
            elapsed = (now - moment).total_seconds()
            if not (0 <= elapsed <= window):
                continue
            fired_key = (entity_id, uid, phase)
            if fired_key in self._fired_calendar:
                continue
            self._fired_calendar.add(fired_key)
            if len(self._fired_calendar) > 400:  # 简单防泄漏：超限清一半老条目
                for old in list(self._fired_calendar)[:200]:
                    self._fired_calendar.discard(old)
            verb = "开始" if phase == "start" else "结束"
            await self._fire("calendar", f"事件：日历「{name}」的日程「{summary}」{verb}")
            return  # 同一事件一次只触发一个相位
