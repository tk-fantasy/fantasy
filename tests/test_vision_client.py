"""Tests for LlmChatClient and LlmVisionClient pure functions."""
from __future__ import annotations

import numpy as np

from app.clients.llm_vision_client import downscale_for_vision


class TestDownscaleForVision:
    def test_no_downscale_when_small(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        result = downscale_for_vision(frame, max_side=200)
        assert result.shape == (100, 100, 3)

    def test_downscale_large_frame(self):
        frame = np.zeros((1000, 800, 3), dtype=np.uint8)
        result = downscale_for_vision(frame, max_side=400)
        assert result.shape[0] <= 400 or result.shape[1] <= 400

    def test_zero_max_side_returns_original(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        result = downscale_for_vision(frame, max_side=0)
        assert result.shape == frame.shape

    def test_preserves_aspect_ratio(self):
        frame = np.zeros((600, 300, 3), dtype=np.uint8)
        result = downscale_for_vision(frame, max_side=300)
        ratio_orig = 600 / 300
        ratio_result = result.shape[0] / result.shape[1]
        assert abs(ratio_orig - ratio_result) < 0.1
