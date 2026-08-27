from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.database import Database
from ..schema.chat_schema import Event, Instruction

logger = logging.getLogger(__name__)

# 会话历史上限：超过后截断旧条目，防止内存和序列化开销随对话长度线性增长
_MAX_HISTORY_EVENTS = 100
_MAX_HISTORY_INSTRUCTIONS = 200
_MAX_MODEL_MESSAGES = 100
_MAX_SUMMARIES = 10

# 这些指令只进调试列表;ToastStream 在前端被映射为可见消息而非 debug,故重建 debug_events 时跳过
_DEBUG_SKIP = {"Template.ToastStream"}


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class SessionState:
    session_id: str
    request_id: str
    user_id: str = ""
    history_events: list[Event] = field(default_factory=list)
    history_instructions: list[Instruction] = field(default_factory=list)
    model_messages: list[dict[str, Any]] = field(default_factory=list)
    summaries: list[dict[str, Any]] = field(default_factory=list)
    latest_visual_state: dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=_now_ms)
    updated_at: int = field(default_factory=_now_ms)

    def title(self) -> str:
        """从首条用户消息推导标题,无则回退到 id。"""
        for message in self.model_messages:
            if message.get("role") == "user":
                text = str(message.get("content", "")).strip()
                if text:
                    return text[:30]
        return self.session_id

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "title": self.title(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.model_messages),
        }

    def visible_messages(self) -> list[dict[str, Any]]:
        """从 model_messages 重建前端可见消息,并附 message_id(下标)供 fork 截断。"""
        result: list[dict[str, Any]] = []
        for index, message in enumerate(self.model_messages):
            result.append(
                {
                    "role": message.get("role", "assistant"),
                    "content": message.get("content", ""),
                    "message_id": str(index),
                }
            )
        return result

    def debug_events(self) -> list[dict[str, Any]]:
        """从 history_instructions 重建调试事件,跳过前端不计入 debug 的 ToastStream。"""
        events: list[dict[str, Any]] = []
        for instruction in self.history_instructions:
            header = instruction.header
            event_type = f"{header.namespace}.{header.name}"
            if event_type in _DEBUG_SKIP:
                continue
            events.append({"type": event_type, "payload": instruction.payload})
        return events

    def detail(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "title": self.title(),
            "visible_messages": self.visible_messages(),
            "debug_events": self.debug_events(),
            "summaries": self.summaries,
        }


