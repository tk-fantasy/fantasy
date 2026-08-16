"""集成平台 config 读写辅助（隔离 config 依赖，便于测试 mock）。"""

from app.core.config import get_config, update_config_section


def get_broadcast_enabled() -> bool:
    """读取全局广播开关（默认 True）。"""
    return bool(get_config("integration.broadcast_enabled", True))


def set_broadcast_enabled(enabled: bool) -> None:
    """持久化全局广播开关到 config.json。"""
    update_config_section("integration", {"broadcast_enabled": bool(enabled)})


def get_disabled_plugins() -> list[str]:
    """读取被禁用的插件 id 列表（禁用的不加载、不启动进程）。"""
    return list(get_config("integration.disabled_plugins", []) or [])


def set_plugin_disabled(plugin_id: str, disabled: bool) -> list[str]:
    """设置某插件禁用/启用，持久化到 config，返回当前禁用列表。"""
    current = get_disabled_plugins()
    if disabled:
        if plugin_id not in current:
            current.append(plugin_id)
    else:
        current = [p for p in current if p != plugin_id]
    update_config_section("integration", {"disabled_plugins": current})
    return current


def get_current_mode() -> str:
    """读取当前聊天模式（默认 "aether"）。"""
    return str(get_config("integration.current_mode", "aether"))


def set_current_mode(mode: str) -> None:
    """持久化当前聊天模式到 config.json。"""
    update_config_section("integration", {"current_mode": str(mode)})


def get_host_config(plugin_id: str) -> dict:
    """读取插件在管理页保存的配置（integration.host_configs.<id>）。

    空配置返回 {}（插件回退到 manifest 默认值 / 环境变量）。
    """
    cfg = get_config(f"integration.host_configs.{plugin_id}", {}) or {}
    return dict(cfg) if isinstance(cfg, dict) else {}


def set_host_config(plugin_id: str, values: dict) -> None:
    """持久化插件配置到 config.json。

    update_config_section 是深合并：只覆盖该插件这一个 key，
    不影响 host_configs 下其他插件的配置。
    """
    update_config_section("integration", {"host_configs": {plugin_id: dict(values)}})


def merge_plugin_config(plugin_id: str, updates: dict, secret_keys: set[str]) -> dict:
    """保存管理页提交的配置并返回合并后的完整值。

    密钥类字段留空 = 保持原值（前端密码框不回显，提交空串不应清空密钥）；
    显式提交非空值则覆盖。
    """
    current = get_host_config(plugin_id)
    for k, v in (updates or {}).items():
        if k in secret_keys and not str(v).strip():
            continue
        current[k] = v
    set_host_config(plugin_id, current)
    return current
