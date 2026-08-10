"""飞书集成入口 —— 宿主侧通用加载机制调用。

宿主扫描 integrations/ 目录，发现本目录有 main.py 且定义了 start/stop 函数就加载。
删目录 → 宿主找不到 → 跳过 → 零影响。

飞书用 WebSocket 长连接（不需要公网 URL），在后台线程跑 lark-oapi 的 ws.Client。
事件回调通过 run_coroutine_threadsafe 调宿主的 async dispatch_fn。
"""

import os
import logging

from .ws_client import FeishuBot

logger = logging.getLogger(__name__)

_bot: FeishuBot | None = None


def start(dispatch_fn, loop):
    """启动飞书长连接。

    Args:
        dispatch_fn: async (query, session_id, user_id) -> str，宿主传入的 LLM 处理函数
        loop: 主线程 asyncio event loop
    Returns:
        FeishuBot 实例（成功）或 None（无凭证/失败）
    """
    global _bot

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.info("飞书凭证未配置，跳过长连接启动")
        return None

    try:
        _bot = FeishuBot(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
            encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", ""),
        )
        _bot.start(dispatch_fn, loop)
        logger.info("飞书 WebSocket 长连接已启动: %s", app_id[:10] + "...")
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
