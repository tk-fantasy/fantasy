"""模型连接测试服务。"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..clients.http_client import new_client

logger = logging.getLogger(__name__)


async def test_model_connection(
    base_url: str,
    model: str,
    role: str,
    *,
    api_key: str | None = None,
    chat_path: str = "/chat/completions",
    embed_path: str = "/embeddings",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """测试模型连接是否可用。

    Returns:
        {"ok": True} or {"ok": False, "error": "..."}
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with new_client(timeout=timeout) as client:
            if role == "stt":
                # STT 走 multipart /audio/transcriptions，无标准 chat/embed 探活端点，跳过
                return {"ok": True, "skipped": "STT 无需测试连接"}
            if role == "embed":
                url = base_url.rstrip("/") + embed_path
                payload = {"model": model, "input": "test"}
                resp = await client.post(url, json=payload, headers=headers)
            else:
                # chat, summary, vision all use chat completions
                url = base_url.rstrip("/") + chat_path
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                }
                resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code == 200:
                return {"ok": True}
            else:
                body = resp.text[:200]
                return {"ok": False, "error": f"HTTP {resp.status_code}: {body}"}

    except httpx.TimeoutException:
        return {"ok": False, "error": f"连接超时（{timeout}秒）"}
    except httpx.ConnectError as e:
        return {"ok": False, "error": f"连接失败: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"未知错误: {e}"}
