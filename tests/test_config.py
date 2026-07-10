"""Tests for app.config pure functions: _parse_dotenv, _deep_merge, get_config."""
from __future__ import annotations

from app.core.config import _deep_merge, _parse_dotenv, get_config


class TestParseDotenv:
    def test_basic(self):
        text = "KEY=value\nOTHER=123"
        result = _parse_dotenv(text)
        assert result == {"KEY": "value", "OTHER": "123"}

    def test_quoted_values(self):
        text = 'KEY="hello world"\nOTHER=\'single\''
        result = _parse_dotenv(text)
        assert result["KEY"] == "hello world"
        assert result["OTHER"] == "single"

    def test_comments_ignored(self):
        text = "# comment\nKEY=value"
        result = _parse_dotenv(text)
        assert result == {"KEY": "value"}

    def test_empty_lines_ignored(self):
        text = "\n\nKEY=value\n\n"
        result = _parse_dotenv(text)
        assert result == {"KEY": "value"}

    def test_no_equals_ignored(self):
        text = "INVALID_LINE\nKEY=value"
        result = _parse_dotenv(text)
        assert result == {"KEY": "value"}

    def test_empty_string(self):
        assert _parse_dotenv("") == {}


class TestDeepMerge:
    def test_flat_merge(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"outer": {"a": 1, "b": 2}}
        override = {"outer": {"b": 3}}
        result = _deep_merge(base, override)
        assert result["outer"] == {"a": 1, "b": 3}

    def test_override_non_dict_with_dict(self):
        base = {"key": "string"}
        override = {"key": {"nested": True}}
        result = _deep_merge(base, override)
        assert result["key"] == {"nested": True}

    def test_empty_override(self):
        base = {"a": 1}
        assert _deep_merge(base, {}) == {"a": 1}

    def test_empty_base(self):
        override = {"a": 1}
        assert _deep_merge({}, override) == {"a": 1}


class TestGetConfig:
    def test_simple_path(self, _patch_config):
        assert get_config("llm.enabled") is True

    def test_nested_path(self, _patch_config):
        assert get_config("llm.chat_model") == "test-chat"

    def test_missing_path_returns_default(self, _patch_config):
        assert get_config("nonexistent.key") is None

    def test_custom_default(self, _patch_config):
        assert get_config("nonexistent", "fallback") == "fallback"
