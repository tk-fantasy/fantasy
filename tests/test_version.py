"""版本号助手测试 — 09 清单条目 2。"""
from __future__ import annotations

import json

from app.core import version as ver


def test_reads_version_json(tmp_path, monkeypatch):
    (tmp_path / "version.json").write_text(
        json.dumps({"version": "1.2.3", "min_compatible": "1.0.0"}), encoding="utf-8"
    )
    monkeypatch.setattr(ver, "VERSION_PATH", tmp_path / "version.json")
    ver.get_version.cache_clear()
    assert ver.get_version() == "1.2.3"


def test_fallback_on_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ver, "VERSION_PATH", tmp_path / "nope.json")
    ver.get_version.cache_clear()
    assert ver.get_version() == ver.FALLBACK_VERSION


def test_fallback_on_broken_json(tmp_path, monkeypatch):
    (tmp_path / "version.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(ver, "VERSION_PATH", tmp_path / "version.json")
    ver.get_version.cache_clear()
    assert ver.get_version() == ver.FALLBACK_VERSION


def test_repo_version_json_is_valid():
    ver.get_version.cache_clear()
    # 仓库自带的 version.json 必须可解析且是语义化版本（x.y.z）
    v = ver.get_version()
    assert v != ver.FALLBACK_VERSION
    assert len(v.split(".")) == 3
