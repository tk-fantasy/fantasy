"""Tests for vision.py ActionResult dataclass."""
from __future__ import annotations

from app.vision import ActionResult


class TestActionResult:
    def test_basic(self):
        r = ActionResult(action="idle", feedback="平静")
        assert r.action == "idle"
        assert r.details == {}

    def test_with_details(self):
        r = ActionResult(action="person", feedback="检测到人",
                         details={"count": 2})
        assert r.details["count"] == 2

    def test_default_details(self):
        r1 = ActionResult(action="a", feedback="f")
        r2 = ActionResult(action="b", feedback="f")
        r1.details["key"] = "val"
        assert "key" not in r2.details
