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
async def test_system_prompt_subname_guidance_no_forbid_marker():
    """注入 device_catalog 时含子功能名指引；禁止设备已改为渲染层隐藏，
    prompt 不再输出 ⛔ 权限文案。"""
    from app.services.prompt_service import build_system_prompt
    prompt = await build_system_prompt(
        device_catalog="# A灯\n- switch.a_on_p2 (类型:switch, 状态:off) 名称:A灯 会客厅灯 左键"
    )
    # 子功能名仅供匹配指称的指引存在
    assert "子功能名" in prompt
    # ⛔ 权限文案不再输出（禁止=隐藏，由渲染层保证）
    assert "⛔" not in prompt


@pytest.mark.asyncio
async def test_system_prompt_candidate_retry_guideline():
    """GUIDELINES：报错附候选时允许重试一次，无候选才停下。"""
    from app.services.prompt_service import GUIDELINES
    assert "候选" in GUIDELINES
    assert "重试一次" in GUIDELINES
