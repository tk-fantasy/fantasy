"""Tests for VisionService with mocked client."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.vision_service import VisionService


class TestVisionService:
    @pytest.mark.asyncio
    async def test_evaluate_condition_returns_int(self):
        client = MagicMock()
        client.evaluate_condition = AsyncMock(return_value=1)
        svc = VisionService(client=client)
        result = await svc.evaluate_condition([[1, 2, 3]], "有人")
        assert result == 1

    @pytest.mark.asyncio
    async def test_evaluate_condition_zero(self):
        client = MagicMock()
        client.evaluate_condition = AsyncMock(return_value=0)
        svc = VisionService(client=client)
        result = await svc.evaluate_condition([[1, 2, 3]], "有猫")
        assert result == 0

    def test_model_property(self):
        client = MagicMock()
        client.model = "test-vision-model"
        svc = VisionService(client=client)
        assert svc.model == "test-vision-model"

    def test_enabled_property(self):
        client = MagicMock()
        client.enabled = True
        svc = VisionService(client=client)
        assert svc.enabled is True
