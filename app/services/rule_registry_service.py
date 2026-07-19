from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from ..core.database import Database
from ..core.exceptions import AppException
from ..utils.async_utils import TaskManager

logger = logging.getLogger(__name__)


@dataclass
class AutomationRule:
    id: str
    trigger: dict
    conditions: list[dict]
    actions: list[dict]
    summary: str
    enabled: bool
    created_at: int
    updated_at: int
    name: str = ""                                       # 规则名称
    condition: str = ""                                  # 自然语言条件,如"桌子上有鼠标"
    action_descriptions: list[str] = field(default_factory=list)  # 动作的人类可读描述
    cooldown_seconds: int = 10                           # 防重复触发冷却
    last_triggered_at: float = 0.0                       # 上次触发时间(秒级)
    user_id: str = ""                                    # 创建者，用于 per-user LLM key 解析；空表示老规则回退全局

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "trigger": self.trigger,
            "conditions": self.conditions,
            "condition": self.condition,
            "actions": self.actions,
            "action_descriptions": self.action_descriptions,
            "summary": self.summary,
            "enabled": self.enabled,
            "cooldown_seconds": self.cooldown_seconds,
            "last_triggered_at": self.last_triggered_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "user_id": self.user_id,
        }


class RuleRegistryService:
    def __init__(self) -> None:
        self._rules: list[AutomationRule] = []
        self._lock = threading.RLock()
        self._loaded = False
        # 使用统一的 TaskManager 管理后台任务
        self._task_manager = TaskManager()

    async def load_from_db(self) -> None:
        """从 SQLite 加载规则到内存缓存。"""
        with self._lock:
            if self._loaded:
                return
        try:
            db = Database.get()
            rules_data = await db.rules_all()
            with self._lock:
                for item in rules_data:
                    self._rules.append(
                        AutomationRule(
                            id=item.get("id") or str(uuid.uuid4()),
                            trigger=item.get("trigger", {}),
                            conditions=item.get("conditions", []),
                            actions=item.get("actions", []),
                            summary=item.get("summary", ""),
                            enabled=bool(item.get("enabled", True)),
                            created_at=int(item.get("created_at", time.time() * 1000)),
                            updated_at=int(item.get("updated_at", item.get("created_at", time.time() * 1000))),
                            name=item.get("name", ""),
                            condition=item.get("condition", ""),
                            action_descriptions=item.get("action_descriptions", []),
                            cooldown_seconds=int(item.get("cooldown_seconds", 10)),
                            last_triggered_at=float(item.get("last_triggered_at", 0.0)),
                            user_id=str(item.get("user_id", "")),
                        )
                    )
            logger.info("Loaded %d rules from database", len(rules_data))
        except Exception:
            logger.warning("Failed to load rules from database, starting fresh", exc_info=True)
        with self._lock:
            self._loaded = True

    def _spawn_task(self, coro) -> None:
        """创建后台任务并保存引用，完成后自动移除。"""
        try:
            self._task_manager.spawn(coro, on_done=self._log_task_error)
        except RuntimeError:
            logger.debug("Cannot create task: no running event loop")

    def _save_rule_async(self, rule: AutomationRule) -> None:
        """异步持久化单个规则到 SQLite。"""
        try:
            db = Database.get()
            self._spawn_task(db.rules_update(rule.id, rule.to_dict()))
        except RuntimeError:
            pass  # Database not initialized yet

    def _insert_rule_async(self, rule: AutomationRule) -> None:
        """异步插入新规则到 SQLite。"""
        try:
            db = Database.get()
            self._spawn_task(db.rules_insert(rule.id, rule.to_dict(), rule.user_id))
        except RuntimeError:
            pass

    def _delete_rule_async(self, rule_id: str) -> None:
        """异步从 SQLite 删除规则。"""
        try:
            db = Database.get()
            self._spawn_task(db.rules_delete(rule_id))
        except RuntimeError:
            pass

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """记录后台任务的异常，避免静默丢失。"""
        if not task.cancelled() and task.exception() is not None:
            logger.error("Background DB task failed: %s", task.exception(), exc_info=task.exception())

    def add_rule(self, rule: dict, user_id: str = "") -> dict:
        now = int(time.time() * 1000)
        normalized = AutomationRule(
            id=str(rule.get("id") or uuid.uuid4()),
            trigger=rule.get("trigger", {}),
            conditions=rule.get("conditions", []),
            actions=rule.get("actions", []),
            summary=rule.get("summary", ""),
            enabled=True,
            created_at=now,
            updated_at=now,
            name=str(rule.get("name", "")),
            condition=str(rule.get("condition", "")),
            action_descriptions=rule.get("action_descriptions", []),
            cooldown_seconds=int(rule.get("cooldown_seconds", 10)),
            last_triggered_at=0.0,
            user_id=str(user_id or rule.get("user_id", "")),
        )
        with self._lock:
            self._rules.append(normalized)
            self._insert_rule_async(normalized)
        return normalized.to_dict()

    def update_trigger_time(self, rule_id: str, ts: float) -> None:
        """记录规则上次触发时间(供 cooldown 防重复)。"""
        with self._lock:
            for rule in self._rules:
                if rule.id == rule_id:
                    rule.last_triggered_at = float(ts)
                    self._save_rule_async(rule)
                    return

    def list_rules(self) -> list[dict]:
        with self._lock:
            return [rule.to_dict() for rule in self._rules]

    def set_enabled(self, rule_id: str, enabled: bool) -> dict:
        with self._lock:
            for rule in self._rules:
                if rule.id == rule_id:
                    rule.enabled = bool(enabled)
                    rule.updated_at = int(time.time() * 1000)
                    self._save_rule_async(rule)
                    return rule.to_dict()
        raise AppException(f"规则不存在: {rule_id}", code="rule_not_found", http_status=404)

    def delete_rule(self, rule_id: str) -> dict:
        with self._lock:
            for index, rule in enumerate(self._rules):
                if rule.id == rule_id:
                    removed = self._rules.pop(index)
                    self._delete_rule_async(rule_id)
                    return removed.to_dict()
        raise AppException(f"规则不存在: {rule_id}", code="rule_not_found", http_status=404)
