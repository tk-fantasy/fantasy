"""摄像头管理路由:吸收原 ptz_routes / discovery_routes / vision-focus / 状态/MJPEG 端点。

Task 6:多摄像头统一入口 /api/cameras/{camera_id}/... + /api/ha/areas。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.container import get_container
from app.core.api_models import ApiResponse
from app.core.config import get_config
from app.core.database import Database

router = APIRouter()

# 凭证不回传前端：密码列从 GET 响应剥除，只回 has_* 标志
# （编辑表单密码留空=不改，PUT 不传密码字段即沿用旧值，流程不受影响）
_PASSWORD_FIELDS = ("rtsp_password", "ptz_password")


def _mask_camera(row: dict) -> dict:
    out = dict(row)
    for f in _PASSWORD_FIELDS:
        if f in out:
            out[f"has_{f}"] = bool(out.pop(f))
    return out


# —— CRUD ——
@router.get("/cameras")
async def list_cameras():
    """摄像头列表。前端管理页用。

    返回 cameras 表行(含 display_enabled/enabled/source_type/rtsp_url 等
    前端卡片与编辑弹窗所需字段)+ 运行时 online；rtsp_password/ptz_password
    不回传，换 has_rtsp_password/has_ptz_password 标志。CameraManager.
    list_cameras() 只返 4 字段(id/name/area/online,给 MCP 工具轻量注入用),
    refetch 后 display_enabled 等字段丢失会导致开关乐观更新被覆盖回退,故路由用完整版。
    """
    c = get_container()
    rows = await c.camera_manager.cameras_all()
    return ApiResponse(data=[_mask_camera(r) for r in rows])


@router.post("/cameras")
async def create_camera(body: dict):
    c = get_container()
    created = await c.camera_manager.create_camera(body)
    return ApiResponse(data=_mask_camera(created))


@router.get("/cameras/{camera_id}")
async def get_camera(camera_id: str):
    c = get_container()
    st = c.camera_manager.get_state(camera_id)
    row = await Database.get().cameras_get(camera_id)
    if row is None:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    return ApiResponse(data=_mask_camera({**row, "state": st}))


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
    """用 body 里的临时配置试打开 RTSP,不落库。返回 ok/error。

    复用 CameraStream._open_network_stream 的打开参数(rtsp_transport=tcp +
    低延迟 + 凭证注入),否则在需要 TCP/鉴权的环境下试连必失败但 worker 能连
    (worker 走 _resolve_rtsp_url 注入凭证 + 设了 OPENCV_FFMPEG_CAPTURE_OPTIONS)。
    """
    import os, time
    import cv2
    from ..core.net_guard import STREAM_SCHEMES, url_scheme_error
    base = str(body.get("rtsp_url", "")).strip()
    if not base:
        return ApiResponse(data={"ok": False, "error": "rtsp_url 为空"})
    # scheme 白名单：rtsp/rtsps/rtmp/http/https。FFmpeg 还能打开 file:/concat:/
    # pipe: 等协议（file:// 可读本地文件），rtsp_url 用户可控必须拦。
    scheme_err = url_scheme_error(base, STREAM_SCHEMES)
    if scheme_err:
        return ApiResponse(data={"ok": False, "error": scheme_err})
    # 复用 worker 在线状态:该路已在线 + body url 与当前 DB 配置一致 → 直接成功。
    # worker 持着该路 RTSP 连接,试连开第二个会被服务器并发拒绝(表现为 isOpened
    # False 但 worker 能抓帧);worker 在线 = 可达,无需重复开连接。
    c = get_container()
    cm = c.camera_manager
    if cm is not None:
        try:
            row = await Database.get().cameras_get(camera_id)
        except Exception:
            row = None
        if row is not None:
            st = cm.get_state(camera_id)
            if st.get("camera_opened") and str(row.get("rtsp_url", "") or "").strip() == base:
                return ApiResponse(data={"ok": True, "error": ""})
    # 凭证注入:rtsp://host → rtsp://user:pwd@host(与 _resolve_rtsp_url 一致)
    # user/pwd 必须 percent-encode：含 @:/# 等特殊字符会破坏 URL 结构。
    from urllib.parse import quote
    user = str(body.get("rtsp_username", "") or "").strip()
    pwd = str(body.get("rtsp_password", "") or "").strip()
    url = base
    if user and pwd and "://" in base:
        scheme, rest = base.split("://", 1)
        url = f"{scheme}://{quote(user, safe='')}:{quote(pwd, safe='')}@{rest}"

    def _probe_sync() -> tuple[bool, str]:
        """同步试连（在线程里跑，绝不碰事件循环）。

        FFmpeg 打开是不可中断的阻塞调用：目标断网/防火墙丢包时 connect 能挂
        几十秒——此前直接跑在 async handler 里，期间全站（WS 聊天、scheduler
        tick、HA 调用）冻结。这里加了两层时限：
        - FFmpeg 层 timeout（5s socket 超时）保证线程自身会退出（线程不可强杀，
          外层超时后泄漏的线程靠这个自行结束）；
        - 外层 asyncio.wait_for 12s 保证 handler 一定返回。
        """
        transport = str(get_config("vision.rtsp_transport", "tcp")).strip().lower() or "tcp"
        # RTSP over TCP + 低延迟(与 _open_network_stream 一致;默认 UDP 在 NAT/桥接
        # 网络下信令通但拿不到帧)。timeout 单位 µs（FFmpeg rtsp 私有选项）。
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            f"rtsp_transport;{transport}"
            f"|buffer_size;256k"
            f"|max_delay;100000"
            f"|fflags;nobuffer+discardcorrupt"
            f"|flags;low_delay"
            f"|timeout;5000000"
        )
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        try:
            if not cap.isOpened():
                return False, "打不开(检查 url/凭证/网络/transport)"
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # 预热读:首帧可能慢(握手+I 帧),多试几次
            for _ in range(10):
                ret, frame = cap.read()
                if ret and frame is not None:
                    return True, ""
                time.sleep(0.1)
            return False, "打开但读不到帧"
        finally:
            cap.release()

    import asyncio
    try:
        ok, err = await asyncio.wait_for(asyncio.to_thread(_probe_sync), timeout=12.0)
    except asyncio.TimeoutError:
        ok, err = False, "试连超时（12 秒无响应）"
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
