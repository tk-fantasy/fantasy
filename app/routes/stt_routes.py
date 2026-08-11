"""语音识别路由 — 接收浏览器录音转发给 STT 服务。

STT 配置已纳入 llm_keys 体系（type=stt），在 /keys 页面统一管理，
本路由只保留转写入口。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, UploadFile

from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..services import stt_service

logger = logging.getLogger(__name__)

router = APIRouter()

# 音频上传大小上限：浏览器 MediaRecorder 录音通常几十~几百 KB，
# 25MB 足够覆盖长语音 + 高码率。防恶意大文件撑爆内存。
MAX_AUDIO_SIZE = 25 * 1024 * 1024  # 25 MB


@router.post("/stt/transcribe")
async def transcribe(audio: UploadFile = File(...), current_user: dict = Depends(get_current_user)) -> ApiResponse[dict]:
    """接收浏览器 MediaRecorder 录制的音频，转发给 STT 服务转文字。

    音频格式由浏览器决定（通常 audio/webm），后端原样转发，SiliconFlow
    SenseVoiceSmall 支持 webm/wav/mp3 等常见格式。鉴权由全局中间件保证。
    """
    audio_bytes = await audio.read()
    if not audio_bytes:
        return ApiResponse(code="invalid_input", message="音频为空", data={"text": ""})
    if len(audio_bytes) > MAX_AUDIO_SIZE:
        return ApiResponse(
            code="invalid_input",
            message=f"音频过大（{len(audio_bytes) // 1024 // 1024}MB > {MAX_AUDIO_SIZE // 1024 // 1024}MB 上限）",
            data={"text": ""},
        )
    text = await stt_service.transcribe(
        audio_bytes,
        filename=audio.filename or "voice.webm",
        content_type=audio.content_type or "audio/webm",
        user_id=current_user.get("user_id", ""),
    )
    return ApiResponse(data={"text": text})
