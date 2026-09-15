"""config.py 空环境变量覆盖行为：空值视为未设置，不得冲掉用户保存的配置。

背景（全新安装死锁 F4）：`.env.example` 的 `HA_TOKEN=` 是"存在但为空"，
mosquitto 硬门槛要求走文档流程复制 .env 后该变量就在环境里；向导保存的
HA token 会被 `_load_env_override()` 的空值覆盖立即冲掉，`setup_complete`
永远无法达成，新用户永远出不了设置向导。
"""

from pathlib import Path

from app.core import config


def test_load_env_override_skips_empty_values(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://homeassistant:8123")
    monkeypatch.setenv("HA_TOKEN", "")  # 存在但为空（.env.example 初始状态）
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")

    override = config._load_env_override()

    assert override["ha"]["url"] == "http://homeassistant:8123"
    assert "token" not in override["ha"]
    assert "base_url" not in override["llm"]
    assert "chat_model" not in override["llm"]


def test_update_config_section_survives_empty_env_token(monkeypatch, tmp_path):
    """向导保存的 HA token 不被空 HA_TOKEN 环境变量冲掉。"""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG", {"llm_keys": [], "ha": {}})
    monkeypatch.setenv("HA_URL", "http://homeassistant:8123")
    monkeypatch.setenv("HA_TOKEN", "")

    config.update_config_section(
        "ha", {"url": "http://homeassistant:8123", "token": "wizard-saved"}
    )

    assert config.get_config("ha")["token"] == "wizard-saved"


def test_nonempty_env_token_still_overrides(monkeypatch, tmp_path):
    """非空 env 值保持原有覆盖语义（Docker 部署 HA_URL 指向服务名）。"""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG", {"llm_keys": [], "ha": {"url": "http://localhost:8123", "token": "old"}})
    monkeypatch.setenv("HA_URL", "http://homeassistant:8123")
    monkeypatch.setenv("HA_TOKEN", "real-token")

    config.update_config_section(
        "ha", {"url": "http://homeassistant:8123", "token": "wizard-saved"}
    )

    ha = config.get_config("ha")
    assert ha["url"] == "http://homeassistant:8123"
    assert ha["token"] == "real-token"
    assert config.CONFIG_PATH == Path(tmp_path / "config.json")
