"""Tests for app/sg/sg_config.py — 构建参数从 config.json 读取，零硬编码。"""
from __future__ import annotations

import pytest

from app.sg.sg_config import SgConfig


class TestSgConfigFromConfig:
    def test_defaults_when_empty(self, monkeypatch):
        """无 llm_keys、无 sg 节点时返回全默认值。"""
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {"llm_keys": [], "sg": {}})

        c = SgConfig.from_config()
        assert c.embed_key == {}
        assert c.chat_key == {}
        assert c.ready is False
        assert c.threshold == 0.7
        assert c.pca_dim == 32
        assert c.max_workers == 8

    def test_ready_when_both_keys_present(self, monkeypatch):
        """embed + chat key 均配置时 ready=True。"""
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [
                {"type": "embed", "model": "BAAI/bge-m3", "base_url": "http://x"},
                {"type": "chat", "model": "glm-4-flash", "base_url": "http://y"},
            ],
            "sg": {},
        })

        c = SgConfig.from_config()
        assert c.ready is True
        assert c.embed_key["model"] == "BAAI/bge-m3"
        assert c.chat_key["model"] == "glm-4-flash"

    def test_not_ready_with_only_embed(self, monkeypatch):
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [{"type": "embed", "model": "bge-m3"}],
            "sg": {},
        })
        assert SgConfig.from_config().ready is False

    def test_not_ready_with_only_chat(self, monkeypatch):
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [{"type": "chat", "model": "glm-4-flash"}],
            "sg": {},
        })
        assert SgConfig.from_config().ready is False

    def test_sg_node_overrides_defaults(self, monkeypatch):
        """sg 节点的超参应覆盖默认值。"""
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [
                {"type": "embed"}, {"type": "chat"},
            ],
            "sg": {
                "threshold": 0.85,
                "pca_dim": 64,
                "umap_n_components": 3,
                "max_workers": 16,
            },
        })

        c = SgConfig.from_config()
        assert c.threshold == 0.85
        assert c.pca_dim == 64
        assert c.max_workers == 16

    def test_ignores_non_embed_chat_keys(self, monkeypatch):
        """type 不是 embed/chat 的 key 不被选中。"""
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [{"type": "vision", "model": "vision-model"}],
            "sg": {},
        })
        c = SgConfig.from_config()
        assert c.embed_key == {}
        assert c.chat_key == {}
        assert c.ready is False
