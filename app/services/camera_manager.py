"""多路摄像头生命周期管理 + 单通道并发调度 + 单路 AI 预览。

设计(D1/D3/D4):
- 全路 worker 抓帧 + dHash 运动检测(CAP.read 释放 GIL、dHash 微秒级,
  4 路几乎不吃 CPU)。
- MJPEG 编码只发生在有人请求 /video_feed 时(mjpeg_generator 惰性 HTTP 流),
  无需 manager 介入 → ARM A55 上天然只编码"当前看的那路"。
- AI 预览推理(classify_frame)全局单例:_active_display_id 同一时刻只 1 路,
  切换旧停新启。封号兜底严格成立(预览恒=1)。
- 自动化+工具共享 _auto_sem(可配 automation.vlm_auto_concurrency,默认 5,
  钳到 [1,9])。峰值 = 1 预览 + N 自动 ≤ 10(glm-4v 上限)。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..camera_stream import CameraStream

logger = logging.getLogger(__name__)


class CameraManager:
    def __init__(
        self,
        vision_service: Any = None,
        ha_service: Any = None,
        db: Any = None,
        discovery_service: Any = None,
        automation_service: Any = None,
        auto_concurrency: int | None = None,
    ) -> None:
        # 并发上限可配:用户在 config.json 调 automation.vlm_auto_concurrency(默认 5)。
        # 钳到 [1,9]:AI 预览固定 1 + 自动化 N ≤ 10(glm-4v 上限)。
        from ..core.config import get_config
        if auto_concurrency is None:
            auto_concurrency = int(get_config("automation.vlm_auto_concurrency", 5))
        auto_concurrency = max(1, min(9, auto_concurrency))
        self._vision_service = vision_service
        self._ha_service = ha_service
        self._db = db
        self._discovery_service = discovery_service
        self._automation_service = automation_service
        self._streams: dict[str, CameraStream] = {}
        # D4:AI 预览单例(非 Semaphore —— enable/disable_display 直接切 active,
        # 全局同一时刻只 1 路预览推理)。
        self._active_display_id: str | None = None
        self._auto_sem = asyncio.Semaphore(auto_concurrency)
        self._loop: asyncio.AbstractEventLoop | None = None
        if discovery_service is not None:
            discovery_service.set_on_ip_changed(self._on_camera_ip_changed)

    # —— 后注入 setter(bootstrap 顺序兜底,见 Task 7)——
    def set_db(self, db) -> None:
        self._db = db

    def set_ha_service(self, svc) -> None:
        self._ha_service = svc

    def set_automation_service(self, svc) -> None:
        self._automation_service = svc

    # —— 生命周期 ——
    async def initialize(self) -> None:
        """D4:启动所有 enabled 路的 worker(全抓帧 + 运动检测);
        AI 预览只激活 display_enabled=1 的第一路,其余待激活(前端切过去才起)。
        """
        rows = await self._db.cameras_all()
        display_activated = False
        for row in rows:
            if not row.get("enabled", 1):
                continue
            stream = await self._spawn(row)
            # D4:只给第一个 display_enabled=1 的路起 AI 预览
            if not display_activated and row.get("display_enabled", 1):
                stream.start_display()
                self._active_display_id = row["id"]
                display_activated = True

    async def _spawn(self, row: dict) -> CameraStream:
        """根据 cameras 行构造一路并启动 worker(抓帧 + 运动检测)。"""
        cid = row["id"]
        stream = CameraStream(
            camera_id=cid,
            config=row,
            vision_service=self._vision_service,
            on_automation_trigger=self._on_automation_trigger,
            discovery_service=self._discovery_service,
        )
        if self._loop is not None:
            stream.set_event_loop(self._loop)
        if self._discovery_service is not None:
            stream.set_discovery_service(self._discovery_service)
        self._streams[cid] = stream
        stream.start()
        return stream

    def set_event_loop(self, loop) -> None:
        self._loop = loop
        for s in self._streams.values():
            s.set_event_loop(loop)

    def stop(self) -> None:
        for s in self._streams.values():
            try:
                s.stop()
            except Exception:  # noqa: BLE001
                logger.exception("stop camera %s failed", getattr(s, "camera_id", "?"))

    # —— CRUD(转发 DB + 增删 stream)——
    async def create_camera(self, data: dict) -> dict:
        import secrets
        data.setdefault("id", f"cam_{secrets.token_hex(3)}")
        await self._db.cameras_insert(data)
        if data.get("enabled", 1):
            await self._spawn(data)
        return data

    async def update_camera(self, camera_id: str, fields: dict) -> dict:
        await self._db.cameras_update(camera_id, fields)
        # 简单策略:重建该路(参数变了)
        old = self._streams.pop(camera_id, None)
        if old:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                logger.exception("stop old stream %s failed", camera_id)
        row = await self._db.cameras_get(camera_id)
        if row and row.get("enabled", 1):
            await self._spawn(row)
        return row

    async def delete_camera(self, camera_id: str) -> bool:
        old = self._streams.pop(camera_id, None)
        if old:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                logger.exception("stop stream %s on delete failed", camera_id)
        if self._active_display_id == camera_id:
            self._active_display_id = None
        return await self._db.cameras_delete(camera_id)

    # —— AI 预览单例(D4)——
    async def enable_display(self, camera_id: str) -> None:
        """切换 AI 预览到指定路:旧路 stop_display,新路 start_display。

        全局同一时刻只 1 路预览推理 → 封号兜底成立。
        """
        if self._active_display_id == camera_id:
            return
        old = self._streams.get(self._active_display_id) if self._active_display_id else None
        if old is not None:
            old.stop_display()
        new = self._streams.get(camera_id)
        if new is not None:
            new.start_display()
        self._active_display_id = camera_id

    async def disable_display(self, camera_id: str) -> None:
        if self._active_display_id == camera_id:
            s = self._streams.get(camera_id)
            if s is not None:
                s.stop_display()
            self._active_display_id = None

    # —— 帧访问 ——
    def get_frame(self, camera_id: str) -> Any:
        s = self._streams.get(camera_id)
        return s.get_latest_frame() if s else None

    def get_recent_frames(self, camera_id: str, n: int = 3) -> list:
        s = self._streams.get(camera_id)
        return s.get_recent_frames(n) if s else []

    def mjpeg_generator(self, camera_id: str):
        s = self._streams.get(camera_id)
        if s is None:
            return iter([])
        return s.mjpeg_generator()

    def get_state(self, camera_id: str) -> dict:
        s = self._streams.get(camera_id)
        if s is None:
            return {"camera_id": camera_id, "online": False}
        return s.get_state()

    def list_cameras(self) -> list[dict]:
        """供工具注入:含 id/name/area/online。"""
        out = []
        for cid, s in self._streams.items():
            st = s.get_state()
            cfg = getattr(s, "_config", {}) or {}
            out.append({
                "id": cid,
                "name": cfg.get("name", cid),
                "area": cfg.get("area", ""),
                "online": bool(st.get("online", False)),
            })
        return out

    # —— 自动化/工具通道 ——
    def _on_automation_trigger(self, camera_id: str) -> None:
        """worker 线程回调:投递自动化评估到主循环。

        worker 检测到运动时同步调此方法(跨线程);run_coroutine_threadsafe
        把 request_automation_eval 投到主循环跑(与 camera_stream.py 推理投递
        同机制)。manager 须已注入 loop。
        """
        if self._loop is None or self._loop.is_closed():
            return
        frames = self.get_recent_frames(camera_id, 3)
        asyncio.run_coroutine_threadsafe(
            self.request_automation_eval(camera_id, frames), self._loop
        )

    async def request_automation_eval(self, camera_id: str, frames: list) -> None:
        """运动触发:拿自动化通道名额 → 跑该路规则评估。"""
        async with self._auto_sem:
            await self._eval_one(camera_id, frames)

    async def _eval_one(self, camera_id: str, frames: list) -> None:
        if self._automation_service is None:
            return
        try:
            await self._automation_service.evaluate(frames=frames, camera_id=camera_id)
        except Exception:  # noqa: BLE001
            logger.exception("automation eval failed for %s", camera_id)

    async def request_tool_inference(self, camera_id: str, prompt: str, frames: list) -> Any:
        """工具调用:共享自动化通道。"""
        async with self._auto_sem:
            if self._vision_service is None:
                return None
            return await self._vision_service.evaluate_condition(frames, prompt)

    def _on_camera_ip_changed(self, camera_id: str, new_ip: str) -> None:
        """discovery 回 IP:记日志。worker 掉线重连已自带(指数退避),
        IP 变更后下次 read 自然连新 IP;ptz per-camera 重连由 Task 5 PtzRegistry 处理。
        """
        logger.info("camera %s ip changed to %s, worker will reconnect", camera_id, new_ip)
