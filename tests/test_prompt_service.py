"""Tests for prompt_service.build_system_prompt."""
from __future__ import annotations

import pytest

from app.services.prompt_service import build_system_prompt


class TestBuildSystemPrompt:
    @pytest.mark.asyncio
    async def test_contains_persona(self):
        prompt = await build_system_prompt()
        assert "Aether" in prompt

    @pytest.mark.asyncio
    async def test_contains_capabilities(self):
        prompt = await build_system_prompt()
        assert "控制" in prompt and "设备" in prompt

    @pytest.mark.asyncio
    async def test_contains_guidelines(self):
        prompt = await build_system_prompt()
        assert "设备名" in prompt

    @pytest.mark.asyncio
    async def test_contains_current_time(self):
        prompt = await build_system_prompt()
        assert "当前时间" in prompt

    @pytest.mark.asyncio
    async def test_with_device_catalog(self):
        prompt = await build_system_prompt(device_catalog="- light.bed 床头灯")
        assert "light.bed" in prompt

    @pytest.mark.asyncio
    async def test_with_visual_summary(self):
        prompt = await build_system_prompt(visual_summary={"action": "idle", "feedback": "平静"})
        assert "idle" in prompt

    @pytest.mark.asyncio
    async def test_without_optional_params(self):
        prompt = await build_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100


@pytest.mark.asyncio
async def test_system_prompt_includes_operable_constraint():
    """注入 device_catalog 时，system prompt 含白名单权限约束文案。"""
    from app.services.prompt_service import build_system_prompt
    prompt = await build_system_prompt(
        device_catalog="# 童锁\n- lock.tong_suo (类型:lock, 状态:locked) 名称:童锁 ⛔AI禁操作"
    )
    assert "⛔" in prompt
    assert "多候选" in prompt or "优先" in prompt
