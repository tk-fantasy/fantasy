"""数据出网策略（egress_policy）单元测试 — 09 清单条目 4。

覆盖：
- 三档模式合法性：非法值 400，默认 cloud
- is_private_host：私网/回环/CGNAT/单标签主机名 = 内网；域名与公网 IP = 外网
- endpoint_report：按 providers.key_id 关联 llm_keys 的 base_url 归类
- warnings_for：local 模式有公网端点要警告；hybrid 只警告敏感角色
- 声明确认：记录 mode/version/操作人/时间 + hash 可复算
"""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import AppException
from app.services import egress_service as es


# ==================== 模式合法性 ====================

class TestMode:
    def test_default_is_cloud(self):
        with patch.object(es, "get_config", return_value=None):
            assert es.get_mode() == "cloud"

    def test_invalid_config_value_falls_back_to_cloud(self):
        with patch.object(es, "get_config", return_value="bogus"):
            assert es.get_mode() == "cloud"

    def test_set_mode_rejects_invalid(self):
        with pytest.raises(AppException) as ei:
            es.set_mode("bogus")
        assert ei.value.http_status == 400

    @pytest.mark.parametrize("mode", es.MODES)
    def test_set_mode_accepts_all_three(self, mode, tmp_path, monkeypatch):
        import app.core.config as cfg
        import json

        config_path = tmp_path / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)
        monkeypatch.setattr(cfg, "CONFIG", {})
        assert es.set_mode(mode) == mode
        assert json.loads(config_path.read_text(encoding="utf-8"))["egress_policy"]["mode"] == mode


# ==================== 内外网判定 ====================

class TestIsPrivateHost:
    @pytest.mark.parametrize("host", [
        "192.168.1.5", "10.0.0.1", "172.16.0.1", "127.0.0.1", "::1",
        "169.254.1.1", "100.101.1.1",  # Tailscale/CGNAT
        "localhost", "ollama", "homeassistant",  # 单标签主机名（Docker 服务名）
    ])
    def test_private(self, host):
        assert es.is_private_host(host) is True

    @pytest.mark.parametrize("host", [
        "api.openai.com", "api.deepseek.com", "8.8.8.8", "1.2.3.4",
    ])
    def test_public(self, host):
        assert es.is_private_host(host) is False

    def test_empty(self):
        assert es.is_private_host("") is False
        assert es.is_private_host(None) is False


# ==================== 端点报告与警告 ====================

def _config_with_keys(base_url_map: dict) -> dict:
    keys = [
        {"id": f"k-{role}", "base_url": url, "model": "m", "type": role,
         "api_key_env": f"LLM_KEY_K_{role.upper()}"}
        for role, url in base_url_map.items()
    ]
    providers = {role: {"key_id": f"k-{role}"} for role in base_url_map}
    return {"llm_keys": keys, "providers": providers}


class TestEndpointReport:
    def test_reports_all_five_roles(self):
        cfg = _config_with_keys({"chat": "https://api.openai.com/v1"})
        with patch.object(es, "get_config", side_effect=lambda p, d=None: cfg.get(p, d)):
            report = es.endpoint_report()
        roles = {r["role"] for r in report}
        assert roles == {"chat", "summary", "vision", "embed", "stt"}
        chat = next(r for r in report if r["role"] == "chat")
        assert chat["configured"] is True
        assert chat["private"] is False

    def test_unconfigured_role_is_not_private_flagged(self):
        cfg = _config_with_keys({})
        with patch.object(es, "get_config", side_effect=lambda p, d=None: cfg.get(p, d)):
            report = es.endpoint_report()
        chat = next(r for r in report if r["role"] == "chat")
        assert chat["configured"] is False
        assert chat["private"] is None

    def test_local_endpoint_detected(self):
        cfg = _config_with_keys({"chat": "http://ollama:11434/v1"})
        with patch.object(es, "get_config", side_effect=lambda p, d=None: cfg.get(p, d)):
            report = es.endpoint_report()
        assert next(r for r in report if r["role"] == "chat")["private"] is True


class TestWarnings:
    def _report(self, *pairs):
        return [{"role": r, "configured": bool(url), "private": None if not url else es.is_private_host(url)}
                for r, url in pairs]

    def test_local_mode_warns_public_chat(self):
        report = self._report(("chat", "https://api.openai.com/v1"))
        warnings = es.warnings_for(report, "local")
        assert len(warnings) == 1 and "chat" in warnings[0]

    def test_local_mode_silent_when_all_private(self):
        report = self._report(("chat", "http://ollama:11434/v1"), ("vision", ""))
        assert es.warnings_for(report, "local") == []

    def test_hybrid_warns_only_sensitive_roles(self):
        report = self._report(
            ("chat", "https://api.openai.com/v1"),
            ("vision", "https://api.openai.com/v1"),
        )
        warnings = es.warnings_for(report, "hybrid")
        assert len(warnings) == 1 and "vision" in warnings[0] and "chat" not in warnings[0]

    def test_cloud_mode_never_warns(self):
        report = self._report(("chat", "https://api.openai.com/v1"), ("vision", "https://x.com"))
        assert es.warnings_for(report, "cloud") == []


# ==================== 声明确认 ====================

class TestConfirmDeclaration:
    async def test_confirm_writes_hash_record(self):
        db = AsyncMock()
        db.kv_get = AsyncMock(return_value=None)
        with patch("app.core.database.Database") as db_cls, \
             patch.object(es, "MODES", es.MODES):
            db_cls.get.return_value = db
            record = await es.confirm_declaration("local", "owner")
        assert record["mode"] == "local"
        assert record["version"] == es.DECLARATION_VERSION
        assert record["confirmed_by"] == "owner"
        assert "T" in record["confirmed_at"]  # ISO 时间
        expected = hashlib.sha256(
            f"v{es.DECLARATION_VERSION}|local|owner|{record['confirmed_at']}".encode()
        ).hexdigest()
        assert record["hash"] == expected
        # 落库的是完整 JSON
        import json
        saved_key, saved_val = db.kv_set.await_args[0]
        assert saved_key == es.CONFIRM_KV_KEY
        assert json.loads(saved_val)["hash"] == expected

    async def test_confirm_rejects_invalid_mode(self):
        with pytest.raises(AppException):
            await es.confirm_declaration("bogus", "owner")

    async def test_get_confirm_record_none_when_unset(self):
        db = AsyncMock()
        db.kv_get = AsyncMock(return_value=None)
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = db
            assert await es.get_confirm_record() is None

    async def test_get_confirm_record_swallows_db_error(self):
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.side_effect = RuntimeError("db not ready")
            assert await es.get_confirm_record() is None
