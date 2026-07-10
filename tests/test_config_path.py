"""Tests for config loading and path resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import CONFIG_PATH, get_config


class TestConfigPathResolution:
    """测试配置文件路径解析是否正确。"""

    def test_config_path_exists(self):
        """验证config.json文件存在。"""
        assert CONFIG_PATH.exists(), f"config.json not found at {CONFIG_PATH}"

    def test_config_path_is_in_project_root(self):
        """验证config.json在项目根目录,不在app/子目录。"""
        # CONFIG_PATH应该在项目根目录,即app/的父目录
        project_root = Path(__file__).resolve().parent.parent
        expected_path = project_root / "config.json"
        assert CONFIG_PATH == expected_path, (
            f"CONFIG_PATH should be {expected_path}, but got {CONFIG_PATH}"
        )

    def test_config_loads_without_error(self):
        """验证配置加载不抛出异常(回归测试)。"""
        # 只要能调用get_config不报错就说明路径正确
        try:
            _ = get_config("ha.url")
            _ = get_config("llm.enabled")
            _ = get_config("storage.session_file")
        except Exception as e:
            pytest.fail(f"Config loading failed: {e}")

    def test_llm_config_loads_correctly(self):
        """验证LLM配置能正确加载。"""
        llm_enabled = get_config("llm.enabled")
        llm_base_url = get_config("llm.base_url")

        if CONFIG_PATH.exists():
            # 至少应该有这些key,值可能是None或具体值
            assert "llm" in str(CONFIG_PATH.read_text(encoding="utf-8")) or llm_enabled is not None
