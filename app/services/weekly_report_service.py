"""家庭周报 — 聚合一周 family_events，LLM 生成人话总结，推送 + 留存。

数据源：family_events 表（告警/恢复/任务成败/自动化触发/插件熔断，由
alert_service 与各 hook 点写入）。生成走 summary 角色的 LLM 客户端
（有全局 key 即可，无 per-user 依赖）。

调度：自带轻量每日检查循环（不进 scheduler 任务列表，避免系统行为出现在
用户的定时任务页）。每周日 weekly_report.hour（默认 20 点）生成一次。

开关（默认保守）：weekly_report.enabled=false（生成消耗 LLM token，用户
明确开启才生效）、weekly_report.hour=20。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from ..core.config import get_config
from ..core.database import Database

logger = logging.getLogger(__name__)

_KV_REPORT_KEY = "weekly_report:last"

_PROMPT = (
    "你是家庭智能管家的周报撰写者。根据过去一周的事件记录，用中文写一份简短的"
    "家庭周报（200 字以内），口吻亲切务实。结构：先一句总评，然后按'自动化与定时任务'"
    "'设备与连接'分组提炼重点（执行了多少次自动化、定时任务成功率、有无异常告警），"
    "最后一句提醒。只输出周报正文，不要寒暄。\n\n事件记录：\n{events}"
)


class WeeklyReportService:
    def __init__(self, llm_chat_client: Any = None) -> None:
        self._llm = llm_chat_client
        self._loop_task: asyncio.Task | None = None

    def set_llm_client(self, llm_chat_client: Any) -> None:
        self._llm = llm_chat_client

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self._is_enabled():
            logger.info("Weekly report disabled (weekly_report.enabled=false)")
            return
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._daily_check(), name="weekly-report")
            logger.info("Weekly report service started")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def _daily_check(self) -> None:
        """每天检查一次是否到本周报告时点（周日 hour 点，错过当天补偿生成）。"""
        while True:
            try:
                now = datetime.now()
                target = self._this_weeks_target(now)
                if now >= target and not await self._already_generated(target):
                    await self.generate()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("weekly report daily check failed")
            await asyncio.sleep(6 * 3600)  # 6 小时查一次（重启后最多延迟 6h 补生成）

    @staticmethod
    def _this_weeks_target(now: datetime) -> datetime:
        """本周日的 report hour（若今天是周日且已过 hour，即本周目标时刻）。"""
        hour = int(get_config("weekly_report.hour", 20) or 20)
        days_ahead = (6 - now.weekday()) % 7  # 周日=6
        target = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=0, second=0, microsecond=0)
        return target

    async def _already_generated(self, target: datetime) -> bool:
        try:
            row = await Database.get().kv_get(_KV_REPORT_KEY)
            if row and float(row) >= target.timestamp():
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    async def generate(self) -> dict:
        """聚合近 7 天事件 → LLM 总结 → 推送 + 落 kv。失败抛异常给调用方。"""
        since = int((time.time() - 7 * 24 * 3600) * 1000)
        events = await Database.get().family_events_since(since)
        if not events:
            logger.info("Weekly report: no events in the last 7 days, skip")
            return {"generated": False, "reason": "no_events"}

        # 事件转紧凑文本（LLM 输入限长：最多取最近 500 条）
        lines = [
            f"{datetime.fromtimestamp(e['created_at']/1000).strftime('%m-%d %H:%M')} "
            f"[{e['kind']}] {e['source']} {e['message']}"
            for e in events[-500:]
        ]
        stats = self._summarize_stats(events)

        text = ""
        if self._llm is not None and getattr(self._llm, "enabled", False):
            try:
                timeout = int(get_config("llm.summary_timeout_seconds", 30) or 30)
                text = await self._llm.chat(
                    [{"role": "user", "content": _PROMPT.format(events="\n".join(lines))}],
                    timeout,
                )
                text = str(text).strip()
            except Exception:  # noqa: BLE001
                logger.warning("Weekly report LLM summarize failed, fallback to stats", exc_info=True)
                text = ""
        if not text:
            text = stats  # LLM 不可用时退化为纯统计文本

        report = {
            "generated": True,
            "generated_at": int(time.time() * 1000),
            "week": datetime.now().strftime("%Y-%m-%d"),
            "stats": stats,
            "text": text,
        }
        await Database.get().kv_set(_KV_REPORT_KEY, str(time.time()))
        # 落一份完整报告到 family_events（kind=weekly_report，前端历史可查）
        await Database.get().family_event_add("weekly_report", "report", text)
        # 推送（notifier 渠道 + 在线 WS，全部 try/except）
        from .alert_service import alert_service
        await alert_service.broadcast_report(text)
        logger.info("Weekly report generated (%d events)", len(events))
        return report

    @staticmethod
    def _summarize_stats(events: list[dict]) -> str:
        counts: dict[str, int] = {}
        for e in events:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        automation = counts.get("automation", 0)
        task_ok = counts.get("task_success", 0)
        task_fail = counts.get("task_failed", 0)
        alerts = counts.get("alert", 0)
        parts = [
            f"本周自动化触发 {automation} 次",
            f"定时任务执行 {task_ok + task_fail} 次"
            + (f"（失败 {task_fail} 次）" if task_fail else "，全部成功"),
            f"告警 {alerts} 次" + (f"，已恢复 {counts.get('alert_resolved', 0)} 次" if alerts else ""),
        ]
        return "；".join(parts) + "。"

    def _is_enabled(self) -> bool:
        try:
            return bool(get_config("weekly_report.enabled", False))
        except Exception:  # noqa: BLE001
            return False

    async def latest_report(self) -> dict | None:
        """取最近一份周报文本（family_events 里最后一次 weekly_report）。"""
        events = await Database.get().family_events_since(0, kinds=["weekly_report"])
        if not events:
            return None
        e = events[-1]
        return {"generated_at": e["created_at"], "text": e["message"]}
