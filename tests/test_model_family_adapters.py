"""Tests for 模型家族适配器（model_family_adapters 插件动态加载）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.model_family_adapters import (
    ModelFamilyAdapter,
    get_adapter,
    refresh_plugin_adapters,
    reset_adapters,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_adapters()


def _make_plugin(root: Path, plugin_id: str, *, family: str = "qwen",
                 pattern: str = "qwen", disabled_skip: bool = False) -> None:
    """在临时目录造一个 model_adapter 插件。"""
    pdir = root / plugin_id
    pdir.mkdir(parents=True)
    manifest = {
        "id": plugin_id, "name": plugin_id, "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "model_adapter", "id": f"{plugin_id}_cap"}],
    }
    (pdir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    (pdir / "plugin.py").write_text("", encoding="utf-8")
    if not disabled_skip:  # 缺 adapters.py 的插件应被跳过
        (pdir / "adapters.py").write_text(
            "import re\n"
            "from app.agents.model_family_adapters import ModelFamilyAdapter\n\n"
            f"class Adapter(ModelFamilyAdapter):\n"
            f"    family = '{family}'\n"
            f"    _match_re = re.compile(r'{pattern}', re.IGNORECASE)\n\n"
            "    def no_think(self, s, u):\n"
            "        return s + '-sys', u + '-usr'\n\n"
            "ADAPTERS = [Adapter()]\n",
            encoding="utf-8")


class TestPluginLoading:
    def test_load_from_plugin_dir(self, tmp_path):
        _make_plugin(tmp_path, "qwen-adapter")
        n = refresh_plugin_adapters(plugin_dir=tmp_path, disabled=[])
        assert n == 1
        adapter = get_adapter("qwen3.8-27b-mlx")
        assert adapter is not None and adapter.family == "qwen"
        assert adapter.no_think("s", "u") == ("s-sys", "u-usr")

    def test_disabled_plugin_excluded(self, tmp_path):
        _make_plugin(tmp_path, "qwen-adapter")
        refresh_plugin_adapters(plugin_dir=tmp_path,
                                disabled=["qwen-adapter"])
        assert get_adapter("qwen3.8-27b-mlx") is None

    def test_missing_adapters_module_skipped(self, tmp_path):
        _make_plugin(tmp_path, "broken-plugin", disabled_skip=True)
        assert refresh_plugin_adapters(plugin_dir=tmp_path, disabled=[]) == 0

    def test_lazy_load_on_first_get(self, tmp_path, monkeypatch):
        _make_plugin(tmp_path, "qwen-adapter")
        monkeypatch.setattr(
            "app.agents.model_family_adapters._default_plugin_dir",
            lambda: tmp_path)
        assert get_adapter("Qwen3:8b") is not None

    def test_no_match_returns_none(self, tmp_path):
        _make_plugin(tmp_path, "qwen-adapter")
        refresh_plugin_adapters(plugin_dir=tmp_path, disabled=[])
        assert get_adapter("glm-4-flash") is None
        assert get_adapter("") is None

    def test_repo_qwen_adapter_registers(self):
        """仓库自带的 integrations/qwen-adapter 可被加载并命中。"""
        repo_integrations = Path(__file__).resolve().parent.parent / "integrations"
        n = refresh_plugin_adapters(plugin_dir=repo_integrations, disabled=[])
        assert n >= 1
        adapter = get_adapter("qwen3.8-27b-mlx")
        assert adapter is not None
        sys_text, user_text = adapter.no_think("你是助手。", "你好")
        assert sys_text == "你是助手。\n/no_think"
        assert user_text == "你好 /no_think"


class TestAdapterContract:
    def test_base_adapter_no_think_passthrough(self):
        """未覆写 no_think 的适配器原样返回（新家族零成本接入）。"""

        class Other(ModelFamilyAdapter):
            family = "other"

        adapter = Other()
        assert adapter.no_think("s", "u") == ("s", "u")

    def test_base_class_matches_false_without_regex(self):
        assert not ModelFamilyAdapter.matches("anything")
