"""高级配置路由 — 管理系统级参数（网页搜索、视觉、RAG）和 Embed 状态。"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section, write_secrets
from ..schema.api_schemas import AdvancedConfigRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/advanced/config")
async def get_advanced_config() -> ApiResponse[dict]:
    """获取高级配置（网页搜索、视觉、RAG）。"""
    vision_cfg = dict(get_config("vision", {}))
    # 密码不回传明文，只回「是否已配置」标志（像 weather 的 has_private_key）
    pwd_env = str(vision_cfg.get("rtsp_password_env", "")).strip()
    vision_cfg["has_rtsp_password"] = bool(pwd_env and os.getenv(pwd_env))
    return ApiResponse(data={
        "web_search": get_config("web_search", {}),
        "vision": vision_cfg,
        "rag": get_config("rag", {}),
    })


@router.post("/advanced/config")
async def set_advanced_config(
    payload: AdvancedConfigRequest,
) -> ApiResponse[dict]:
    """保存高级配置。"""
    if payload.web_search is not None:
        exa_data = payload.web_search.model_dump()
        update_config_section("web_search", exa_data)
        logger.info("Web search config updated: api_key_set=%s", bool(exa_data.get("api_key")))

    if payload.vision is not None:
        # 摄像头源：RTSP URL + 用户名进 config.json，密码单独走 .env
        vision_data = payload.vision.model_dump()
        # 固定变量名，保证 _resolve_rtsp_url 能读到
        if vision_data.get("rtsp_url"):
            vision_data["rtsp_password_env"] = "RTSP_PASSWORD"
            # 自动从 RTSP URL 提取 IP → 同步到 ptz.ip（同一摄像头）
            from ..services.ptz_service import extract_host_from_url

            host = extract_host_from_url(vision_data["rtsp_url"])
            if host:
                update_config_section("ptz", {"ip": host})
                logger.info("PTZ ip auto-synced from RTSP URL: %s", host)
        update_config_section("vision", vision_data)
        logger.info(
            "Vision config updated: rtsp_url_set=%s",
            bool(vision_data.get("rtsp_url")),
        )

    # RTSP 密码：留空表示不修改，非空才写 .env
    if payload.rtsp_password:
        write_secrets({"RTSP_PASSWORD": payload.rtsp_password})
        # 确保 config.json 记住变量名（_resolve_rtsp_url 靠它取密码）
        update_config_section("vision", {"rtsp_password_env": "RTSP_PASSWORD"})
        logger.info("RTSP password updated in .env")

    if payload.rag is not None:
        update_config_section("rag", payload.rag.model_dump())
        logger.info("RAG config updated")

    return ApiResponse(data={"saved": True})


@router.get("/advanced/embed-status")
async def get_embed_status(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """获取 Embed 模型状态和各搜索功能可用性。"""
    # 检查 embed 模型是否配置
    embed_keys = [k for k in get_config("llm_keys", []) if k.get("type") == "embed"]
    embed_configured = len(embed_keys) > 0
    embed_model = embed_keys[0].get("model", "") if embed_keys else ""

    # 检查 providers.embed 是否设置了 key_id
    embed_provider = get_config("providers.embed", {})
    embed_key_id = embed_provider.get("key_id", "")
    if not embed_key_id and embed_keys:
        # 没有指定 key_id 但有 embed 类型的 key，算已配置
        embed_configured = True

    # 检查 Emoji 搜索可用性
    emoji_available = embed_configured

    # 检查 RAG 可用性（通过 RagService 读取索引状态）
    rag_service = container.rag_service
    rag_available = rag_service is not None and rag_service.is_ready
    rag_chunks = rag_service.chunk_count if rag_service else 0

    return ApiResponse(data={
        "configured": embed_configured,
        "model": embed_model,
        "emoji_available": emoji_available,
        "rag_available": rag_available,
        "rag_chunks": rag_chunks,
    })
