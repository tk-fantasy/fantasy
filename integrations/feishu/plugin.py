"""飞书机器人插件 —— Phase 4。

飞书用户私聊/群聊 @机器人 → webhook（宿主侧）→ Dispatcher → 回复 → speak_to → 本插件发消息。

飞书发消息流程：
  1. 用 app_id + app_secret 换 tenant_access_token（缓存 + 自动刷新）
  2. POST 发消息 API 到指定 chat_id

chat_id 路由：webhook 调 speak_to 时把 chat_id 作为 msg_id 传入。
broadcast fan-out 误到飞书时 msg_id 是 request_id（非 oc_ 开头），skip。
"""

import asyncio
import json
import os
import sys
from typing import Any

import httpx

from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.sink_base import OutputSink

# 飞书 Open API 端点
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


class FeishuSink(OutputSink):
    """飞书发消息 sink。

    speak(text, msg_id) —— msg_id 在定向调用（speak_to）时是 chat_id（oc_ 开头），
    在 broadcast fan-out 时是 request_id（非 oc_ 开头，skip）。
    """

    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_cache: str | None = None
        self._token_expire: float = 0.0
        self._seq_lock = asyncio.Lock()  # 串行发消息（飞书 API 限频）

    async def speak(self, text: str, msg_id: str = "") -> dict:
        # msg_id = chat_id（定向调用时宿主传入）
        if not msg_id or not msg_id.startswith("oc_"):
            return {"ok": False, "skipped": True}  # broadcast fan-out 乱入，跳过
        if not text:
            return {"ok": False, "skipped": True}
        chat_id = msg_id
        try:
            token = await self._get_tenant_token()
            async with self._seq_lock:
                await self._send_message(token, chat_id, text)
            return {"ok": True, "chat_id": chat_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def interrupt(self) -> dict:
        # 飞书无 TTS 可打断，no-op
        return {"ok": True}

    async def _get_tenant_token(self) -> str:
        """获取 tenant_access_token（缓存 + 自动刷新）。"""
        loop = asyncio.get_event_loop()
        if self._token_cache and loop.time() < self._token_expire:
            return self._token_cache
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _TOKEN_URL,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            self._token_cache = data["tenant_access_token"]
            expire = data.get("expire", 7200)
            # 提前 60 秒过期，避免临界点
            self._token_expire = loop.time() + expire - 60
            return self._token_cache

    async def _send_message(self, token: str, chat_id: str, text: str) -> None:
        """POST 飞书发消息 API（发到指定 chat_id）。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _MESSAGE_URL,
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
            )
            resp.raise_for_status()


class FeishuPlugin(IntegrationPlugin):
    """飞书插件。setup 时读环境变量凭证。"""

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        self.manifest = manifest_dict
        app_id = os.environ.get("AETHER_FEISHU_APP_ID", "")
        app_secret = os.environ.get("AETHER_FEISHU_APP_SECRET", "")
        if app_id and app_secret:
            self.sinks = [FeishuSink(app_id, app_secret)]
        else:
            self.sinks = []  # 无凭证时不注册 sink


if __name__ == "__main__":
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(FeishuPlugin, _manifest_path))
