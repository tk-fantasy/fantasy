"""Tests for app.services.priority_service — pure, no mocking."""
from __future__ import annotations

from app.services.priority_service import InteractivePriority


class TestInteractivePriority:
    def test_initial_state(self):
        p = InteractivePriority()
        assert p.active() is False

    def test_acquire_release(self):
        p = InteractivePriority()
        p.acquire()
        assert p.active() is True
        p.release()
        assert p.active() is False

    def test_multiple_acquire(self):
        p = InteractivePriority()
        p.acquire()
        p.acquire()
        assert p.active() is True
        p.release()
        assert p.active() is True
        p.release()
        assert p.active() is False

    def test_release_below_zero_clamps(self):
        p = InteractivePriority()
        p.release()
        p.release()
        assert p.active() is False

    def test_hold_context_manager(self):
        p = InteractivePriority()
        assert p.active() is False
        with p.hold():
            assert p.active() is True
        assert p.active() is False

    def test_hold_releases_on_exception(self):
        p = InteractivePriority()
        try:
            with p.hold():
                raise ValueError("boom")
        except ValueError:
            pass
        assert p.active() is False
