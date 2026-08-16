"""PTZ（云台）控制服务 — 封装 ONVIF 协议，复用连接。

参考 scripts 桌面的 ptz_control.py（键盘 WASD 版），改为长连接复用：
每次 move/stop/step 不重建 ONVIFCamera，省去 wsdiscovery + 鉴权握手（~1s）。

ContinuousMove 是持续运动指令，摄像头会一直转到收到 Stop 为止。
- move/stop：按住式控制（前端松手调 stop）。
- step：点按式步进 —— ContinuousMove 一小段后自动 Stop，到点必停，
  停转由后端保证，不依赖前端事件，避免转飞。

所有 ONVIF 调用都在 self._lock 下串行，防止 move/stop 跨协程竞态
（zeep client 非线程安全，并发调用会让 Stop 丢失，表现为"转到底"）。

onvif-zeep-async 4.x 的 ONVIFCamera 构造和 create_*_service 都是 async
（内部用 aiohttp），所以整个 service 改为 async API。
service 对象的 GetProfiles/ContinuousMove/Stop 等仍是同步调用（zeep 同步 client）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from urllib.parse import urlparse

from ..core.config import get_config

logger = logging.getLogger(__name__)


def extract_host_from_url(url: str) -> str:
    """从 RTSP/HTTP URL 提取主机 IP（去端口、路径、凭据）。

    rtsp://admin:pass@192.168.1.100:554/stream → 192.168.1.100
    rtsp://192.168.1.100:554/stream            → 192.168.1.100
    rtsp://192.168.1.100/stream                → 192.168.1.100
    空串 / 无效 URL                              → ""
    """
    if not url or not url.strip():
        return ""
    return urlparse(url.strip()).hostname or ""

# 方向 → (pan, tilt) 速度向量。ONVIF PanTilt：x=pan(左右), y=tilt(上下)。
# 数值范围 -1~1，正负由设备安装方向定；这里按常见约定：
#   pan 正=右，tilt 正=上。speed 配置项可整体缩放。
_DIRECTION_VECTORS: dict[str, tuple[float, float]] = {
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
}


class PtzService:
    """ONVIF PTZ 控制服务，懒加载连接、单例复用。

    首次调用时才创建 ONVIFCamera（wsdiscovery 较慢，避免启动卡顿）。
    连接失败标记 _broken，下次调用自动重连，避免每条指令都吃超时。
    """

    def __init__(self, camera_id: str = "", config: dict | None = None) -> None:
        # Task 5:per-camera 参数化。config 非空时从 cameras 行读 ptz_* 字段;
        # config=None 时回退读全局 get_config("ptz.*")(向后兼容旧单例)。
        self.camera_id = camera_id
        self._config = config or {}
        self._cam = None
        self._ptz = None
        self._profile_token: str | None = None
        self._lock = asyncio.Lock()
        self._broken = False
        self._step_token = 0  # 最新步进序号；新 step 使进行中的旧 step 提前交权

    def _enabled(self) -> bool:
        if self._config:
            return bool(self._config.get("ptz_enabled", 0))
        return bool(get_config("ptz.enabled", False))

    async def _ensure_connected(self) -> bool:
        """懒加载 + 断线重连。返回是否就绪。已连接直接返回 True。
        调用方须持有 self._lock（避免与其它 ONVIF 调用并发建连）。"""
        if self._cam is not None and not self._broken:
            return True
        if not self._enabled():
            return False
        # Task 5:per-camera 从 config 读;旧路径从 get_config 读
        if self._config:
            ip = str(self._config.get("ptz_ip", ""))
            port = int(self._config.get("ptz_port", 80))
            user = str(self._config.get("ptz_username", ""))
            pwd = str(self._config.get("ptz_password", ""))   # cameras 表存明文
        else:
            ip = str(get_config("ptz.ip", ""))
            port = int(get_config("ptz.port", 80))
            user = str(get_config("ptz.username", ""))
            pwd_env = str(get_config("ptz.password_env", ""))
            pwd = os.getenv(pwd_env, "") if pwd_env else ""
        if not ip:
            logger.warning("PTZ ip not configured (cam=%s)", self.camera_id or "global")
            return False
        try:
            import onvif
            from onvif import ONVIFCamera
            logger.info("Connecting to PTZ camera %s:%d (cam=%s)", ip, port, self.camera_id or "global")
            wsdl_dir = os.path.join(os.path.dirname(onvif.__file__), "wsdl")
            self._cam = ONVIFCamera(ip, port, user, pwd, wsdl_dir=wsdl_dir)
            await self._cam.update_xaddrs()
            media = await self._cam.create_media_service()
            profiles = await media.GetProfiles()
            if not profiles:
                logger.warning("PTZ camera has no media profiles")
                self._broken = True
                return False
            self._profile_token = profiles[0].token
            self._ptz = await self._cam.create_ptz_service()
            self._broken = False
            logger.info("PTZ connected, profile=%s (cam=%s)", self._profile_token, self.camera_id or "global")
            return True
        except Exception:
            logger.exception("PTZ connect failed (cam=%s)", self.camera_id or "global")
            self._broken = True
            return False

    def _speed(self) -> float:
        if self._config:
            return max(0.1, min(1.0, float(self._config.get("ptz_speed", 0.5))))
        return max(0.1, min(1.0, float(get_config("ptz.speed", 0.5))))

    def notify_ip_changed(self, new_ip: str, camera_id: str = "") -> None:
        """通知 PTZ 摄像头 IP 已变(由 discovery 调用):作废缓存连接。

        Task 3:加 camera_id 参数(过渡兼容,默认空串=旧行为)。完整 per-camera
        PTZ 在 Task 5 PtzRegistry 处理。config.ptz.ip 已被 discovery 更新过
        (写入内存 + 磁盘),这里只清掉旧 IP 建的 ONVIFCamera 缓存,下次
        _ensure_connected 会读新 config 的 IP 懒重连。同步操作,不需锁。
        """
        logger.info("PTZ notified of IP change → %s (cam=%s), will reconnect",
                    new_ip, camera_id or "global")
        self._cam = None
        self._ptz = None
        self._profile_token = None
        self._broken = True

    async def _continuous_move_locked(self, direction: str, vec: tuple[float, float]) -> None:
        """发 ContinuousMove（须持有 self._lock）。4.x service 方法是 async。"""
        pan, tilt = vec
        spd = self._speed()
        req = self._ptz.create_type("ContinuousMove")
        req.ProfileToken = self._profile_token
        req.Velocity = {"PanTilt": {"x": pan * spd, "y": tilt * spd}, "Zoom": {"x": 0}}
        await self._ptz.ContinuousMove(req)
        logger.info("PTZ move %s (pan=%.2f tilt=%.2f)", direction, pan * spd, tilt * spd)

    async def _stop_locked(self) -> None:
        """发 Stop（须持有 self._lock）。带 PanTilt=True，与 ptz_control.py 一致；
        部分摄像头对纯 ProfileToken 的 Stop 响应不稳。Stop 在已停止时报错属正常，忽略。"""
        try:
            await self._ptz.Stop({"ProfileToken": self._profile_token, "PanTilt": True})
        except Exception as exc:
            logger.debug("PTZ stop error (ignored): %s", exc)

    async def move(self, direction: str) -> dict:
        """开始持续向某方向转动，直到 stop() 被调用。"""
        vec = _DIRECTION_VECTORS.get(direction)
        if vec is None:
            return {"success": False, "error": f"unknown direction: {direction}"}
        async with self._lock:
            if not await self._ensure_connected():
                return {"success": False, "error": "PTZ not connected"}
            await self._stop_locked()  # 清除残留转动状态，避免 500
            try:
                await self._continuous_move_locked(direction, vec)
            except Exception as exc:
                self._broken = True
                logger.exception("PTZ move failed")
                return {"success": False, "error": str(exc)}
            return {"success": True, "direction": direction}

    async def stop(self) -> dict:
        """停止转动。松开按钮或紧急停转时调用。"""
        async with self._lock:
            if not await self._ensure_connected():
                return {"success": False, "error": "PTZ not connected"}
            await self._stop_locked()
            return {"success": True}

    async def step(self, direction: str, duration_ms: int) -> dict:
        """步进：ContinuousMove 一小段后自动 Stop，实现"按一下动一下"。

        停转在后端保证：即使前端关页面，到点也会 Stop，不会转飞。
        新的 step 会使进行中的旧 step 提前交权（旧 step 不再发 Stop，
        由新 step 接管），快速连点也能即时换向。
        """
        vec = _DIRECTION_VECTORS.get(direction)
        if vec is None:
            return {"success": False, "error": f"unknown direction: {direction}"}
        async with self._lock:
            if not await self._ensure_connected():
                return {"success": False, "error": "PTZ not connected"}
            # 先 Stop 清除可能残留的转动状态：此摄像头在 ContinuousMove 残留时
            # 会对新指令返回 500，必须先 Stop 解锁（实测四方向均需此前置 Stop）。
            await self._stop_locked()
            try:
                await self._continuous_move_locked(direction, vec)
            except Exception as exc:
                self._broken = True
                logger.exception("PTZ step move failed")
                return {"success": False, "error": str(exc)}
            self._step_token += 1
            token = self._step_token
        # 锁外等待步进时长；新 step 到来则提前交权，不再发 Stop
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, duration_ms) / 1000.0
        while loop.time() < deadline:
            if self._step_token != token:
                return {"success": True, "interrupted": True}
            await asyncio.sleep(0.02)
        async with self._lock:
            if self._step_token == token:
                await self._stop_locked()
                logger.info("PTZ step %s auto-stop after %dms", direction, duration_ms)
        return {"success": True}


class PtzRegistry:
    """Task 5:按 camera_id 管理 PtzService 实例。每路独立连接态。

    camera_routes 用 await registry.get(camera_id, row) 拿到该路的 PtzService;
    discovery IP 变更时 registry.notify_ip_changed 通知该路重连。
    """

    def __init__(self) -> None:
        self._by_cam: dict[str, PtzService] = {}
        self._lock = asyncio.Lock()

    async def get(self, camera_id: str, config: dict) -> PtzService:
        async with self._lock:
            svc = self._by_cam.get(camera_id)
            if svc is None:
                svc = PtzService(camera_id=camera_id, config=config)
                self._by_cam[camera_id] = svc
            return svc

    def notify_ip_changed(self, camera_id: str, new_ip: str) -> None:
        svc = self._by_cam.get(camera_id)
        if svc is not None:
            svc.notify_ip_changed(new_ip, camera_id=camera_id)


ptz_registry = PtzRegistry()
