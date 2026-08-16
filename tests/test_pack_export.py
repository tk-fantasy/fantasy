"""升级包分发测试 — /api/ops/update-pack/* 的服务层。

覆盖：
- 包名白名单正则：合法包名通过、路径穿越/畸形名拒绝
- scan_local_packs：目录扫描、mtime 排序、非包文件忽略
- 路由注册（五个端点挂上 /api）
docker save 真实导出链路属部署侧验收（见运维指南）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.exceptions import AppException
from app.ops import pack_export as pe


# ==================== 包名校验（防穿越） ====================

class TestPackName:
    def test_valid_names(self):
        for name in ("aether-update-1.0.0.tar.gz", "aether-update-1.2.3-beta.1.tar.gz",
                     "aether-update-2.0.0-rc_1.tar.gz"):
            assert pe.PACK_NAME_RE.match(name)

    def test_traversal_rejected(self):
        for name in ("../evil.tar.gz", "aether-update-../../etc.tar.gz",
                     "aether-update-1.0.0.zip", "aether-update-.tar.gz", "x.tar.gz"):
            assert not pe.PACK_NAME_RE.match(name)

    def test_local_pack_path_rejects_bad_name(self):
        with pytest.raises(AppException, match="非法"):
            pe.local_pack_path("../../etc/passwd.tar.gz")

    def test_local_pack_path_joins_safely(self):
        p = pe.local_pack_path("aether-update-1.0.0.tar.gz")
        assert p.name == "aether-update-1.0.0.tar.gz"
        assert p.parent == pe.PACK_DIR


# ==================== 本地包扫描 ====================

class TestScanLocalPacks:
    def test_scan_finds_only_packs_sorted_by_mtime(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pe, "PACK_DIR", tmp_path)
        import os
        for i, name in enumerate([
            "aether-update-1.0.0.tar.gz", "aether-update-1.1.0.tar.gz",
            "not-a-pack.txt", "aether-backup-20260101-000000.tar.gz",
        ]):
            f = tmp_path / name
            f.write_bytes(b"x")
            os.utime(f, (1700000000 + i, 1700000000 + i))
        packs = pe.scan_local_packs()
        assert [p["name"] for p in packs] == ["aether-update-1.1.0.tar.gz", "aether-update-1.0.0.tar.gz"]
        assert packs[0]["size_bytes"] == 1

    def test_scan_missing_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pe, "PACK_DIR", tmp_path / "nope")
        assert pe.scan_local_packs() == []


# ==================== 导出状态 ====================

class TestExportStatus:
    def test_idle_shape(self, monkeypatch):
        monkeypatch.setattr(pe, "_state", {"status": "idle", "staged_bytes": 0,
                                           "total_bytes": 0, "file": "", "error": ""})
        s = pe.export_status()
        assert s["status"] == "idle"
        assert "size_bytes" not in s

    def test_running_rejected_concurrent_start(self, monkeypatch):
        import asyncio
        monkeypatch.setattr(pe, "_state", {"status": "running", "staged_bytes": 0,
                                           "total_bytes": 0, "file": "", "error": ""})
        with pytest.raises(AppException) as ei:
            asyncio.run(pe.start_export("t"))
        assert ei.value.http_status == 409


# ==================== 路由注册 ====================

def test_pack_routes_registered():
    import app.main as m
    paths = {r.path for r in m.app.routes if hasattr(r, "path")}
    for p in ("/api/ops/update-pack/export",
              "/api/ops/update-pack/export/status",
              "/api/ops/update-pack/download",
              "/api/ops/update-pack/local",
              "/api/ops/update-pack/local/{name}/apply"):
        assert p in paths, p
