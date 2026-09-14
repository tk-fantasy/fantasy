"""Tests for SessionState pure methods and SessionStore in-memory operations."""
from __future__ import annotations

import pytest

from app.services.session_store import SessionState, SessionStore


class TestSessionState:
    def _make_session(self, **kwargs) -> SessionState:
        defaults = {
            "session_id": "test-123",
            "request_id": "req-1",
        }
        defaults.update(kwargs)
        return SessionState(**defaults)

    def test_title_from_first_user_message(self):
        s = self._make_session()
        s.model_messages = [
            {"role": "user", "content": "打开床头灯"},
            {"role": "assistant", "content": "好的"},
        ]
        assert s.title() == "打开床头灯"

    def test_title_truncates_long(self):
        s = self._make_session()
        s.model_messages = [{"role": "user", "content": "a" * 50}]
        assert len(s.title()) == 30

    def test_title_fallback_to_session_id(self):
        s = self._make_session()
        s.model_messages = []
        assert s.title() == "test-123"

    def test_summary(self):
        s = self._make_session()
        s.model_messages = [{"role": "user", "content": "hi"}]
        result = s.summary()
        assert result["id"] == "test-123"
        assert result["message_count"] == 1

    def test_visible_messages(self):
        s = self._make_session()
        s.model_messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = s.visible_messages()
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["message_id"] == "0"
        assert result[1]["message_id"] == "1"

    def test_detail(self):
        s = self._make_session()
        s.model_messages = [{"role": "user", "content": "test"}]
        result = s.detail()
        assert result["id"] == "test-123"
        assert "visible_messages" in result


class TestSessionStoreInMemory:
    @pytest.mark.asyncio
    async def test_create_session(self):
        store = SessionStore()
        session = await store.create_session()
        assert session.session_id is not None
        assert len(store._sessions) == 1

    @pytest.mark.asyncio
    async def test_get_session(self):
        store = SessionStore()
        created = await store.create_session()
        fetched = await store.get_session(created.session_id)
        assert fetched is not None
        assert fetched.session_id == created.session_id

    @pytest.mark.asyncio
    async def test_get_nonexistent(self):
        store = SessionStore()
        assert await store.get_session("nonexistent") is None

    @pytest.mark.asyncio
    async def test_delete_session(self):
        store = SessionStore()
        session = await store.create_session()
        assert await store.delete_session(session.session_id) is True
        assert await store.get_session(session.session_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        store = SessionStore()
        assert await store.delete_session("nonexistent") is False

    @pytest.mark.asyncio
    async def test_list_summaries(self):
        store = SessionStore()
        await store.create_session()
        await store.create_session()
        summaries = await store.list_summaries()
        assert len(summaries) == 2

    @pytest.mark.asyncio
    async def test_get_or_create_existing(self):
        store = SessionStore()
        s1 = await store.create_session()
        s2 = await store.get_or_create(s1.session_id, "new-req")
        assert s2.session_id == s1.session_id

    @pytest.mark.asyncio
    async def test_get_or_create_new(self):
        store = SessionStore()
        session = await store.get_or_create("new-id", "req-1")
        assert session.session_id == "new-id"

    @pytest.mark.asyncio
    async def test_fork_session(self):
        store = SessionStore()
        original = await store.create_session()
        original.model_messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        forked = await store.fork_session(original.session_id, "0")
        assert forked is not None
        assert forked.session_id != original.session_id
        assert len(forked.model_messages) == 1

    @pytest.mark.asyncio
    async def test_fork_nonexistent(self):
        store = SessionStore()
        assert await store.fork_session("nonexistent", "0") is None

    @pytest.mark.asyncio
    async def test_undo_last_message(self):
        store = SessionStore()
        session = await store.create_session()
        session.model_messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "bye"},
            {"role": "assistant", "content": "goodbye"},
        ]
        assert await store.undo_last_message(session.session_id) is True
        assert len(session.model_messages) == 2
        assert session.model_messages[0]["content"] == "hi"
        assert session.model_messages[1]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_undo_too_few_messages(self):
        store = SessionStore()
        session = await store.create_session()
        session.model_messages = [{"role": "user", "content": "only one"}]
        assert await store.undo_last_message(session.session_id) is False
        assert len(session.model_messages) == 1

    @pytest.mark.asyncio
    async def test_undo_empty_session(self):
        store = SessionStore()
        session = await store.create_session()
        assert await store.undo_last_message(session.session_id) is False

    @pytest.mark.asyncio
    async def test_undo_nonexistent(self):
        store = SessionStore()
        assert await store.undo_last_message("nonexistent") is False

    @pytest.mark.asyncio
    async def test_clear_messages(self):
        store = SessionStore()
        session = await store.create_session()
        session.model_messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        session.summaries = ["some summary"]
        assert await store.clear_messages(session.session_id) is True
        assert session.model_messages == []
        assert session.summaries == []
        assert session.history_events == []
        assert session.history_instructions == []

    @pytest.mark.asyncio
    async def test_clear_nonexistent(self):
        store = SessionStore()
        assert await store.clear_messages("nonexistent") is False


class TestSessionOwnership:
    """会话归属不可变：他人 session_id 不能接管（此前轮首覆盖 user_id 的横向越权）。"""

    @pytest.mark.asyncio
    async def test_new_session_carries_user_id(self):
        store = SessionStore()
        s = await store.get_or_create("sid-1", "req-1", user_id="alice")
        assert s.user_id == "alice"

    @pytest.mark.asyncio
    async def test_owner_reentry_preserves_ownership(self):
        store = SessionStore()
        await store.get_or_create("sid-1", "req-1", user_id="alice")
        s = await store.get_or_create("sid-1", "req-2", user_id="alice")
        assert s.user_id == "alice"
        assert s.request_id == "req-2"

    @pytest.mark.asyncio
    async def test_foreign_user_rejected(self):
        from app.services.session_store import SessionOwnershipError

        store = SessionStore()
        await store.get_or_create("sid-1", "req-1", user_id="alice")
        with pytest.raises(SessionOwnershipError):
            await store.get_or_create("sid-1", "req-2", user_id="mallory")
        # 归属未被篡改，且会话仍然存在
        s = await store.get_session("sid-1")
        assert s is not None
        assert s.user_id == "alice"

    @pytest.mark.asyncio
    async def test_ownership_error_is_app_exception_403(self):
        from app.core.exceptions import AppException
        from app.services.session_store import SessionOwnershipError

        err = SessionOwnershipError("sid-1")
        assert isinstance(err, AppException)
        assert err.http_status == 403
        assert err.code == "forbidden"

    @pytest.mark.asyncio
    async def test_unowned_session_claimed_by_first_user(self):
        store = SessionStore()
        await store.create_session()  # 无归属（旧版数据路径）
        sessions = await store.list_summaries()
        sid = sessions[0]["id"]
        s = await store.get_or_create(sid, "req-1", user_id="alice")
        assert s.user_id == "alice"

    @pytest.mark.asyncio
    async def test_internal_call_without_user_id_keeps_ownership(self):
        """内部调用（user_id 为空，如后台任务）不覆盖归属也不触发越权。"""
        store = SessionStore()
        await store.get_or_create("sid-1", "req-1", user_id="alice")
        s = await store.get_or_create("sid-1", "req-2", user_id="")
        assert s.user_id == "alice"
