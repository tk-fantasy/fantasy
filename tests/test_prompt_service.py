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
