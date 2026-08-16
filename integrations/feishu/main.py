"""飞书集成入口 —— 宿主侧通用加载机制调用。

宿主扫描 integrations/ 目录，发现本目录有 main.py 且定义了 start/stop 函数就加载。
删目录 → 宿主找不到 → 跳过 → 零影响。

飞书用 WebSocket 长连接（不需要公网 URL），在后台线程跑 lark-oapi 的 ws.Client。
事件回调通过 run_coroutine_threadsafe 调宿主的 async dispatch_fn。

凭证来源优先级：插件管理页保存的配置（config.json integration.host_configs.feishu）
> 环境变量 FEISHU_APP_ID 等（.env，老部署兼容）。管理页改配置后宿主会调
stop()+start() 热重连，无需重启容器。
"""

import logging
import os

from .ws_client import FeishuBot

logger = logging.getLogger(__name__)

_bot: FeishuBot | None = None

# 管理页字段名 → 环境变量名（未在界面配置时的回退来源）
_ENV_FALLBACK = {
    "app_id": "FEISHU_APP_ID",
    "app_secret": "FEISHU_APP_SECRET",
    "verification_token": "FEISHU_VERIFICATION_TOKEN",
    "encrypt_key": "FEISHU_ENCRYPT_KEY",
}


def _read_config() -> tuple[dict, str]:
    """合并界面配置与环境变量，返回 (配置, 来源标记)。

    界面配置逐字段优先：界面只填了 app_id 时，secret 等其余字段仍回退 env。
    """
    ui: dict = {}
    try:
        from app.integration.config_helper import get_host_config
        ui = get_host_config("feishu") or {}
    except Exception:
        ui = {}

    cfg = {}
    for key, env_name in _ENV_FALLBACK.items():
        value = str(ui.get(key, "") or "").strip() or os.environ.get(env_name, "")
        cfg[key] = value
    source = "ui" if any(str(ui.get(k, "") or "").strip() for k in _ENV_FALLBACK) else "env"
    return cfg, source


def start(dispatch_fn, loop):
    """启动飞书长连接。

    Args:
        dispatch_fn: async (query, session_id, user_id) -> str，宿主传入的 LLM 处理函数
        loop: 主线程 asyncio event loop
    Returns:
        FeishuBot 实例（成功）或 None（无凭证/失败）
    """
    global _bot

    cfg, source = _read_config()
    if not cfg["app_id"] or not cfg["app_secret"]:
        logger.info("飞书凭证未配置（管理页与 .env 均为空），跳过长连接启动")
        return None

    try:
        _bot = FeishuBot(
            app_id=cfg["app_id"],
            app_secret=cfg["app_secret"],
            verification_token=cfg.get("verification_token", ""),
            encrypt_key=cfg.get("encrypt_key", ""),
        )
        _bot.start(dispatch_fn, loop)
        logger.info("飞书 WebSocket 长连接已启动（凭证来源: %s）: %s", source, cfg["app_id"][:10] + "...")
        return _bot
    except Exception:
        logger.exception("飞书长连接启动失败（non-fatal）")
        return None


def stop():
    """停止飞书长连接。"""
    global _bot
    if _bot:
        _bot.stop()
        _bot = None
