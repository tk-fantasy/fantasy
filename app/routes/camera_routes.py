"""摄像头管理路由:吸收原 ptz_routes / discovery_routes / vision-focus / 状态/MJPEG 端点。

Task 6:多摄像头统一入口 /api/cameras/{camera_id}/... + /api/ha/areas。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.container import get_container
from app.core.api_models import ApiResponse
from app.core.database import Database

router = APIRouter()


# —— CRUD ——
@router.get("/cameras")
async def list_cameras():
    """摄像头列表(完整行)。前端管理页 + 工具注入共用。

    查 cameras 表完整行(含 display_enabled/enabled/source_type/rtsp_url 等
    前端卡片与编辑弹窗所需字段),合并运行时 online 状态(从 get_state 推断)。
    CameraManager.list_cameras() 只返 4 字段(id/name/area/online,给 MCP 工具
    轻量注入用),不能直接给前端——refetch 后 display_enabled 等字段丢失会导致
    开关乐观更新被覆盖回退。
    """
    c = get_container()
    rows = await Database.get().cameras_all()
    cm = c.camera_manager
    for row in rows:
        st = cm.get_state(row["id"]) if cm else None
        # CameraState 无 online 字段,用 camera_opened 推断(已打开=在线)
        row["online"] = bool(st.get("camera_opened", False)) if st else False
    return ApiResponse(data=rows)


@router.post("/cameras")
async def create_camera(body: dict):
    c = get_container()
    created = await c.camera_manager.create_camera(body)
    return ApiResponse(data=created)


@router.get("/cameras/{camera_id}")
async def get_camera(camera_id: str):
    c = get_container()
    st = c.camera_manager.get_state(camera_id)
    row = await Database.get().cameras_get(camera_id)
    if row is None:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    return ApiResponse(data={**row, "state": st})


@router.put("/cameras/{camera_id}")
async def update_camera(camera_id: str, body: dict):
    c = get_container()
    updated = await c.camera_manager.update_camera(camera_id, body)
    return ApiResponse(data=updated)


@router.delete("/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    c = get_container()
    ok = await c.camera_manager.delete_camera(camera_id)
    return ApiResponse(data={"deleted": ok})


# —— MJPEG 单路(替代旧 mcp_routes.py 单路端点)——
@router.get("/cameras/{camera_id}/video_feed")
async def video_feed(camera_id: str):
    c = get_container()
    return StreamingResponse(
        c.camera_manager.mjpeg_generator(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# —— 试连(前端保存前验证 RTSP 可达)——
@router.post("/cameras/{camera_id}/test-stream")
async def test_stream(camera_id: str, body: dict):
    """用 body 里的临时配置试打开 RTSP,不落库。返回 ok/error。"""
    url = str(body.get("rtsp_url", "")).strip()
    if not url:
        return ApiResponse(data={"ok": False, "error": "rtsp_url 为空"})
    import cv2
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    ok = cap.isOpened()
    err = ""
    if ok:
        ret, _ = cap.read()
        ok = ret
        if not ret:
            err = "打开但读不到帧"
    else:
        err = "打不开(检查 url/凭证/网络)"
    cap.release()
    return ApiResponse(data={"ok": ok, "error": err})


# —— AI 预览单例切换(D4)——
@router.post("/cameras/{camera_id}/display/enable")
async def enable_display(camera_id: str):
    c = get_container()
    await c.camera_manager.enable_display(camera_id)
    return ApiResponse(data={"ok": True})


@router.post("/cameras/{camera_id}/display/disable")
async def disable_display(camera_id: str):
    c = get_container()
    await c.camera_manager.disable_display(camera_id)
    return ApiResponse(data={"ok": True})


@router.get("/cameras/{camera_id}/state")
async def camera_state(camera_id: str):
    c = get_container()
    return ApiResponse(data=c.camera_manager.get_state(camera_id))


# —— PTZ(从 ptz_routes 迁入)——
@router.post("/cameras/{camera_id}/ptz/move")
async def ptz_move(camera_id: str, body: dict):
    c = get_container()
    row = await Database.get().cameras_get(camera_id)
    svc = await c.ptz_registry.get(camera_id, row)
    return ApiResponse(data=await svc.move(body.get("direction", "")))


@router.post("/cameras/{camera_id}/ptz/stop")
async def ptz_stop(camera_id: str):
    c = get_container()
    row = await Database.get().cameras_get(camera_id)
    svc = await c.ptz_registry.get(camera_id, row)
    return ApiResponse(data=await svc.stop())


@router.post("/cameras/{camera_id}/ptz/step")
async def ptz_step(camera_id: str, body: dict):
    c = get_container()
    row = await Database.get().cameras_get(camera_id)
    svc = await c.ptz_registry.get(camera_id, row)
    return ApiResponse(data=await svc.step(body.get("direction", ""), int(body.get("duration_ms", 300))))


# —— ONVIF 发现(从 discovery_routes 迁入)——
@router.post("/cameras/{camera_id}/discovery/find")
async def discovery_find(camera_id: str):
    c = get_container()
    new_ip = await c.discovery_service.find_and_apply(camera_id)
    return ApiResponse(data={"new_ip": new_ip})


@router.post("/cameras/{camera_id}/discovery/manual-ip")
async def discovery_manual_ip(camera_id: str, body: dict):
    c = get_container()
    await c.discovery_service.apply_found_ip(camera_id=camera_id, new_ip=body.get("ip", ""))
    return ApiResponse(data={"ok": True})


# —— 视觉关注项(从 settings_routes 迁入,per-camera)——
# 持久化:每次增删改后把全部 bucket 拍平写回 KV,重启 load_focuses 重新分桶。
async def _persist_focuses():
    """把 VisionService 全部关注项拍平写回 KV。"""
    import json
    c = get_container()
    try:
        db = Database.get()
    except RuntimeError:
        return  # 测试环境 DB 未初始化,跳过持久化
    await db.kv_set("vision_focuses", json.dumps(
        c.vision_service.get_all_focuses_flat(), ensure_ascii=False))


@router.get("/cameras/{camera_id}/focuses")
async def list_focuses(camera_id: str):
    c = get_container()
    return ApiResponse(data=c.vision_service.get_vision_focuses(camera_id))


@router.post("/cameras/{camera_id}/focuses")
async def add_focus(camera_id: str, body: dict):
    c = get_container()
    result = c.vision_service.add_focus(body.get("text", ""), camera_id=camera_id)
    await _persist_focuses()
    return ApiResponse(data=result)


@router.put("/cameras/{camera_id}/focuses/{focus_id}")
async def update_focus(camera_id: str, focus_id: str, body: dict):
    c = get_container()
    result = c.vision_service.update_focus(
        focus_id, text=body.get("text"), enabled=body.get("enabled"), camera_id=camera_id)
    await _persist_focuses()
    return ApiResponse(data=result)


@router.delete("/cameras/{camera_id}/focuses/{focus_id}")
async def delete_focus(camera_id: str, focus_id: str):
    c = get_container()
    deleted = c.vision_service.delete_focus(focus_id, camera_id=camera_id)
    await _persist_focuses()
    return ApiResponse(data={"deleted": deleted})


# —— HA areas(补 spec §7.1 区域下拉所需,当前缺)——
@router.get("/ha/areas")
async def list_areas():
    c = get_container()
    return ApiResponse(data=await c.ha_service.get_areas())
