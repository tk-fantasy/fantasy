"""Assist 单轮对话桥 — HA conversation agent 的「脑子」入口（阶段7）。

入口归 HA（Assist 管线管 STT/TTS/wake word），复杂请求经 HA 自定义集成
aether_conversation 转到这里：鉴权用 X-API-Token（APP_TOKEN，机器对机器），
单轮转发给 dispatcher（非流式），从 ToastStream 收尾指令取回全文。

会话连续性：conversation_id 非空时映射为固定 session_id（assist_<id>），
HA 侧同一对话上下文在 Aether 里也是同一会话；不识别用户身份（Assist 语音
没有登录概念），user_id 固定 "assist"。
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Request

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.exceptions import AppException
from ..core.tracing import new_request_id
from ..schema.chat_schema import Dialog, Event, Instruction, Nlp, Template

logger = logging.getLogger(__name__)

router = APIRouter()


def _token_ok(request: Request) -> bool:
    """X-API-Token（APP_TOKEN）校验。未设置 APP_TOKEN 时桥接整体关闭。"""
    from ..main import APP_TOKEN
    if not APP_TOKEN:
        return False
    provided = request.headers.get("X-API-Token", "")
    return bool(provided) and secrets.compare_digest(provided, APP_TOKEN)


@router.get("/assist/status")
async def assist_status() -> ApiResponse[dict]:
    """Assist 对接就绪状态：组件文件（随仓库分发）+ APP_TOKEN。"""
    from pathlib import Path
    from ..main import APP_TOKEN
    component_dir = (Path(__file__).resolve().parents[2]
                     / "ha_config" / "custom_components" / "aether_conversation")
    deployed = (component_dir / "manifest.json").is_file() \
        and (component_dir / "__init__.py").is_file()
    return ApiResponse(data={
        "component_deployed": deployed,
        "token_configured": bool(APP_TOKEN),
        "ready": deployed and bool(APP_TOKEN),
        "hint": ("组件已随镜像/仓库分发；在 HA「设备与服务 → 添加集成 → Aether」填"
                 "地址与 Token（APP_TOKEN），再到「语音助手」把对话引擎选成 Aether。"
                 "若 HA 里看不到集成，确认 aether-ha 挂载的 ha_config 含该组件并重启 HA。"
                 if deployed and APP_TOKEN else
                 "APP_TOKEN 未设置（组件文件缺失时参见组件 README）。"),
    })


@router.post("/assist/chat")
async def assist_chat(
    body: dict,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """HA Assist → Aether 单轮对话。body: {text, conversation_id?}。"""
    if not _token_ok(request):
        raise AppException("X-API-Token 缺失或无效（APP_TOKEN 未设置时桥接不可用）",
                           code="unauthorized", http_status=401)
    text = str(body.get("text", "") or "").strip()
    if not text:
        raise AppException("text 不能为空", code="missing_params", http_status=400)
    conversation_id = str(body.get("conversation_id", "") or "").strip()
    dispatcher = getattr(container, "dispatcher", None)
    if dispatcher is None:
        raise AppException("对话服务未就绪", code="dispatcher_unavailable", http_status=503)

    session_id = f"assist_{conversation_id}" if conversation_id else new_request_id()
    rid = new_request_id()
    event = Event.build_event(
        Nlp.Request(query=text), request_id=rid, session_id=session_id)
    try:
        instructions = await dispatcher.dispatch(event, user_id="assist")
    except Exception as e:
        logger.exception("assist dispatch failed")
        raise AppException(f"处理失败: {e}", code="dispatch_failed", http_status=500)

    # 非流式收尾：全文在最后一条 ToastStream；Finish 带成功标志
    reply, success = "", True
    for inst in instructions or []:
        name = getattr(inst.header, "name", "")
        payload = getattr(inst, "payload", {}) or {}
        if name == Template.ToastStream.NAME and payload.get("stream"):
            reply = str(payload["stream"])
        elif name == Dialog.Finish.NAME:
            success = bool(payload.get("success", True))
    return ApiResponse(data={
        "reply": reply,
        "success": success,
        "session_id": session_id,
        "conversation_id": conversation_id,
    })
