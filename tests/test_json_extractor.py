"""Tests for app.utils.json_extractor — pure functions, no mocking."""
from __future__ import annotations

import json

from app.utils.json_extractor import extract_json_from_content


class TestExtractJsonFromContent:
    def test_plain_json(self):
        raw = '{"tool_name": "test", "action": "respond"}'
        result = json.loads(extract_json_from_content(raw))
        assert result["tool_name"] == "test"

    def test_json_in_markdown_block(self):
        raw = '```json\n{"key": "value"}\n```'
        result = json.loads(extract_json_from_content(raw))
        assert result["key"] == "value"

    def test_json_in_bare_code_block(self):
        raw = '```\n{"key": "value"}\n```'
        result = json.loads(extract_json_from_content(raw))
        assert result["key"] == "value"

    def test_json_with_surrounding_text(self):
        raw = 'Here is the result:\n{"tool_name": "test"}\nDone.'
        result = json.loads(extract_json_from_content(raw))
        assert result["tool_name"] == "test"

    def test_nested_json(self):
        raw = '{"outer": {"inner": [1, 2, 3]}}'
        result = json.loads(extract_json_from_content(raw))
        assert result["outer"]["inner"] == [1, 2, 3]

    def test_no_json_returns_original(self):
        raw = "no json here at all"
        assert extract_json_from_content(raw) == raw

    def test_whitespace_stripped(self):
        raw = '  \n {"key": "value"} \n  '
        result = json.loads(extract_json_from_content(raw))
        assert result["key"] == "value"

    def test_json_with_escaped_quotes(self):
        raw = '{"msg": "he said \\"hello\\""}'
        result = json.loads(extract_json_from_content(raw))
        assert result["msg"] == 'he said "hello"'

    def test_multiple_json_blocks_returns_first_from_markdown(self):
        raw = '```json\n{"first": true}\n```\nand\n```json\n{"second": true}\n```'
        result = json.loads(extract_json_from_content(raw))
        assert result.get("first") is True
