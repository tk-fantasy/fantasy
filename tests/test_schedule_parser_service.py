"""Tests for app/services/schedule_parser_service.py — 自然语言时间解析。

parse_schedule 依赖 LLM 调用（网络），此处只测纯函数：
- _extract_json：从杂文本抠 JSON（直接解析 / 代码块 / 裸 JSON）
- _validate_schedule：三种 kind 的合法性校验与拒绝路径
- parse_schedule 的空输入守卫（不触发 LLM）
"""
from __future__ import annotations

import pytest

from app.services.schedule_parser_service import _extract_json, _validate_schedule, parse_schedule


class TestExtractJson:
    """_extract_json 三层策略。"""

    def test_plain_json(self):
        result = _extract_json('{"kind": "every", "every_seconds": 60}')
        assert result["kind"] == "every"
        assert result["every_seconds"] == 60

    def test_markdown_code_block(self):
        text = '```json\n{"kind": "at", "at": "2026-07-08T10:00:00"}\n```'
        result = _extract_json(text)
        assert result["kind"] == "at"
        assert result["at"] == "2026-07-08T10:00:00"

    def test_code_block_without_lang_tag(self):
        text = '```\n{"kind": "cron", "expr": "0 8 * * *"}\n```'
        result = _extract_json(text)
        assert result["kind"] == "cron"
        assert result["expr"] == "0 8 * * *"

    def test_json_with_surrounding_text(self):
        text = '好的，结果是 {"kind": "every", "every_seconds": 3600} 如上'
        result = _extract_json(text)
        assert result["kind"] == "every"
        assert result["every_seconds"] == 3600

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError):
            _extract_json("这里没有任何 JSON")

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError):
            _extract_json("{这不是合法 JSON}")


class TestValidateSchedule:
    """_validate_schedule 校验逻辑。"""

    # ---- at ----
    def test_at_valid(self):
        _validate_schedule({"kind": "at", "at": "2026-07-08T10:00:00"})  # 不抛

    def test_at_missing_field(self):
        with pytest.raises(ValueError, match="at 字段"):
            _validate_schedule({"kind": "at"})

    def test_at_invalid_iso(self):
        with pytest.raises(ValueError, match="合法 ISO"):
            _validate_schedule({"kind": "at", "at": "not-a-date"})

    def test_at_with_z_suffix_accepted(self):
        _validate_schedule({"kind": "at", "at": "2026-07-08T10:00:00Z"})  # 宽松去 Z

    # ---- every ----
    def test_every_valid(self):
        _validate_schedule({"kind": "every", "every_seconds": 1800})

    def test_every_zero_rejected(self):
        with pytest.raises(ValueError, match="正数"):
            _validate_schedule({"kind": "every", "every_seconds": 0})

    def test_every_negative_rejected(self):
        with pytest.raises(ValueError, match="正数"):
            _validate_schedule({"kind": "every", "every_seconds": -10})

    def test_every_missing_field(self):
        with pytest.raises(ValueError, match="正数"):
            _validate_schedule({"kind": "every"})

    # ---- cron ----
    def test_cron_valid(self):
        _validate_schedule({"kind": "cron", "expr": "0 8 * * *"})

    def test_cron_valid_workday(self):
        _validate_schedule({"kind": "cron", "expr": "30 17 * * 1-5"})

    def test_cron_missing_expr(self):
        with pytest.raises(ValueError, match="expr"):
            _validate_schedule({"kind": "cron"})

    def test_cron_invalid_expr(self):
        with pytest.raises(ValueError, match="不合法"):
            _validate_schedule({"kind": "cron", "expr": "not cron"})

    # ---- 未知 kind ----
    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="未知触发类型"):
            _validate_schedule({"kind": "weekly", "when": "monday"})


class TestParseScheduleGuard:
    """parse_schedule 守卫（不触发 LLM 调用）。"""

    @pytest.mark.asyncio
    async def test_empty_phrase_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            await parse_schedule("")

    @pytest.mark.asyncio
    async def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            await parse_schedule("   ")

    @pytest.mark.asyncio
    async def test_none_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            await parse_schedule(None)
