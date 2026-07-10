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
    latest_tool_result: dict[str, Any] | None = None
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
    def __init__(self, storage_path: str | None = None) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()
        self._loaded = False
        self._pending_tasks: set[asyncio.Task] = set()

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

    def _save_session_async(self, session: SessionState) -> None:
        """异步持久化会话到 SQLite（带重试）。

        注意：调用方应在持有 self._lock 时调用，以保证序列化期间 session
        不被其他协程修改；序列化在锁内同步完成，仅 DB 写入异步执行。
        """
        try:
            db = Database.get()
            data = self._serialize_session(session)
            task = asyncio.create_task(self._save_with_retry(session.session_id, data, session.user_id))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            task.add_done_callback(self._log_task_error)
        except RuntimeError:
            logger.debug("Cannot persist session %s: no running event loop", session.session_id)

    async def _save_with_retry(self, session_id: str, data: dict, user_id: str, max_retries: int = 2) -> None:
        """带重试的 DB 写入。"""
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
        """异步从 SQLite 删除会话。"""
        try:
            db = Database.get()
            task = asyncio.create_task(db.sessions_delete(session_id))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            task.add_done_callback(self._log_task_error)
        except RuntimeError:
            logger.debug("Cannot delete session %s: no running event loop", session_id)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """记录后台任务的异常，避免静默丢失。"""
        if not task.cancelled() and task.exception() is not None:
            logger.error("Background DB task failed: %s", task.exception(), exc_info=task.exception())

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
            "latest_tool_result": session.latest_tool_result,
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
            latest_tool_result=data.get("latest_tool_result"),
            created_at=data.get("created_at", _now_ms()),
            updated_at=data.get("updated_at", data.get("created_at", _now_ms())),
        )