class SessionStore:
    """会话存储：内存缓存 + SQLite 持久化（sessions 表）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()
        self._loaded = False
        self._pending_tasks: set[asyncio.Task] = set()
        # 每 session 的保存链尾任务：同会话的写入严格按提交顺序落库。
        # fire-and-forget + 失败重试(sleep)下，两次保存可能乱序——第 1 个写
        # 失败进入重试等待时第 2 个(新快照)先落库，随后旧快照覆盖新数据；
        # delete 也可能被先前排队的 save"复活"。链式等待消除这两类竞态。
        self._save_chains: dict[str, asyncio.Task] = {}

    async def load_from_db(self, user_id: str = "") -> None:
        """从 SQLite 加载会话数据到内存缓存。"""
        if self._loaded:
            return
        try:
            db = Database.get()
            sessions_data = await db.sessions_all(user_id=user_id)
            for data in sessions_data:
                session = self._deserialize_session(data)
                self._sessions[session.session_id] = session
            logger.info("Loaded %d sessions from database", len(sessions_data))
        except Exception:
            logger.warning("Failed to load sessions from database, starting fresh", exc_info=True)
        self._loaded = True
        self._evict_overflow_locked()

    def _save_session_async(self, session: SessionState) -> None:
        """异步持久化会话到 SQLite（带重试）。

        注意：调用方应在持有 self._lock 时调用，以保证序列化期间 session
        不被其他协程修改；序列化在锁内同步完成，仅 DB 写入异步执行。
        同会话多次保存按提交顺序排队（见 self._save_chains）。
        """
        try:
            data = self._serialize_session(session)
            prev = self._save_chains.get(session.session_id)
            task = asyncio.create_task(
                self._save_with_retry(session.session_id, data, session.user_id, prev=prev))
            self._save_chains[session.session_id] = task
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            task.add_done_callback(self._log_task_error)
            task.add_done_callback(
                lambda t, sid=session.session_id: self._pop_save_chain(sid, t))
        except RuntimeError:
            logger.debug("Cannot persist session %s: no running event loop", session.session_id)

    def _pop_save_chain(self, session_id: str, task: asyncio.Task) -> None:
        """链尾任务结束时清理；若已被更新的保存接管（链尾非自己）则不动。"""
        if self._save_chains.get(session_id) is task:
            self._save_chains.pop(session_id, None)

    @staticmethod
    async def _await_prev(prev: asyncio.Task | None) -> None:
        """等前序链任务完成（吞掉其异常——新快照本就该覆盖旧数据）。"""
        if prev is None or prev.cancelled():
            return
        try:
            await prev
        except asyncio.CancelledError:
            # prev 被取消（停机清理等）：跳过等待直接写。但若是我们自己
            # 同时被取消，必须让 CancelledError 继续传播。
            cur = asyncio.current_task()
            if cur is not None and cur.cancelling():
                raise
        except Exception:  # noqa: BLE001
            pass

    async def _save_with_retry(self, session_id: str, data: dict, user_id: str,
                               max_retries: int = 2,
                               prev: asyncio.Task | None = None) -> None:
        """带重试的 DB 写入（排在前序链任务之后，保证同会话写序）。"""
        await self._await_prev(prev)
        db = Database.get()
        for attempt in range(max_retries + 1):
            try:
                await db.sessions_upsert(session_id, data, user_id=user_id)
                return
            except Exception:
                if attempt < max_retries:
                    logger.warning("Session save failed (attempt %d/%d), retrying: %s",
                                   attempt + 1, max_retries + 1, session_id)
                    await asyncio.sleep(0.1 * (attempt + 1))
                else:
                    logger.error("Session save failed after %d retries: %s", max_retries + 1, session_id, exc_info=True)

    def _delete_session_async(self, session_id: str) -> None:
        """异步从 SQLite 删除会话（排在同会话所有待写保存之后，防"复活"）。"""
        try:
            db = Database.get()
            prev = self._save_chains.pop(session_id, None)
            task = asyncio.create_task(self._delete_after_prev(prev, db, session_id))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            task.add_done_callback(self._log_task_error)
        except RuntimeError:
            logger.debug("Cannot delete session %s: no running event loop", session_id)

    @staticmethod
    async def _delete_after_prev(prev: asyncio.Task | None, db, session_id: str) -> None:
        await SessionStore._await_prev(prev)
        await db.sessions_delete(session_id)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """记录后台任务的异常，避免静默丢失。"""
        if not task.cancelled() and task.exception() is not None:
            logger.error("Background DB task failed: %s", task.exception(), exc_info=task.exception())

    # ------------------------------------------------------------------
    # 会话治理：每用户保留最近 N 个，超出淘汰最旧
    # ------------------------------------------------------------------

    @staticmethod
    def _max_sessions_per_user() -> int:
        """上限从 config 读取（storage.max_sessions_per_user，默认 50；
        0 = 关闭淘汰）。7x24 长跑下无上限会话全量驻内存 + 每条消息全量
        JSON upsert，数月后启动变慢、内存线性上涨。"""
        from ..core.config import get_config
        try:
            return int(get_config("storage.max_sessions_per_user", 50))
        except (TypeError, ValueError):
            return 50

    def _evict_overflow_locked(self) -> int:
        """按用户淘汰最旧会话（内存 + DB）。须持 self._lock 调用。

        updated_at 在每次 get_or_create/store_session 都会刷新，活跃会话
        天然沉顶；被淘汰的都是长期不用的旧会话。淘汰的 DB 删除走与保存
        相同的链式队列，不会与待写保存竞态复活。
        """
        limit = self._max_sessions_per_user()
        if limit <= 0:
            return 0
        by_user: dict[str, list[SessionState]] = {}
        for s in self._sessions.values():
            by_user.setdefault(s.user_id or "", []).append(s)
        evicted = 0
        for sessions in by_user.values():
            if len(sessions) <= limit:
                continue
            sessions.sort(key=lambda s: s.updated_at)
            for old in sessions[:-limit]:
                self._sessions.pop(old.session_id, None)
                self._delete_session_async(old.session_id)
                evicted += 1
        if evicted:
            logger.info("Session eviction: %d sessions beyond per-user limit %d",
                        evicted, limit)
        return evicted

    async def shutdown(self) -> None:
        """等待所有 pending 的 DB 写入任务完成。"""
        if self._pending_tasks:
            logger.info("SessionStore shutdown: waiting for %d pending tasks", len(self._pending_tasks))
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

    async def get_or_create(self, session_id: str, request_id: str, user_id: str = "") -> SessionState:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = SessionState(session_id=session_id, request_id=request_id, user_id=user_id)
                self._sessions[session_id] = session
            else:
                session.request_id = request_id
                if user_id:
                    session.user_id = user_id
            session.updated_at = _now_ms()
            self._save_session_async(session)
        return session

    async def create_session(self, user_id: str = "") -> SessionState:
        """显式新建一个空会话(供 POST /api/sessions)。"""
        session_id = str(uuid.uuid4())
        async with self._lock:
            session = SessionState(session_id=session_id, request_id=str(uuid.uuid4()), user_id=user_id)
            self._sessions[session_id] = session
            self._save_session_async(session)
        return session

    async def delete_session(self, session_id: str) -> bool:
        async with self._lock:
            existed = self._sessions.pop(session_id, None) is not None
        if existed:
            self._delete_session_async(session_id)
        return existed

    async def delete_all(self, user_id: str = "") -> int:
        """删除所有会话（可按 user_id 过滤），返回删除条数。"""
        async with self._lock:
            if user_id:
                to_delete = [sid for sid, s in self._sessions.items() if s.user_id == user_id]
            else:
                to_delete = list(self._sessions.keys())
            for sid in to_delete:
                self._sessions.pop(sid, None)
        # 异步删除 DB 记录
        if to_delete:
            try:
                db = Database.get()
                task = asyncio.create_task(db.sessions_delete_all(user_id=user_id))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
                task.add_done_callback(self._log_task_error)
            except RuntimeError:
                logger.debug("Cannot delete all sessions: no running event loop")
        return len(to_delete)

    async def fork_session(self, session_id: str, message_id: str, user_id: str = "") -> SessionState | None:
        """复制源会话直到 message_id(下标,含)为止,作为新分支会话。

        分支只继承对话消息(model_messages),history_events/instructions 从空开始,
        作为一条全新的对话支线。
        """
        async with self._lock:
            source = self._sessions.get(session_id)
            if source is None:
                return None
            try:
                cut = int(message_id)
            except (TypeError, ValueError):
                cut = len(source.model_messages) - 1
            cut = max(-1, min(cut, len(source.model_messages) - 1))
            new_id = str(uuid.uuid4())
            forked = SessionState(
                session_id=new_id,
                request_id=str(uuid.uuid4()),
                user_id=user_id or source.user_id,
                model_messages=[dict(m) for m in source.model_messages[: cut + 1]],
                summaries=[dict(s) for s in source.summaries],
                latest_visual_state=dict(source.latest_visual_state),
            )
            self._sessions[new_id] = forked
            self._save_session_async(forked)
        return forked

    async def list_summaries(self, user_id: str = "") -> list[dict[str, Any]]:
        async with self._lock:
            if user_id:
                sessions = [s for s in self._sessions.values() if s.user_id == user_id]
            else:
                sessions = list(self._sessions.values())
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return [s.summary() for s in sessions]

    async def get_session(self, session_id: str) -> SessionState | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def undo_last_message(self, session_id: str) -> bool:
        """删除最后一条用户-助手消息对，实现撤销。"""
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session or len(session.model_messages) < 2:
                return False
            session.model_messages = session.model_messages[:-2]
            session.updated_at = _now_ms()
            self._save_session_async(session)
        return True

    async def clear_messages(self, session_id: str) -> bool:
        """清空所有消息但保留会话元数据。"""
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return False
            session.model_messages = []
            session.history_events = []
            session.history_instructions = []
            session.summaries = []
            session.updated_at = _now_ms()
            self._save_session_async(session)
        return True

    async def store_session(self, session: SessionState) -> None:
        async with self._lock:
            session.updated_at = _now_ms()
            self._truncate_history(session)
            self._sessions[session.session_id] = session
            self._save_session_async(session)
            self._evict_overflow_locked()

    @staticmethod
    def _truncate_history(session: SessionState) -> None:
        """截断过长的历史列表，保留最近的条目。"""
        if len(session.history_events) > _MAX_HISTORY_EVENTS:
            session.history_events = session.history_events[-_MAX_HISTORY_EVENTS:]
        if len(session.history_instructions) > _MAX_HISTORY_INSTRUCTIONS:
            session.history_instructions = session.history_instructions[-_MAX_HISTORY_INSTRUCTIONS:]
        if len(session.model_messages) > _MAX_MODEL_MESSAGES:
            session.model_messages = session.model_messages[-_MAX_MODEL_MESSAGES:]
        if len(session.summaries) > _MAX_SUMMARIES:
            session.summaries = session.summaries[-_MAX_SUMMARIES:]

    def _serialize_session(self, session: SessionState) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "request_id": session.request_id,
            "user_id": session.user_id,
            "history_events": [event.model_dump() for event in session.history_events],
            "history_instructions": [instruction.model_dump() for instruction in session.history_instructions],
            "model_messages": session.model_messages,
            "summaries": session.summaries,
            "latest_visual_state": session.latest_visual_state,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }

    def _deserialize_session(self, data: dict[str, Any]) -> SessionState:
        return SessionState(
            session_id=data["session_id"],
            request_id=data.get("request_id", data["session_id"]),
            user_id=data.get("user_id", ""),
            history_events=[Event.model_validate(item) for item in data.get("history_events", [])],
            history_instructions=[Instruction.model_validate(item) for item in data.get("history_instructions", [])],
            model_messages=data.get("model_messages", []),
            summaries=data.get("summaries", []),
            latest_visual_state=data.get("latest_visual_state", {}),
            created_at=data.get("created_at", _now_ms()),
            updated_at=data.get("updated_at", data.get("created_at", _now_ms())),
        )
