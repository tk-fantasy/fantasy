"""运维页后端单测：备份/恢复（app/ops/backup.py）+ 升级校验（app/ops/upgrade.py）。

backup：SQLite 一致性快照、包结构白名单（防穿越）、保留 3 份、恢复预检。
upgrade：manifest/sha256 校验、min_compatible 兼容判定、历史记录。
diagnose：容器内外端口目标映射（另见 test_diag_export.py 的脱敏部分）。
"""
from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from app.ops import backup as bk
from app.ops import upgrade as up
from app.ops import diagnose as dg


# ==================== 备份 ====================

@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """把 backup 模块的路径全部指到临时目录，隔离真实数据。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backups_dir = tmp_path / "backups"
    monkeypatch.setattr(bk, "DATA_DIR", data_dir)
    monkeypatch.setattr(bk, "BACKUP_DIR", backups_dir)
    monkeypatch.setattr(bk, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(bk, "ENV_PATH", tmp_path / ".env")
    # 审计重定向，避免污染真实 logs/audit
    from app.ops import audit
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit" / "ops_audit.jsonl")
    (tmp_path / "config.json").write_text('{"ha": {"url": "http://x"}}', encoding="utf-8")
    (tmp_path / ".env").write_text("LLM_KEY_X=sk-test", encoding="utf-8")
    return tmp_path


def _make_db(data_dir: Path):
    conn = sqlite3.connect(data_dir / "aether.db")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello-备份快照')")
    conn.commit()
    conn.close()
    (data_dir / "jwt_secret").write_text("secret-123", encoding="utf-8")


class TestCreateBackup:
    def test_creates_tar_gz_with_whitelisted_layout(self, isolated_env):
        _make_db(bk.DATA_DIR)
        result = bk.create_backup("tester")
        assert result["name"].startswith("aether-backup-") and result["name"].endswith(".tar.gz")

        with tarfile.open(bk.BACKUP_DIR / result["name"], "r:gz") as tf:
            names = tf.getnames()
        assert "config.json" in names
        assert ".env" in names
        assert "data/aether.db" in names
        assert "data/jwt_secret" in names
        # WAL/SHM 伴生文件不进包（已并入一致性快照）
        assert not any(n.endswith(("-wal", "-shm")) for n in names)

    def test_sqlite_snapshot_is_consistent_copy(self, isolated_env):
        _make_db(bk.DATA_DIR)
        result = bk.create_backup("tester")
        with tarfile.open(bk.BACKUP_DIR / result["name"], "r:gz") as tf:
            content = tf.extractfile("data/aether.db").read()
        out = isolated_env / "restored.db"
        out.write_bytes(content)
        conn = sqlite3.connect(out)
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "hello-备份快照"
        conn.close()

    def test_retention_keeps_only_3(self, isolated_env, monkeypatch):
        # 文件名带秒级时间戳，同一秒内多次创建会撞名 → 模拟 4 个历史文件
        bk.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            fake = bk.BACKUP_DIR / f"aether-backup-2026010{i}-000000.tar.gz"
            fake.write_bytes(b"x")
            # mtime 递增保证排序稳定
            import os
            os.utime(fake, (1700000000 + i, 1700000000 + i))
        _make_db(bk.DATA_DIR)
        bk.create_backup("tester")
        remaining = list(bk.BACKUP_DIR.iterdir())
        assert len(remaining) == 3


class TestBackupValidation:
    def test_rejects_traversal_entry(self, isolated_env, tmp_path):
        bk.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _make_db(bk.DATA_DIR)
        result = bk.create_backup("t")
        pack = bk.BACKUP_DIR / result["name"]
        # 构造一个带 ../ 的恶意包
        evil = bk.BACKUP_DIR / "aether-backup-20260101-000000.tar.gz"
        with tarfile.open(evil, "w:gz") as tf:
            info = tarfile.TarInfo("../../etc/passwd")
            data = b"evil"
            info.size = len(data)
            tf.addfile(info, __import__("io").BytesIO(data))
        with pytest.raises(ValueError, match="非法路径"):
            bk.validate_backup(evil.name)
        evil.unlink()

    def test_rejects_non_whitelisted_entry(self, isolated_env):
        bk.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        evil = bk.BACKUP_DIR / "aether-backup-20260101-000001.tar.gz"
        with tarfile.open(evil, "w:gz") as tf:
            info = tarfile.TarInfo("etc/shadow")
            data = b"x"
            info.size = len(data)
            tf.addfile(info, __import__("io").BytesIO(data))
        with pytest.raises(ValueError, match="非白名单"):
            bk.validate_backup(evil.name)

    def test_rejects_bad_name(self, isolated_env):
        with pytest.raises(ValueError):
            bk.validate_backup("../../config.json")
        with pytest.raises(ValueError):
            bk.validate_backup("aether-backup-evil.tar.gz")

    def test_delete_only_valid_names(self, isolated_env):
        bk.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        good = bk.BACKUP_DIR / "aether-backup-20260101-000002.tar.gz"
        good.write_bytes(b"x")
        assert bk.delete_backup(good.name) is True
        assert not good.exists()
        with pytest.raises(ValueError):
            bk.delete_backup("../outside.tar.gz")

    def test_list_and_validate_roundtrip(self, isolated_env):
        _make_db(bk.DATA_DIR)
        created = bk.create_backup("tester")
        listed = bk.list_backups()
        assert [b["name"] for b in listed] == [created["name"]]
        info = bk.validate_backup(created["name"])
        assert info["has_config"] and info["has_env"] and info["has_data"]


# ==================== 升级校验 ====================

def _make_pack(tmp_path: Path, version: str, min_compatible: str, corrupt: bool = False) -> Path:
    """构造升级包（manifest + 伪镜像 tar），返回包路径。"""
    import hashlib
    import io

    inner = tmp_path / "images"
    inner.mkdir(parents=True)
    image_tar = inner / "aether.tar"
    payload = b"fake-docker-image" * 100
    image_tar.write_bytes(payload)
    sha = hashlib.sha256(b"CORRUPTED" if corrupt else payload).hexdigest()
    pack = tmp_path / f"aether-update-{version}.tar.gz"
    manifest = {"version": version, "min_compatible": min_compatible,
                "images": [{"name": "aether-app", "file": "images/aether.tar", "sha256": sha}]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "upgrade.sh").write_bytes(b"#!/bin/sh")
    with tarfile.open(pack, "w:gz") as tf:
        tf.add(tmp_path / "manifest.json", arcname="manifest.json")
        tf.add(image_tar, arcname="images/aether.tar")
        tf.add(tmp_path / "upgrade.sh", arcname="upgrade.sh")
    return pack


class TestVerifyPack:
    def test_valid_pack_returns_manifest(self, tmp_path, monkeypatch):
        import app.core.version as ver
        monkeypatch.setattr(ver, "get_version", lambda: "1.0.0")
        pack = _make_pack(tmp_path, "1.1.0", "1.0.0")
        manifest = up.verify_pack(pack)
        assert manifest["version"] == "1.1.0"

    def test_sha_mismatch_rejected(self, tmp_path, monkeypatch):
        import app.core.version as ver
        monkeypatch.setattr(ver, "get_version", lambda: "1.0.0")
        pack = _make_pack(tmp_path, "1.1.0", "1.0.0", corrupt=True)
        with pytest.raises(ValueError, match="sha256"):
            up.verify_pack(pack)

    def test_incompatible_version_rejected(self, tmp_path, monkeypatch):
        import app.core.version as ver
        monkeypatch.setattr(ver, "get_version", lambda: "1.0.0")
        pack = _make_pack(tmp_path, "2.0.0", min_compatible="1.5.0")
        with pytest.raises(ValueError, match="最低兼容"):
            up.verify_pack(pack)

    def test_same_version_upgrade_allowed(self, tmp_path, monkeypatch):
        """同版本重装/降级到同版允许（重装场景）。"""
        import app.core.version as ver
        monkeypatch.setattr(ver, "get_version", lambda: "1.1.0")
        pack = _make_pack(tmp_path, "1.1.0", "1.0.0")
        assert up.verify_pack(pack)["version"] == "1.1.0"

    def test_missing_manifest_rejected(self, tmp_path):
        pack = tmp_path / "random.tar.gz"
        with tarfile.open(pack, "w:gz") as tf:
            info = tarfile.TarInfo("foo.txt")
            info.size = 3
            tf.addfile(info, __import__("io").BytesIO(b"bar"))
        with pytest.raises(ValueError, match="manifest"):
            up.verify_pack(pack)


class TestUpgradeHistory:
    def test_append_and_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(up, "HISTORY_FILE", tmp_path / "upgrade-history.jsonl")
        up._append_history({"from_version": "1.0.0", "to_version": "1.1.0", "operator": "t"})
        up._append_history({"from_version": "1.1.0", "to_version": "1.2.0", "operator": "t"})
        history = up.upgrade_history()
        assert len(history) == 2
        assert history[0]["to_version"] == "1.2.0"  # 新的在前


# ==================== 体检目标映射 ====================

class TestDiagnoseTargets:
    def test_four_service_targets_with_ports(self):
        assert [(label, port) for label, _, port in dg.SERVICE_TARGETS] == [
            ("aether 后端", 8010),
            ("Home Assistant", 8123),
            ("MQTT (mosquitto)", 1884),
            ("启动进度页", 8011),
        ]

    def test_host_vs_container_resolution(self):
        """容器内 HA/MQTT 走 compose 服务名（隔壁容器），宿主模式全走 127.0.0.1。"""
        hosts = {label: host for label, host, _ in dg.SERVICE_TARGETS}
        if dg.IN_CONTAINER:
            assert hosts["Home Assistant"] == "homeassistant"
            assert hosts["MQTT (mosquitto)"] == "mqtt"
            assert hosts["aether 后端"] == "127.0.0.1"
        else:
            assert all(h == "127.0.0.1" for h in hosts.values())
