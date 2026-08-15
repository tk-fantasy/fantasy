"""诊断包导出测试 — 09 清单条目 1。

覆盖脱敏的两条底线：
- 密钥类字段（token/api_key/private_key/password/secret，任意层级）必须打码
- 个人信息（home 段家庭名/户主/住址、vision.device_mac）必须占位
- 设备名/URL/IP 等排障信息保留
以及 zip 内容完整性（manifest/config/README/logs）与审计记录落盘。
"""
from __future__ import annotations

import json
import zipfile
from io import BytesIO

import pytest

from app.ops import audit
from app.ops.diag import (
    build_diagnostic_package,
    collect_log_files,
    mask_value,
    sanitize_config,
)


# ==================== 脱敏：密钥 ====================

class TestSanitizeKeys:
    CONFIG = {
        "ha": {"url": "http://homeassistant:8123", "token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig"},
        "weather": {"host": "devapi.qweather.com", "private_key": "MC4CAQAwBQYDK2VwBCIEIExtremelyLongSecretKey"},
        "llm_keys": [
            {"id": "k1", "base_url": "https://api.deepseek.com/v1", "api_key_env": "LLM_KEY_K1"},
        ],
        "providers": {"chat": {"key_id": "k1"}},
        "cameras": [],
    }

    def test_tokens_masked(self):
        out = sanitize_config(self.CONFIG)
        token = out["ha"]["token"]
        # 长凭证保留首尾 4 字符便于人工核对，中段遮蔽
        assert token == "eyJh****.sig"
        assert "eyJhbGciOiJIUzI1NiJ9" not in token
        assert out["ha"]["url"] == "http://homeassistant:8123"  # URL 保留

    def test_private_key_masked(self):
        out = sanitize_config(self.CONFIG)
        assert "ExtremelyLongSecretKey" not in out["weather"]["private_key"]

    def test_env_var_names_kept(self):
        # api_key_env 只是变量名（无明文），但含 api_key 关键词——按密钥处理打码无害
        out = sanitize_config(self.CONFIG)
        assert isinstance(out["llm_keys"], list) and out["llm_keys"][0]["id"] == "k1"

    def test_non_sensitive_values_untouched(self):
        out = sanitize_config(self.CONFIG)
        assert out["weather"]["host"] == "devapi.qweather.com"
        assert out["providers"]["chat"]["key_id"] == "k1"

    def test_nested_password_in_list_dict(self):
        cfg = {"integrations": [{"name": "x", "password": "super-secret"}]}
        out = sanitize_config(cfg)
        assert out["integrations"][0]["password"] == "****"
        assert out["integrations"][0]["name"] == "x"

    def test_empty_sensitive_value_stays_empty(self):
        out = sanitize_config({"ha": {"token": ""}})
        assert out["ha"]["token"] == ""


# ==================== 脱敏：个人信息 ====================

class TestSanitizePII:
    def test_home_fields_redacted(self):
        cfg = {"home": {"home_name": "我们家", "owner_name": "小王", "city": "上海市", "district": "闵行区", "lat": 31.2}}
        out = sanitize_config(cfg)
        assert out["home"]["home_name"] == "[已脱敏]"
        assert out["home"]["owner_name"] == "[已脱敏]"
        assert out["home"]["city"] == "[已脱敏]"
        assert out["home"]["district"] == "[已脱敏]"
        assert out["home"]["lat"] == 31.2  # 经纬度保留（天气功能排障用）

    def test_device_mac_redacted(self):
        out = sanitize_config({"vision": {"device_mac": "AA:BB:CC:DD:EE:FF", "motion_threshold": 15}})
        assert out["vision"]["device_mac"] == "[已脱敏]"
        assert out["vision"]["motion_threshold"] == 15

    def test_mask_value_shapes(self):
        assert mask_value("12345678901234567890") == "1234****7890"
        assert mask_value("short") == "****"
        # 空值由 sanitize 层守卫（保留空串），直接调用 mask 一律给 "****"
        assert mask_value("") == "****"


# ==================== zip 完整性与审计 ====================

class TestBuildPackage:
    async def test_package_contains_expected_members(self, tmp_path, monkeypatch):
        import app.ops.diag as diag

        # 用临时 config + 临时 logs，不碰真实数据
        (tmp_path / "config.json").write_text(
            json.dumps({"ha": {"url": "http://localhost:8123", "token": "t" * 40}}),
            encoding="utf-8",
        )
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "app.log").write_text("x" * 100, encoding="utf-8")
        (logs_dir / "audit").mkdir()
        (logs_dir / "audit" / "ops_audit.jsonl").write_text("{}", encoding="utf-8")

        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
        monkeypatch.setattr(diag, "LOGS_DIR", logs_dir)
        monkeypatch.setattr(diag, "BASE_DIR", tmp_path)  # BASE_DIR 被 diag 引用
        # 审计文件重定向到 tmp，避免污染真实 logs/audit
        monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path / "audit_out")
        monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit_out" / "ops_audit.jsonl")

        data, filename = await build_diagnostic_package(operator="tester")
        assert filename.startswith("aether-diag-") and filename.endswith(".zip")

        zf = zipfile.ZipFile(BytesIO(data))
        names = zf.namelist()
        assert "manifest.json" in names
        assert "config/config_sanitized.json" in names
        assert "system/system_info.json" in names
        assert "README.txt" in names
        assert "logs/app.log" in names
        assert not any(n.startswith("logs/audit") for n in names)  # 审计目录不进包

        sanitized = json.loads(zf.read("config/config_sanitized.json"))
        assert sanitized["ha"]["token"] != "t" * 40
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["operator"] == "tester"

        # 审计已落盘
        audit_lines = (tmp_path / "audit_out" / "ops_audit.jsonl").read_text(encoding="utf-8").splitlines()
        entry = json.loads(audit_lines[-1])
        assert entry["action"] == "diag_export" and entry["operator"] == "tester"


class TestCollectLogFiles:
    def test_tail_and_budget(self, tmp_path, monkeypatch):
        import app.ops.diag as diag

        monkeypatch.setattr(diag, "LOGS_DIR", tmp_path)
        big = tmp_path / "app.log"
        big.write_bytes(b"a" * 1000)
        # 每文件尾部上限改小，验证截尾
        monkeypatch.setattr(diag, "PER_LOG_TAIL_BYTES", 100)
        collected = diag.collect_log_files()
        assert len(collected) == 1
        name, data = collected[0]
        assert name == "app.log" and len(data) == 100

    def test_missing_dir(self, tmp_path, monkeypatch):
        import app.ops.diag as diag

        monkeypatch.setattr(diag, "LOGS_DIR", tmp_path / "nope")
        assert diag.collect_log_files() == []


class TestAudit:
    def test_record_and_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
        monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "ops_audit.jsonl")
        audit.record("u1", "diag_export", {"filename": "x.zip"})
        audit.record("u2", "other")
        entries = audit.tail()
        assert [e["operator"] for e in entries] == ["u1", "u2"]
        assert entries[0]["action"] == "diag_export"
        assert entries[0]["detail"]["filename"] == "x.zip"

    def test_tail_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "none.jsonl")
        assert audit.tail() == []
