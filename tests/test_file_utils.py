"""Tests for file_utils.atomic_write using tmp_path."""
from __future__ import annotations

from pathlib import Path

from app.utils.file_utils import atomic_write


class TestAtomicWrite:
    def test_creates_file(self, tmp_path: Path):
        target = tmp_path / "test.txt"
        atomic_write(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_overwrites_existing(self, tmp_path: Path):
        target = tmp_path / "test.txt"
        target.write_text("old", encoding="utf-8")
        atomic_write(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_no_tmp_leftover(self, tmp_path: Path):
        target = tmp_path / "test.txt"
        atomic_write(target, "data")
        tmp_file = target.with_suffix(target.suffix + ".tmp")
        assert not tmp_file.exists()

    def test_unicode_content(self, tmp_path: Path):
        target = tmp_path / "test.txt"
        atomic_write(target, "中文内容 🎉")
        assert target.read_text(encoding="utf-8") == "中文内容 🎉"
