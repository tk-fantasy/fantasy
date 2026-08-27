"""高级配置路由 — 管理系统级参数（网页搜索、视觉、RAG）和 Embed 状态。

保存策略（跟 HA 一致）：用户填了新凭证时，先用候选凭证真连一次服务，
probe 通过才落盘。杜绝「脏凭证存进去、下次使用才发现不工作」。
留空字段跳过 probe（表示「不修改」）。

RTSP 源配置已随多摄像头体系移入 cameras 表(「摄像头设置」页 per-camera
管理,试连走 /api/cameras/{id}/test-stream),本路由不再涉及 RTSP。
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_admin
from ..core.config import get_config, update_config_section
from ..schema.api_schemas import AdvancedConfigRequest, VisionConfig
from ..services.config_probes import probe_exa

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/advanced/config")
async def get_advanced_config() -> ApiResponse[dict]:
    """获取高级配置（网页搜索、视觉、RAG）。

    exa.api_key 不回传明文（家庭成员互相不该看到付费 key），回 has_exa_key
    标志；前端保存时留空 = 不修改（见 set_advanced_config 的合并语义）。
    """
    vision_cfg = dict(get_config("vision", {}))
    # 密码不回传明文，只回「是否已配置」标志（像 weather 的 has_private_key）
    pwd_env = str(vision_cfg.get("rtsp_password_env", "")).strip()
    vision_cfg["has_rtsp_password"] = bool(pwd_env and os.getenv(pwd_env))
    exa_key = str(get_config("web_search.exa.api_key", "") or "")
    return ApiResponse(data={
        "web_search": {"exa": {"api_key": "", "has_exa_key": bool(exa_key)}},
        "vision": vision_cfg,
        "rag": get_config("rag", {}),
    })


@router.post("/advanced/config")
async def set_advanced_config(
    payload: AdvancedConfigRequest,
    current_user: dict = Depends(get_current_admin),
) -> ApiResponse[dict]:
    """保存高级配置（系统级参数，仅管理员）。

    凭证类字段（Exa api_key）填了新值时，先用候选值 probe 一次，
    probe 失败拒绝落盘并返回 reason，前端据此展示差异化错误。
    api_key 留空 = 保留已保存的旧值（GET 已脱敏不回明文，必须靠该语义
    防止"打开面板→保存"把已配置的 key 清空）。
    """
    # ---- Exa ----
    if payload.web_search is not None:
        exa_data = payload.web_search.model_dump()
        new_api_key = (exa_data.get("exa", {}).get("api_key", "") or "").strip()
        # 只有用户填了新 key 才 probe（留空 = 不修改，跳过）
        if new_api_key:
            result = await probe_exa(new_api_key)
            if not result.ok:
                logger.warning("Exa config save rejected: %s (%s)", result.reason, result.detail)
                return ApiResponse(
                    code="probe_failed",
                    message=result.detail,
                    data={"saved": False, "section": "exa", **result.to_dict()},
                )
        else:
            # 留空：保留旧 key（仅更新 exa 段其他字段，当前只有 api_key 一个字段）
            old_key = str(get_config("web_search.exa.api_key", "") or "")
            if old_key:
                exa_data.setdefault("exa", {})["api_key"] = old_key
        update_config_section("web_search", exa_data)
        logger.info("Web search config updated: api_key_set=%s", bool(new_api_key))

    # ---- Vision ----
    # 注意:此处只存视觉处理参数(分辨率/压缩/运动检测等)。RTSP 源配置
    # (url/用户名/密码)已随多摄像头体系移入 cameras 表,归「摄像头设置」页
    # per-camera 管理;vision 段里的 rtsp_* 字段是单摄时代的遗留,不参与任何
    # 运行时取流,也不再 probe——否则会被一个永不更新的旧 IP 卡死保存。
    if payload.vision is not None:
        vision_data = payload.vision.model_dump()
        update_config_section("vision", vision_data)
        logger.info("Vision config updated")

    # 兼容旧前端/脚本:rtsp_password 一律忽略(凭证请在摄像头设置里改)。

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
