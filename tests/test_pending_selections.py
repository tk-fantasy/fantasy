"""Tests for pending_selections — 设备消歧待选草稿。

闸门判定 ambiguous/category_miss 时不执行、不猜，把 LLM 已解析好的动作挂成草稿；
本文件覆盖草稿生命周期（建/取/TTL/确认/取消/清理），执行本身由 executor 打桩。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.pending_rules import KIND_AUTOMATION_RULE, PENDING_TTL_SECONDS, pending_store
from app.services.pending_selections import (
    KIND_DEVICE_SELECTION,
    REASON_AMBIGUOUS,
    REASON_CATEGORY_MISS,
    cancel_selection,
    confirm_selection,
    create_selection_draft,
    drop_selection_drafts,
)

CANDIDATES = [
    {"entity_id": "light.a", "label": "床头灯", "domain": "light",
     "area_name": "卧室", "state": "off"},
    {"entity_id": "light.b", "label": "客厅吊灯", "domain": "light",
     "area_name": "客厅", "state": "off"},
]


def _session():
    return SimpleNamespace(pending_confirmations={})


def _draft(session, **overrides):
    payload = {
        "query": "开灯", "domain": "light", "service": "turn_on",
        "data": {}, "candidates": CANDIDATES, "reason": REASON_AMBIGUOUS,
    }
    payload.update(overrides)
    return create_selection_draft(session, **payload)


def _expire(session, pending_id):
    pending_store(session)[pending_id]["created_at"] = time.time() - PENDING_TTL_SECONDS - 1


class TestCreateAndLocate:
    def test_draft_carries_full_action_and_candidates(self):
        session = _session()
        pid = _draft(session)
        entry = pending_store(session)[pid]
        assert entry["kind"] == KIND_DEVICE_SELECTION
        assert entry["query"] == "开灯"
        assert entry["domain"] == "light"
        assert entry["service"] == "turn_on"
        assert entry["data"] == {}
        assert entry["candidates"] == CANDIDATES
        assert entry["reason"] == REASON_AMBIGUOUS

    def test_pending_ids_are_unique(self):
        session = _session()
        assert _draft(session) != _draft(session)
        assert len(pending_store(session)) == 2

    def test_category_miss_reason_preserved(self):
        """弹框文案要靠 reason 区分「挑一个」和「没找到 X」。"""
        session = _session()
        pid = _draft(session, reason=REASON_CATEGORY_MISS, query="月球的灯")
        assert pending_store(session)[pid]["reason"] == REASON_CATEGORY_MISS

    def test_works_on_session_without_attr(self):
        """反序列化回来的旧会话对象可能没有 pending_confirmations 字段。"""
        session = SimpleNamespace()
        pid = _draft(session)
        assert pending_store(session)[pid]["kind"] == KIND_DEVICE_SELECTION


class TestConfirm:
    @pytest.mark.asyncio
    async def test_confirm_executes_and_pops_draft(self):
        session = _session()
        pid = _draft(session)
        executor = AsyncMock(return_value={"success": True})
        result = await confirm_selection(session, pid, ["light.b", "light.a"], executor)
        assert result["ok"] is True
        assert result["pending_id"] == pid
        assert pid not in pending_store(session)
        # executor 拿到草稿 + 逗号拼接的 entity_id（HA 批量口径）
        draft_arg, entity_id = executor.await_args.args
        assert entity_id == "light.b,light.a"
        assert draft_arg["service"] == "turn_on"

    @pytest.mark.asyncio
    async def test_confirm_returns_selected_names(self):
        """REST 端点要用它拼「我已通过界面选择：X、Y」的合成消息。"""
        session = _session()
        pid = _draft(session)
        result = await confirm_selection(session, pid, ["light.a"], AsyncMock(return_value={}))
        assert result["names"] == ["床头灯"]
        assert result["entity_ids"] == ["light.a"]

    @pytest.mark.asyncio
    async def test_selection_outside_candidates_rejected(self):
        """用户只能从候选里挑——否则弹框就成了绕过闸门的后门。"""
        session = _session()
        pid = _draft(session)
        executor = AsyncMock()
        result = await confirm_selection(session, pid, ["light.a", "switch.evil"], executor)
        assert result["ok"] is False
        assert result["reason"] == "invalid_selection"
        assert "switch.evil" in result["error"]
        executor.assert_not_awaited()
        # 草稿不摘除：用户可以在 TTL 内重选
        assert pid in pending_store(session)

    @pytest.mark.asyncio
    async def test_empty_selection_rejected(self):
        session = _session()
        pid = _draft(session)
        executor = AsyncMock()
        result = await confirm_selection(session, pid, [], executor)
        assert result["ok"] is False
        assert result["reason"] == "invalid_selection"
        executor.assert_not_awaited()
        assert pid in pending_store(session)

    @pytest.mark.asyncio
    async def test_blank_strings_ignored_then_rejected(self):
        session = _session()
        pid = _draft(session)
        result = await confirm_selection(session, pid, ["", "  "], AsyncMock())
        assert result["ok"] is False
        assert result["reason"] == "invalid_selection"

    @pytest.mark.asyncio
    async def test_expired_draft_returns_not_found(self):
        session = _session()
        pid = _draft(session)
        _expire(session, pid)
        executor = AsyncMock()
        result = await confirm_selection(session, pid, ["light.a"], executor)
        assert result["ok"] is False
        assert result["reason"] == "not_found"
        executor.assert_not_awaited()
        # 懒过期：取的时候就该被清掉
        assert pid not in pending_store(session)

    @pytest.mark.asyncio
    async def test_unknown_pending_id_falls_back_to_unique_live_draft(self):
        """口头确认路径拿不到 id（会话历史不存 tool 消息），靠唯一草稿兜底。"""
        session = _session()
        pid = _draft(session)
        result = await confirm_selection(session, "", ["light.a"], AsyncMock(return_value={}))
        assert result["ok"] is True
        assert result["pending_id"] == pid

    @pytest.mark.asyncio
    async def test_executor_exception_keeps_draft(self):
        """执行失败不能把草稿摘掉——用户重试时无草稿可用就等于要重说一遍指令。"""
        session = _session()
        pid = _draft(session)
        executor = AsyncMock(side_effect=RuntimeError("HA 不可达"))
        result = await confirm_selection(session, pid, ["light.a"], executor)
        assert result["ok"] is False
        assert result["reason"] == "exec_failed"
        assert "HA 不可达" in result["error"]
        assert pid in pending_store(session)

    @pytest.mark.asyncio
    async def test_does_not_touch_other_kind(self):
        """同会话里并存待确认规则草稿时，不能误取到别的 kind。"""
        session = _session()
        pending_store(session)["rule-1"] = {
            "kind": KIND_AUTOMATION_RULE, "rule": {}, "created_at": time.time(),
        }
        result = await confirm_selection(session, "rule-1", ["light.a"], AsyncMock())
        assert result["ok"] is False
        assert result["reason"] == "not_found"
        assert "rule-1" in pending_store(session)


class TestCancelAndDrop:
    def test_cancel_removes_draft(self):
        session = _session()
        pid = _draft(session)
        assert cancel_selection(session, pid) is True
        assert pid not in pending_store(session)

    def test_cancel_twice_returns_false(self):
        session = _session()
        pid = _draft(session)
        cancel_selection(session, pid)
        assert cancel_selection(session, pid) is False

    def test_drop_clears_all_selection_drafts_only(self):
        """闸门干净解决一轮指令时清场，但不得动别的 kind 的草稿。"""
        session = _session()
        pid1 = _draft(session)
        pid2 = _draft(session, query="关窗帘")
        pending_store(session)["rule-1"] = {
            "kind": KIND_AUTOMATION_RULE, "rule": {}, "created_at": time.time(),
        }
        assert drop_selection_drafts(session) == 2
        assert pid1 not in pending_store(session)
        assert pid2 not in pending_store(session)
        assert "rule-1" in pending_store(session)

    def test_drop_on_empty_store(self):
        assert drop_selection_drafts(_session()) == 0
