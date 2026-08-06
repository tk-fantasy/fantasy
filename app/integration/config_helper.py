"""集成平台 config 读写辅助（隔离 config 依赖，便于测试 mock）。"""

from app.core.config import get_config, update_config_section


def get_broadcast_enabled() -> bool:
    """读取全局广播开关（默认 True）。"""
    return bool(get_config("integration.broadcast_enabled", True))


def set_broadcast_enabled(enabled: bool) -> None:
    """持久化全局广播开关到 config.json。"""
    update_config_section("integration", {"broadcast_enabled": bool(enabled)})
