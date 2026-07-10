"""Tests for app.services.motion_service — pure computation with numpy."""
from __future__ import annotations

import numpy as np

from app.services.motion_service import MotionDetector, compute_dhash, hamming_distance


class TestHammingDistance:
    def test_identical(self):
        a = np.array([True, False, True, False])
        assert hamming_distance(a, a) == 0

    def test_all_different(self):
        a = np.array([True, True, True])
        b = np.array([False, False, False])
        assert hamming_distance(a, b) == 3

    def test_partial(self):
        a = np.array([True, False, True, False])
        b = np.array([True, True, False, False])
        assert hamming_distance(a, b) == 2


class TestComputeDhash:
    def test_returns_bool_array(self):
        frame = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        h = compute_dhash(frame, hash_size=8)
        assert h.shape == (8, 8)
        assert h.dtype == bool

    def test_default_hash_size(self):
        frame = np.random.randint(0, 255, (200, 200, 3), dtype=np.uint8)
        h = compute_dhash(frame)
        assert h.shape == (16, 16)


class TestMotionDetector:
    def test_first_assess_returns_true(self):
        det = MotionDetector(hash_size=8, threshold=5)
        frame = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        motion, distance = det.assess(frame)
        assert motion is True
        assert distance == -1

    def test_same_frame_no_motion(self):
        det = MotionDetector(hash_size=8, threshold=5)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        det.assess(frame)
        det.commit_reference()
        motion, distance = det.assess(frame)
        assert motion is False
        assert distance == 0

    def test_different_frame_motion(self):
        det = MotionDetector(hash_size=8, threshold=2)
        # Use frames with spatial variation (gradient vs reverse gradient)
        # because uniform frames (all-0 vs all-255) produce identical dHashes
        ramp = np.linspace(0, 255, 100).astype(np.uint8)
        frame1 = np.stack([np.tile(ramp, (100, 1))] * 3, axis=-1)
        frame2 = np.stack([np.tile(ramp[::-1], (100, 1))] * 3, axis=-1)
        det.assess(frame1)
        det.commit_reference()
        motion, distance = det.assess(frame2)
        assert motion is True
        assert distance > 0

    def test_threshold_property(self):
        det = MotionDetector(threshold=10)
        assert det.threshold == 10

    def test_commit_reference_without_assess(self):
        det = MotionDetector()
        det.commit_reference()  # should not raise
