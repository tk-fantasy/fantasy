"""manifest schema + 加载器测试。"""

import json

from app.integration.manifest_loader import load_manifests
from app.integration.schema import Manifest


def test_load_valid_manifest(tmp_path):
    plugin_dir = tmp_path / "echo"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "echo",
        "name": "回声测试",
        "version": "1.0.0",
        "aether_api_version": "1",
        "entry": "plugin.py",
        "capabilities": [{
            "type": "output_sink",
            "id": "echo_main",
            "priority": 100,
            "config_schema": {},
        }],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert len(manifests) == 1
    assert manifests[0].id == "echo"
    assert manifests[0].capabilities[0].type.value == "output_sink"
    assert manifests[0].capabilities[0].priority == 100


def test_skip_manifest_with_wrong_api_version(tmp_path):
    plugin_dir = tmp_path / "old"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "old",
        "name": "旧插件",
        "version": "0.1.0",
        "aether_api_version": "0",
        "entry": "plugin.py",
        "capabilities": [],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert manifests == []


def test_skip_dir_without_manifest(tmp_path):
    (tmp_path / "empty").mkdir()

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert manifests == []


def test_skip_invalid_json_manifest(tmp_path):
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text("{not valid json", encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert manifests == []


def test_nonexistent_plugin_dir_returns_empty():
    assert load_manifests("/no/such/dir/xyz", api_version="1") == []


def test_capability_priority_defaults_to_zero(tmp_path):
    plugin_dir = tmp_path / "p"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "p", "name": "P", "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "output_sink", "id": "p1"}],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert manifests[0].capabilities[0].priority == 0


def test_manifest_has_capability_helper(tmp_path):
    """has_capability 辅助方法。"""
    plugin_dir = tmp_path / "mixed"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "mixed", "name": "M", "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [
            {"type": "output_sink", "id": "m1"},
            {"type": "inbound_router", "id": "m2"},
        ],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")
    from app.integration.schema import CapabilityType
    assert manifests[0].has_capability(CapabilityType.OUTPUT_SINK) is True
    assert manifests[0].has_capability(CapabilityType.INBOUND_ROUTER) is True


def test_manifest_secrets_field_parsed(tmp_path):
    """secrets 声明字段应被解析（用于解耦凭证注入）。"""
    plugin_dir = tmp_path / "sec"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "sec", "name": "S", "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "output_sink", "id": "s1"}],
        "secrets": ["ha_url", "ha_token"],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")
    assert manifests[0].secrets == ["ha_url", "ha_token"]


def test_manifest_secrets_defaults_to_empty(tmp_path):
    """未声明 secrets 时默认空列表。"""
    plugin_dir = tmp_path / "nosec"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "nosec", "name": "N", "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "output_sink", "id": "n1"}],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")
    assert manifests[0].secrets == []


def test_manifest_ui_contributions_parsed(tmp_path):
    """ui_contributions 字段应被解析（用于前端 UI 贡献）。"""
    plugin_dir = tmp_path / "ui"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "ui", "name": "U", "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "output_sink", "id": "u1"}],
        "ui_contributions": [{
            "slot": "chat_input_toolbar",
            "type": "toggle_button",
            "props": {"icon_on": "🔊", "icon_off": "🔇"},
            "state_key": "broadcast_enabled",
            "action": "toggle_broadcast"
        }],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")
    assert len(manifests[0].ui_contributions) == 1
    ui = manifests[0].ui_contributions[0]
    assert ui.slot == "chat_input_toolbar"
    assert ui.type == "toggle_button"
    assert ui.state_key == "broadcast_enabled"
    assert ui.action == "toggle_broadcast"


def test_manifest_ui_contributions_default_empty(tmp_path):
    """未声明 ui_contributions 时默认空列表（没插件 UI = 前端无该元素）。"""
    plugin_dir = tmp_path / "noui"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "noui", "name": "NU", "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "output_sink", "id": "nu1"}],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")
    assert manifests[0].ui_contributions == []
