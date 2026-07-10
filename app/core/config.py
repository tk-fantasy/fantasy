from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ..utils.file_utils import atomic_write

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"

WEEKDAY_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 配置写入锁：防止并发 update_config_section 互相覆盖
_config_write_lock = threading.Lock()


def _parse_dotenv(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _load_dotenv() -> None:
    """把 .env 注入 os.environ(不覆盖已存在的真实环境变量)。"""
    if not ENV_PATH.exists():
        return
    for key, value in _parse_dotenv(ENV_PATH.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)


def write_secrets(env_updates: dict[str, str]) -> None:
    """更新 .env 中的密钥(增量合并),同时同步进 os.environ 当前进程立即生效。"""
    existing = _parse_dotenv(ENV_PATH.read_text(encoding="utf-8")) if ENV_PATH.exists() else {}
    for key, value in env_updates.items():
        existing[key] = value
        os.environ[key] = value
    lines = ["# 敏感配置,本文件已被 .gitignore 忽略,请勿提交", ""]
    lines += [f"{key}={value}" for key, value in existing.items()]
    atomic_write(ENV_PATH, "\n".join(lines) + "\n")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_file_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        # JSON 格式错误时尝试从备份恢复
        backup_path = CONFIG_PATH.with_suffix(".json.bak")
        if backup_path.exists():
            try:
                return json.loads(backup_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        # 备份也不可用，返回空配置并记录错误
        import logging
        logging.getLogger(__name__).error(
            "config.json 格式错误，使用空配置启动: %s", e
        )
        return {}


def _load_env_override() -> dict[str, Any]:
    override: dict[str, Any] = {"llm": {}, "rag": {}, "storage": {}, "logging": {}, "ha": {}}
    if "LLM_ENABLED" in os.environ:
        override["llm"]["enabled"] = os.getenv("LLM_ENABLED", "0") == "1"
    if "LLM_BASE_URL" in os.environ:
        override["llm"]["base_url"] = os.getenv("LLM_BASE_URL")
    if "LLM_MODEL" in os.environ:
        override["llm"]["chat_model"] = os.getenv("LLM_MODEL")
        override["llm"]["vision_model"] = os.getenv("LLM_MODEL")
    if "LLM_EMBED_MODEL" in os.environ:
        override["llm"]["embed_model"] = os.getenv("LLM_EMBED_MODEL")
    if "LOG_LEVEL" in os.environ:
        override["logging"]["level"] = os.getenv("LOG_LEVEL")
    if "SESSION_FILE" in os.environ:
        override["storage"]["session_file"] = os.getenv("SESSION_FILE")
    # HA 连接：容器部署时用服务名（如 http://homeassistant:8123）覆盖 config.json 里的 localhost
    if "HA_URL" in os.environ:
        override["ha"]["url"] = os.getenv("HA_URL")
    if "HA_TOKEN" in os.environ:
        override["ha"]["token"] = os.getenv("HA_TOKEN")
    return override


_load_dotenv()
CONFIG = _deep_merge(_load_file_config(), _load_env_override())


def get_config(path: str, default: Any = None) -> Any:
    current: Any = CONFIG
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def update_config_section(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """运行时更新某个配置段:写入 config.json 并同步内存 CONFIG。"""
    global CONFIG
    values = dict(values)

    with _config_write_lock:
        file_config = _load_file_config()
        file_config[section] = _deep_merge(file_config.get(section, {}), values)
        # 写磁盘前清理 llm_keys 中的明文 api_key（旧代码遗留，密钥应只存 .env）
        for key_entry in file_config.get("llm_keys", []):
            key_entry.pop("api_key", None)
        # 备份当前配置（如果备份文件已存在则先删除）
        if CONFIG_PATH.exists():
            backup_path = CONFIG_PATH.with_suffix('.json.bak')
            if backup_path.exists():
                backup_path.unlink()
            CONFIG_PATH.rename(backup_path)
        atomic_write(
            CONFIG_PATH,
            json.dumps(file_config, ensure_ascii=False, indent=2) + "\n",
        )
        CONFIG = _deep_merge(CONFIG, {section: values})
    return file_config[section]


def update_memory_config(path: str, value: Any) -> None:
    """只更新内存 CONFIG，不写磁盘。

    用于多用户切换时临时覆盖配置（如 llm_keys、providers），
    重启或重新加载后会恢复为 config.json 中的值。

    Args:
        path: 点分路径，如 "llm_keys" 或 "providers.chat.key_id"
        value: 要设置的值
    """
    global CONFIG
    parts = path.split(".")
    if len(parts) == 1:
        CONFIG[parts[0]] = value
    else:
        # 逐级导航到父对象
        current = CONFIG
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value


def upsert_llm_key(entry: dict[str, Any], api_key_value: str | None = None) -> list[dict[str, Any]]:
    """新增或更新一个 llm_keys 条目（按 id 唯一）。

    密钥值(api_key_value)写 .env(env 名取 entry['api_key_env']),
    内存 CONFIG 更新，不写 config.json。返回更新后的 llm_keys 数组。
    """
    key_id = str(entry.get("id", "")).strip()
    if not key_id:
        raise ValueError("llm_key 必须有 id")
    env_name = str(entry.get("api_key_env", "")).strip()
    if not env_name:
        # 自动生成 env 名
        env_name = f"LLM_KEY_{key_id.upper().replace('-', '_')}"
        entry["api_key_env"] = env_name
    if api_key_value:
        write_secrets({env_name: str(api_key_value).strip()})

    # 只更新内存 CONFIG，不写 config.json
    keys = list(get_config("llm_keys", []) or [])
    # 按 id 替换或追加
    replaced = False
    for i, k in enumerate(keys):
        if k.get("id") == key_id:
            keys[i] = entry
            replaced = True
            break
    if not replaced:
        keys.append(entry)
    update_memory_config("llm_keys", keys)
    return keys


def delete_llm_key(key_id: str) -> list[dict[str, Any]]:
    """删除一个 llm_keys 条目（.env 里的密钥保留，不主动删，避免误伤）。"""
    keys = [k for k in (get_config("llm_keys", []) or []) if k.get("id") != key_id]
    update_memory_config("llm_keys", keys)
    return keys
