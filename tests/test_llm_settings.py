"""Tests for LlmSettingsService."""
from __future__ import annotations

import pytest

from app.services.llm_settings_service import LlmSettingsService, ROLE_KEYS
from app.core.exceptions import AppException


class TestLlmSettingsService:
    def test_current_settings(self):
        svc = LlmSettingsService()
        settings = svc.current_settings()
        assert isinstance(settings, dict)
        for role in ROLE_KEYS:
            assert role in settings
            # 每个角色应该有 key_id, max_concurrency, thinking, multimodal
            assert "key_id" in settings[role]
            assert "max_concurrency" in settings[role]

    def test_apply_invalid_role(self):
        svc = LlmSettingsService()
        with pytest.raises(AppException):
            svc.apply("invalid_role", "some-key-id")

    def test_apply_valid_role(self):
        svc = LlmSettingsService()
        result = svc.apply("chat", "test-key-id", max_concurrency=4)
        assert result["role"] == "chat"
        assert result["applied"]["key_id"] == "test-key-id"
        assert result["applied"]["max_concurrency"] == 4

    def test_warnings_returns_list(self):
        svc = LlmSettingsService()
        warnings = svc.warnings()
        assert isinstance(warnings, list)

    def test_register_reload_hook(self):
        svc = LlmSettingsService()
        called = []
        svc.register_reload_hook(lambda: called.append(1))
        svc._run_hooks()
        assert len(called) == 1
