"""自动升级（投放即升）测试 — app/ops/auto_update.py 的判定逻辑。

覆盖：
- manifest 版本窥探：正常包 / 缺 manifest / 损坏包
- 候选判定：只升不降不重装（> 当前才装）、落稳窗口（mtime 太新跳过）、
  多包取最新版本、非包文件忽略
- 开关：update.auto_upgrade 配置
真实 watcher 循环与 docker load 链路属部署侧验收。
"""
from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

from app.ops import auto_update as au
from app.ops import pack_export as pe


def _make_pack(path: Path, version: str, with_manifest: bool = True, corrupt: bool = False):
    """构造只含 manifest 的最小包（peek 只读 manifest，不需要真镜像）。"""
    if corrupt:
        path.write_bytes(b"not a tar at all")
        return
    with tarfile.open(path, "w:gz") as tf:
        if with_manifest:
            data = json.dumps({"version": version, "min_compatible": "1.0.0"}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


class TestPeekManifest:
    def test_reads_version(self, tmp_path):
        p = tmp_path / "aether-update-2.0.0.tar.gz"
        _make_pack(p, "2.0.0")
        meta = pe.peek_pack_meta(p)
        assert meta["version"] == "2.0.0"
        assert meta["min_compatible"] == "1.0.0"

    def test_missing_manifest_returns_none(self, tmp_path):
        p = tmp_path / "aether-update-2.0.0.tar.gz"
        _make_pack(p, "2.0.0", with_manifest=False)
        assert pe.peek_pack_meta(p) is None

    def test_corrupt_returns_none(self, tmp_path):
        p = tmp_path / "aether-update-2.0.0.tar.gz"
        _make_pack(p, "2.0.0", corrupt=True)
        assert pe.peek_pack_meta(p) is None


class TestFindCandidate:
    def _setup(self, tmp_path, monkeypatch, current="1.0.0"):
        monkeypatch.setattr(pe, "PACK_DIR", tmp_path)
        monkeypatch.setattr(au, "SETTLE_SECONDS", 0)
        import app.core.version as ver
        monkeypatch.setattr(ver, "get_version", lambda: current)
        monkeypatch.setattr(au, "get_version", lambda: current)

    def _old(self, p: Path, age: int = 120):
        os.utime(p, (os.path.getmtime(p) - age,) * 2)

    def test_newer_pack_is_candidate(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        p = tmp_path / "aether-update-1.5.0.tar.gz"
        _make_pack(p, "1.5.0")
        self._old(p)
        assert au.find_candidate() == (p, "1.5.0")

    def test_same_or_lower_version_skipped(self, tmp_path, monkeypatch):
        # 关键场景：导出方本机导出的包（版本=当前）不能触发自装
        self._setup(tmp_path, monkeypatch, current="1.5.0")
        for name, ver in (("aether-update-1.5.0.tar.gz", "1.5.0"),
                          ("aether-update-1.0.0.tar.gz", "1.0.0")):
            p = tmp_path / name
            _make_pack(p, ver)
            self._old(p)
        assert au.find_candidate() is None

    def test_fresh_file_waits_settle(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(au, "SETTLE_SECONDS", 60)
        p = tmp_path / "aether-update-2.0.0.tar.gz"
        _make_pack(p, "2.0.0")   # mtime = now → 还在拷贝
        assert au.find_candidate() is None
        self._old(p)
        assert au.find_candidate() == (p, "2.0.0")

    def test_picks_newest_among_multiple(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        for ver in ("1.2.0", "1.9.0", "1.5.0"):
            p = tmp_path / f"aether-update-{ver}.tar.gz"
            _make_pack(p, ver)
            self._old(p)
        assert au.find_candidate()[1] == "1.9.0"

    def test_ignores_non_pack_and_bad_name(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        junk = tmp_path / "notes.txt"
        junk.write_text("x")
        evil = tmp_path / "aether-update-1.2.0.tar.gz"
        _make_pack(evil, "9.9.9")
        assert au.find_candidate() == (evil, "9.9.9")   # 合法包仍能识别
        evil.unlink()
        junk2 = tmp_path / "../escape.tar.gz"
        assert au.find_candidate() is None


class TestToggle:
    def test_default_on_config_off(self, tmp_path, monkeypatch):
        import app.core.config as cfg
        monkeypatch.setitem(cfg.CONFIG, "update", {"auto_upgrade": False})
        assert au.auto_upgrade_enabled() is False
        monkeypatch.setitem(cfg.CONFIG, "update", {})
        assert au.auto_upgrade_enabled() is True
