"""Tests for ApiKeyManager with mocked config."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.services.api_key_manager import ApiKeyManager


class TestApiKeyManager:
    def test_empty_config_no_entries(self, _patch_config):
        _patch_config["llm_keys"] = []
        mgr = ApiKeyManager(role="chat")
        assert mgr.total_concurrency_sync == 0

    def test_loads_keys_from_config(self, _patch_config):
        _patch_config["llm_keys"] = [
            {
                "id": "test-key",
                "api_key_env": "TEST_API_KEY",
                "base_url": "http://localhost:8000",
                "model": "test-model",
                "type": "chat",
            },
        ]
        _patch_config["providers"] = {
            "chat": {"max_concurrency": 4},
        }
        with patch.dict(os.environ, {"TEST_API_KEY": "sk-test"}):
            mgr = ApiKeyManager(role="chat")
        assert mgr.total_concurrency_sync == 4

    def test_skips_keys_without_api_key(self, _patch_config):
        _patch_config["llm_keys"] = [
            {
                "id": "no-key",
                "api_key_env": "NONEXISTENT_KEY",
                "model": "model",
                "type": "chat",
            },
        ]
        _patch_config["providers"] = {
            "chat": {"max_concurrency": 1},
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NONEXISTENT_KEY", None)
            mgr = ApiKeyManager(role="chat")
        assert mgr.total_concurrency_sync == 0

    def test_skips_keys_with_wrong_type(self, _patch_config):
        _patch_config["llm_keys"] = [
            {
                "id": "vision-only",
                "api_key_env": "TEST_KEY",
                "model": "v-model",
                "type": "vision",
            },
        ]
        _patch_config["providers"] = {
            "chat": {"max_concurrency": 2},
        }
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            mgr = ApiKeyManager(role="chat")
        assert mgr.total_concurrency_sync == 0

    def test_loads_keys_with_direct_api_key(self, _patch_config):
        _patch_config["llm_keys"] = [
            {
                "id": "direct-key",
                "api_key": "sk-direct",
                "base_url": "http://localhost:8000",
                "model": "test-model",
                "type": "chat",
            },
        ]
        _patch_config["providers"] = {
            "chat": {"max_concurrency": 4},
        }
        mgr = ApiKeyManager(role="chat")
        assert mgr.total_concurrency_sync == 4
