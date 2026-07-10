"""语音识别路由 — 接收浏览器录音转发给 STT 服务。

STT 配置已纳入 llm_keys 体系（type=stt），在 /keys 页面统一管理，
本路由只保留转写入口。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, UploadFile

from ..core.api_models import ApiResponse
from ..services import stt_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/stt/transcribe")
async def transcribe(audio: UploadFile = File(...)) -> ApiResponse[dict]:
    """接收浏览器 MediaRecorder 录制的音频，转发给 STT 服务转文字。

    音频格式由浏览器决定（通常 audio/webm），后端原样转发，SiliconFlow
    SenseVoiceSmall 支持 webm/wav/mp3 等常见格式。鉴权由全局中间件保证。
    """
    audio_bytes = await audio.read()
    if not audio_bytes:
        return ApiResponse(code="invalid_input", message="音频为空", data={"text": ""})
    text = await stt_service.transcribe(
        audio_bytes,
        filename=audio.filename or "voice.webm",
        content_type=audio.content_type or "audio/webm",
    )
    return ApiResponse(data={"text": text})
