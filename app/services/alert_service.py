"""离线告警服务 — 摄像头离线 / HA 断连 / 定时任务失败 / 插件熔断的家庭通知。

设计约束（与插件完全解耦）：
- 本服务是核心自包含模块（模块级单例，模式同 metrics_service），核心代码
  零 import 插件代码；通知渠道用 Notifier 反向注册——宿主集成/插件**自愿**
  注册自己为渠道（如飞书 webhook），不在线时告警自动降级为仅日志 + WS 推送。
- 所有公开方法吞异常（记日志）：告警路径的任何失败绝不影响主流程。
- 同一 source 的告警有 30 分钟冷却（防摄像头反复重连时的告警风暴），
  恢复时发一条 resolve 通知。

事件流落 family_events 表（告警/恢复/任务成败/自动化触发），是家庭周报的
数据源。启用开关：alerts.enabled（默认 true）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from ..core.config import get_config

logger = logging.getLogger(__name__)

# 通知渠道：名字 → async fn(message: str, level: str)
Notifier = Callable[[str, str], Awaitable[None]]

_ALERT_COOLDOWN_SECONDS = 30 * 60
_MONITOR_INTERVAL_SECONDS = 60


class AlertService:
    def __init__(self) -> None:
        self._notifiers: dict[str, Notifier] = {}
        # source → {"alerted_at": ts, "active": bool}
        self._active: dict[str, dict] = {}
        self._monitor_task: asyncio.Task | None = None
        self._monitor_tasks: set[asyncio.Task] = set()
        self._camera_manager: Any = None
        self._health_checker: Any = None
        self._camera_offline_ticks: dict[str, int] = {}
        self._ha_down_ticks = 0
        self._enabled = True

    # ------------------------------------------------------------------
    # 装配（bind 由 main lifespan 调用；start 后才真正开始监控）
    # ------------------------------------------------------------------

    def bind(self, camera_manager: Any = None, health_checker: Any = None) -> None:
        """注入运行时引用（可选依赖，None 时对应监控项自动跳过）。"""
        self._camera_manager = camera_manager
        self._health_checker = health_checker

    def register_notifier(self, name: str, fn: Notifier) -> None:
        """注册通知渠道（宿主集成/插件自愿调用；重名覆盖）。"""
        self._notifiers[name] = fn
        logger.info("Alert notifier registered: %s", name)

    def unregister_notifier(self, name: str) -> None:
        self._notifiers.pop(name, None)

    @property
    def notifiers(self) -> list[str]:
        return list(self._notifiers)

    # ------------------------------------------------------------------
    # 事件入口（全部安全：异常吞掉只记日志）
    # ------------------------------------------------------------------

    async def notify(self, source: str, message: str, level: str = "warning") -> None:
        """发一条告警（同 source 30 分钟冷却）。"""
        try:
            if not self._is_enabled():
                return
            now = time.time()
            state = self._active.get(source)
            if state and now - state.get("alerted_at", 0) < _ALERT_COOLDOWN_SECONDS:
                return  # 冷却中，不重复打扰
            self._active[source] = {"alerted_at": now, "active": True}
            await self._record("alert", source, message)
            logger.warning("[Alert] %s: %s", source, message)
            await self._broadcast(message, level)
        except Exception:  # noqa: BLE001
            logger.exception("alert_service.notify failed (source=%s)", source)

    async def resolve(self, source: str, message: str = "") -> None:
        """恢复通知（此前必须处于 active 告警态，否则静默 no-op）。"""
        try:
            state = self._active.pop(source, None)
            if not state or not state.get("active"):
                return
            text = message or f"{source} 已恢复"
            await self._record("alert_resolved", source, text)
            logger.info("[Alert resolved] %s: %s", source, text)
            await self._broadcast(f"✅ {text}", "info")
        except Exception:  # noqa: BLE001
            logger.exception("alert_service.resolve failed (source=%s)", source)

    async def record(self, kind: str, source: str, message: str) -> None:
        """只落库不广播（任务成败/自动化触发等周报数据用）。"""
        try:
            await self._record(kind, source, message)
        except Exception:  # noqa: BLE001
            logger.exception("alert_service.record failed (kind=%s)", kind)

    # ------------------------------------------------------------------
    # 监控循环：摄像头离线 + HA 断连（轮询，不侵入被监控对象）
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._enabled = self._is_enabled()
        if not self._enabled:
            logger.info("Alert service disabled (alerts.enabled=false)")
            return
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop(), name="alert-monitor")
            logger.info("Alert service started (notifiers=%s)", list(self._notifiers))

    async def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        for t in list(self._monitor_tasks):
            t.cancel()

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(_MONITOR_INTERVAL_SECONDS)
            try:
                await self._check_cameras()
                await self._check_ha()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("alert monitor tick failed")

    async def _check_cameras(self) -> None:
        cm = self._camera_manager
        list_cameras = getattr(cm, "list_cameras", None)
        if cm is None or not callable(list_cameras):
            return
        try:
            cameras = list_cameras()
        except Exception:  # noqa: BLE001
            return
        if not cameras:
            return
        online_ids = set()
        for cam in cameras:
            cid = str(cam.get("id", ""))
            name = str(cam.get("name", "") or cid)
            state = cm.get_state(cid) if cid else {}
            if state.get("camera_opened"):
                online_ids.add(cid)
                self._camera_offline_ticks.pop(cid, None)
                await self.resolve(f"camera:{cid}", f"摄像头「{name}」已恢复在线")
            else:
                ticks = self._camera_offline_ticks.get(cid, 0) + 1
                self._camera_offline_ticks[cid] = ticks
                # 连续 2 个周期离线才告警（瞬时重连不打扰）
                if ticks >= 2:
                    await self.notify(f"camera:{cid}", f"摄像头「{name}」已离线（视觉自动化停止工作）")

    async def _check_ha(self) -> None:
        hc = self._health_checker
        if hc is None:
            return
        if not getattr(hc, "ha_available", True):
            self._ha_down_ticks += 1
            if self._ha_down_ticks >= 3:  # ~3 分钟不可用
                await self.notify("ha:connection", "Home Assistant 连接不可用（设备控制已中断）")
        else:
            self._ha_down_ticks = 0
            await self.resolve("ha:connection", "Home Assistant 连接已恢复")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _is_enabled(self) -> bool:
        try:
            return bool(get_config("alerts.enabled", True))
        except Exception:  # noqa: BLE001
            return True

    async def _record(self, kind: str, source: str, message: str) -> None:
        from ..core.database import Database
        db = Database.get()
        if db is not None:
            await db.family_event_add(kind, source, message)

    async def _broadcast(self, message: str, level: str) -> None:
        """推给全部注册渠道 + 在线 WS 用户。单渠道失败不影响其他。"""
        await self._dispatch_notifiers(message, level)
        try:
            from ..core import ws_registry
            await ws_registry.push_to_all({"type": "alert", "level": level, "message": message})
        except Exception:  # noqa: BLE001
            logger.debug("alert ws push failed (no online users?)", exc_info=True)

    async def broadcast_report(self, text: str) -> None:
        """推送周报等周期性内容（不带告警语义，level=info）。"""
        try:
            await self._dispatch_notifiers(text, "info")
            from ..core import ws_registry
            await ws_registry.push_to_all({"type": "report", "message": text})
        except Exception:  # noqa: BLE001
            logger.debug("report broadcast failed", exc_info=True)

    async def _dispatch_notifiers(self, message: str, level: str) -> None:
        """逐渠道推送。单渠道失败/超时不影响其他渠道与主流程。"""
        for name, fn in list(self._notifiers.items()):
            try:
                await asyncio.wait_for(fn(message, level), timeout=10.0)
            except Exception:  # noqa: BLE001
                logger.warning("alert notifier %s dispatch failed", name, exc_info=True)


# 模块级单例（模式同 metrics_service）：hook 点直接 import 使用，零构造接线
alert_service = AlertService()
