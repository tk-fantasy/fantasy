"""语音转文字服务 — 代理 SiliconFlow /audio/transcriptions。

浏览器端用 MediaRecorder 录音，后端只接收音频文件转发给 STT 服务，
不在服务器侧录音（无需 pyaudio、不依赖服务器声卡、支持远程访问）。

STT 配置纳入 llm_keys 体系（type=stt），与 LLM key 统一在 /keys 页面管理。
本服务读取第一条 type=stt 且有 api_key 的条目。
"""
from __future__ import annotations

import logging

from ..clients.http_client import new_client
from ..core.config import get_config
from ..core.exceptions import AppException
from ..core.key_resolver import resolve_key_for_role

logger = logging.getLogger(__name__)

# STT 请求体较大、网络上行慢，给一个稍长的默认超时
_DEFAULT_TIMEOUT = 30.0


async def _resolve_config(user_id: str = "") -> dict:
    """从 providers.stt.key_id 指定的 key 取配置，未指定则自动选第一条 type=stt。

    有 user_id 时优先从该用户的 DB 解析 per-user key，无配置则回退全局。
    """
    if user_id:
        try:
            from ..core.key_resolver import resolve_key_for_role_user
            key = await resolve_key_for_role_user("stt", user_id)
            if key and key.get("api_key"):
                return {
                    "available": True,
                    "base_url": str(key.get("base_url", "")).rstrip("/"),
                    "model": str(key.get("model", "FunAudioLLM/SenseVoiceSmall")),
                    "api_key": key["api_key"],
                    "timeout": float(get_config("stt.timeout_seconds", _DEFAULT_TIMEOUT)),
                }
        except Exception:
            logger.debug("Failed to resolve per-user stt key, falling back to global", exc_info=True)

    # 回退全局
    key = resolve_key_for_role("stt")
    if not key:
        return {"available": False, "timeout": float(get_config("stt.timeout_seconds", _DEFAULT_TIMEOUT))}

    return {
        "available": True,
        "base_url": str(key.get("base_url", "")).rstrip("/"),
        "model": str(key.get("model", "FunAudioLLM/SenseVoiceSmall")),
        "api_key": key.get("api_key", ""),
        "timeout": float(get_config("stt.timeout_seconds", _DEFAULT_TIMEOUT)),
    }


async def transcribe(audio_bytes: bytes, filename: str, content_type: str, user_id: str = "") -> str:
    """转发音频到 STT 服务，返回识别文本。

    音频以 multipart 上传，API key 走环境变量（参照 LLM key 的 api_key_env 模式，
    不写进 config.json）。new_client 设了 trust_env=False，绕过系统代理。
    有 user_id 时优先用该用户的 STT key，无配置则回退全局。
    """
    cfg = await _resolve_config(user_id)
    if not cfg["available"]:
        logger.warning("STT API key not configured (no type=stt key in llm_keys)")
        raise AppException(
            "未配置语音识别 API Key，请在 /keys 页面添加 type=stt 的 Key",
            code="stt_no_key",
            http_status=400,
        )

    logger.info("STT transcribe: model=%s, file=%s, %d bytes", cfg["model"], filename, len(audio_bytes))
    try:
        async with new_client(timeout=cfg["timeout"]) as client:
            resp = await client.post(
                f"{cfg['base_url']}/audio/transcriptions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                files={"file": (filename, audio_bytes, content_type)},
                data={"model": cfg["model"]},
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.exception("STT upstream call failed")
        raise AppException(
            f"语音识别服务调用失败: {exc}",
            code="stt_upstream_error",
            http_status=502,
        ) from exc

    text = (resp.json() or {}).get("text", "")
    logger.info("STT transcribed: %d chars", len(text))
    return text
