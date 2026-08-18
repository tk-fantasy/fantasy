"""发版辅助脚本测试 — scripts/new-version.py 的 apply_bump。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# scripts/ 不是包且文件名带连字符，按路径加载
_spec = importlib.util.spec_from_file_location(
    "new_version", Path(__file__).resolve().parent.parent / "scripts" / "new-version.py")
new_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(new_version)
apply_bump = new_version.apply_bump
current_commit = new_version.current_commit
git_log_since = new_version.git_log_since


class TestGitAutoNotes:
    """变更说明自动取 git 提交记录（本仓库本身就是 git 仓库）。"""

    def test_current_commit_is_hex(self):
        c = current_commit()
        assert c and all(ch in "0123456789abcdef" for ch in c)

    def test_log_since_real_commit(self):
        c = current_commit()
        log = git_log_since(c)
        assert isinstance(log, str)   # 区间空也可能回退到最近 20 条

    def test_log_since_bad_commit_falls_back(self):
        log = git_log_since("deadbeef99")
        assert isinstance(log, str) and len(log) >= 0   # 不抛错,静默回退


BASE = {"version": "1.0.0", "min_compatible": "1.0.0", "notes": "初始版本化基线。"}


class TestApplyBump:
    def test_bump_updates_version_and_notes(self):
        out = apply_bump(BASE, "1.1.0", None, "新增升级包分发")
        assert out["version"] == "1.1.0"
        assert out["notes"] == "新增升级包分发"
        assert out["min_compatible"] == "1.0.0"   # 不传则保留

    def test_min_compatible_override(self):
        out = apply_bump(BASE, "2.0.0", "1.5.0", "破坏性改动")
        assert out["min_compatible"] == "1.5.0"

    def test_empty_notes_keeps_old(self):
        out = apply_bump(BASE, "1.0.1", None, "")
        assert out["notes"] == BASE["notes"]

    def test_rejects_bad_version_format(self):
        for bad in ("1.1", "v1.1.0", "1.1.0.0", "abc", "1.1.0/../x"):
            with pytest.raises(ValueError):
                apply_bump(BASE, bad, None, "")

    def test_prerelease_suffix_allowed(self):
        out = apply_bump(BASE, "1.2.0-rc.1", None, "")
        assert out["version"] == "1.2.0-rc.1"
