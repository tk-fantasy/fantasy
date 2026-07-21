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
    """把容器内 .env 注入 os.environ。

    优先级策略（处理 docker compose env_file 与容器内 .env 不同步的问题）：
    - 真实环境变量（docker compose 注入的，非空）优先，不覆盖
    - 容器内 .env 的值用于「补位」：当 os.environ 里某个 key 为空或不存在时，
      用容器内 .env 的值。这样 write_secrets 写的密码即使没被 docker compose
      注入，进程启动时也能从容器内 .env 读到。

    背景：docker compose 启动时读宿主机 .env 注入容器环境变量，但
    write_secrets 写的是容器内 /aether/.env。两者不同步时（如 rebuild 后
    宿主机 .env 没密码但容器内 .env 有），靠这里补位让密码不丢。
    """
    if not ENV_PATH.exists():
        return
    for key, value in _parse_dotenv(ENV_PATH.read_text(encoding="utf-8")).items():
        existing = os.environ.get(key, "")
        # 已存在且非空 → 保留 docker compose 注入的真实值
        # 不存在或为空 → 用容器内 .env 的值补位
        if not existing:
            os.environ[key] = value


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


def _safe_backup_config() -> None:
    """把当前 config.json 备份到 .json.bak（用 copy 而非 rename）。

    Docker bind-mount 下 os.rename 会报 "Device or resource busy"，
    改用读+写拷贝内容，避免移动 bind-mount 的目录条目。
    """
    if not CONFIG_PATH.exists():
        return
    backup_path = CONFIG_PATH.with_suffix('.json.bak')
    try:
        backup_path.write_text(
            CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
    except OSError:
        # 备份失败不阻塞写入，只记日志
        import logging
        logging.getLogger(__name__).warning("Failed to backup config.json", exc_info=True)


def update_config_section(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """运行时更新某个配置段:写入 config.json 并同步内存 CONFIG。

    注意：环境变量覆盖（HA_URL / HA_TOKEN / LLM_* 等）只在启动时跑一次
    （_load_env_override）。本函数把用户填的值合并进内存 CONFIG 后，会
    冲掉启动时的环境变量覆盖。为此调用后重新应用一次环境变量覆盖，保证
    内存 CONFIG 始终以环境变量为准（Docker 部署下 HA_URL 指向 compose
    服务名，用户在 UI 填的 localhost 不该覆盖它）。
    """
    global CONFIG
    values = dict(values)

    with _config_write_lock:
        file_config = _load_file_config()
        file_config[section] = _deep_merge(file_config.get(section, {}), values)
        # 写磁盘前清理 llm_keys 中的明文 api_key（旧代码遗留，密钥应只存 .env）
        for key_entry in file_config.get("llm_keys", []):
            key_entry.pop("api_key", None)
        # 备份当前配置（copy 而非 rename，兼容 Docker bind-mount）
        _safe_backup_config()
        atomic_write(
            CONFIG_PATH,
            json.dumps(file_config, ensure_ascii=False, indent=2) + "\n",
        )
        CONFIG = _deep_merge(CONFIG, {section: values})
        # 重新应用环境变量覆盖，防止用户填的值冲掉 HA_URL 等启动时覆盖
        CONFIG = _deep_merge(CONFIG, _load_env_override())
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


def save_global_llm_keys(keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """整体替换 config.json 的顶层数组 llm_keys（全局共享 key）。

    与 upsert_llm_key 不同：本函数把结果写回 config.json 磁盘，使其跨重启持久化；
    upsert_llm_key 只更新内存，依赖启动时从用户 DB 回填。

    约定：config.json 的 llm_keys 不存明文 api_key，只存 api_key_env（密钥进 .env）。
    因此写入前剥离每项的 api_key 字段。

    Args:
        keys: 完整的 llm_keys 数组（每项含 id/base_url/model/type/chat_path/embed_path/api_key_env）

    Returns:
        写入磁盘的 keys 数组（已剥离明文 api_key）
    """
    global CONFIG
    # 剥离明文 api_key（只保留 api_key_env），避免密钥落 config.json
    sanitized: list[dict[str, Any]] = []
    for k in keys:
        item = dict(k)
        item.pop("api_key", None)
        # 确保有 api_key_env：未提供则按 id 自动生成
        env_name = str(item.get("api_key_env", "")).strip()
        if not env_name and item.get("id"):
            env_name = f"LLM_KEY_{str(item['id']).upper().replace('-', '_')}"
            item["api_key_env"] = env_name
        sanitized.append(item)

    with _config_write_lock:
        file_config = _load_file_config()
        file_config["llm_keys"] = sanitized
        # 备份当前配置（copy 而非 rename，兼容 Docker bind-mount）
        _safe_backup_config()
        atomic_write(
            CONFIG_PATH,
            json.dumps(file_config, ensure_ascii=False, indent=2) + "\n",
        )
        # 同步内存（整体替换，不 merge）
        CONFIG["llm_keys"] = sanitized
    return sanitized


def get_secondary_password_hash() -> str:
    """读取 config.json 中 security.secondary_password_hash（全局 key 配置的二级密码）。

    未设置返回空串。"""
    return str(get_config("security.secondary_password_hash", "") or "")


def set_secondary_password_hash(password_hash: str) -> None:
    """写入二级密码哈希到 config.json 的 security section（持久化）。"""
    update_config_section("security", {"secondary_password_hash": password_hash})
