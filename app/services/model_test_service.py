"""模型连接测试服务。"""
from __future__ import annotations

import io
import logging
import struct
import wave
from typing import Any

import httpx

from ..clients.http_client import new_client

logger = logging.getLogger(__name__)


def _silence_wav(duration_ms: int = 100, sample_rate: int = 16000) -> bytes:
    """生成一段静音 WAV（16-bit mono PCM），用于 STT 连接测试。

    SenseVoice 等模型接受 16kHz 单声道 WAV；100ms 静音足以让服务端返回
    200（空文本），用来验证 api_key + base_url + model 是否有效。
    """
    n_frames = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{n_frames}h", *([0] * n_frames)))
    return buf.getvalue()


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
                # STT 走 multipart /audio/transcriptions，发一段静音 WAV 验证
                # api_key + base_url + model 是否有效。服务端返回 200 即视为通过。
                url = base_url.rstrip("/") + "/audio/transcriptions"
                wav_bytes = _silence_wav()
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                    files={"file": ("test.wav", wav_bytes, "audio/wav")},
                    data={"model": model},
                )
            elif role == "embed":
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
