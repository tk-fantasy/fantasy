"""高级配置路由 — 管理系统级参数（网页搜索、视觉、RAG）和 Embed 状态。

保存策略（跟 HA 一致）：用户填了新凭证时，先用候选凭证真连一次服务，
probe 通过才落盘。杜绝「脏凭证存进去、下次使用才发现不工作」。
留空字段跳过 probe（表示「不修改」）。
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section, write_secrets
from ..schema.api_schemas import AdvancedConfigRequest, VisionConfig
from ..services.config_probes import probe_exa, probe_rtsp

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


def _resolve_rtsp_password_for_probe(new_password: str) -> str:
    """拿到 probe 要用的密码：优先用户新填的，否则从 .env 读现有的。

    跟 CameraStream._resolve_rtsp_url 一致：config.json 只存变量名，
    真密码在 .env 的 RTSP_PASSWORD。
    """
    pwd = (new_password or "").strip()
    if pwd:
        return pwd
    return os.getenv("RTSP_PASSWORD", "")


@router.post("/advanced/config")
async def set_advanced_config(
    payload: AdvancedConfigRequest,
) -> ApiResponse[dict]:
    """保存高级配置。

    凭证类字段（Exa api_key / RTSP url+密码）填了新值时，先用候选值 probe
    一次，probe 失败拒绝落盘并返回 reason，前端据此展示差异化错误。
    """
    # ---- Exa ----
    if payload.web_search is not None:
        exa_data = payload.web_search.model_dump()
        new_api_key = (exa_data.get("exa", {}).get("api_key", "") or "").strip()
        # 只有用户填了新 key 才 probe（留空 = 匿名/不修改，跳过）
        if new_api_key:
            result = await probe_exa(new_api_key)
            if not result.ok:
                logger.warning("Exa config save rejected: %s (%s)", result.reason, result.detail)
                return ApiResponse(
                    code="probe_failed",
                    message=result.detail,
                    data={"saved": False, "section": "exa", **result.to_dict()},
                )
        update_config_section("web_search", exa_data)
        logger.info("Web search config updated: api_key_set=%s", bool(new_api_key))

    # ---- Vision / RTSP ----
    if payload.vision is not None:
        vision_data = payload.vision.model_dump()
        rtsp_url = (vision_data.get("rtsp_url", "") or "").strip()
        # 配了 RTSP URL 才 probe（留空 = 走 USB，不验证）
        if rtsp_url:
            username = (vision_data.get("rtsp_username", "") or "").strip()
            password = _resolve_rtsp_password_for_probe(payload.rtsp_password)
            result = await probe_rtsp(rtsp_url, username, password)
            if not result.ok:
                logger.warning("RTSP config save rejected: %s (%s)", result.reason, result.detail)
                return ApiResponse(
                    code="probe_failed",
                    message=result.detail,
                    data={"saved": False, "section": "rtsp", **result.to_dict()},
                )
            # 固定变量名，保证 _resolve_rtsp_url 能读到
            vision_data["rtsp_password_env"] = "RTSP_PASSWORD"
            # 自动从 RTSP URL 提取 IP → 同步到 ptz.ip（同一摄像头）
            from ..services.ptz_service import extract_host_from_url

            host = extract_host_from_url(rtsp_url)
            if host:
                update_config_section("ptz", {"ip": host})
                logger.info("PTZ ip auto-synced from RTSP URL: %s", host)
        update_config_section("vision", vision_data)
        logger.info(
            "Vision config updated: rtsp_url_set=%s",
            bool(rtsp_url),
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


@router.post("/advanced/test/exa")
async def test_exa_connection() -> ApiResponse[dict]:
    """测试 Exa 连接（用当前已保存的 api_key）。

    前端「测试连接」按钮调用。api_key 为空时测匿名调用是否通。
    """
    api_key = str(get_config("web_search.exa.api_key", "") or "")
    result = await probe_exa(api_key)
    if not result.ok:
        logger.warning("Exa test failed: %s (%s)", result.reason, result.detail)
    return ApiResponse(
        code="probe_failed" if not result.ok else "ok",
        message=result.detail,
        data={"connected": result.ok, **result.to_dict()},
    )


@router.post("/advanced/test/rtsp")
async def test_rtsp_connection() -> ApiResponse[dict]:
    """测试 RTSP 连接（用当前已保存的 url/username/password）。

    前端「测试连接」按钮调用。注意：probe 会临时占一路流，如果当前
    摄像头 worker 已在拉同一路流（单流摄像头），probe 可能失败。
    """
    rtsp_url = str(get_config("vision.rtsp_url", "") or "").strip()
    if not rtsp_url:
        return ApiResponse(
            code="probe_failed",
            message="未配置 RTSP URL",
            data={"connected": False, "reason": "bad_format", "detail": "未配置 RTSP URL"},
        )
    username = str(get_config("vision.rtsp_username", "") or "").strip()
    password = os.getenv("RTSP_PASSWORD", "")
    result = await probe_rtsp(rtsp_url, username, password)
    if not result.ok:
        logger.warning("RTSP test failed: %s (%s)", result.reason, result.detail)
    return ApiResponse(
        code="probe_failed" if not result.ok else "ok",
        message=result.detail,
        data={"connected": result.ok, **result.to_dict()},
    )


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
