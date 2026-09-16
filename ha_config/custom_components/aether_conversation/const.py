"""Aether conversation agent 常量。"""

DOMAIN = "aether_conversation"

# Aether 侧连接配置（config flow 填写）
CONF_HOST = "host"    # Aether 基地址，如 http://aether:8000
CONF_TOKEN = "token"  # Aether 的 X-API-Token（APP_TOKEN 环境变量）

DEFAULT_TIMEOUT_SECONDS = 60
