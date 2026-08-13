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
import time
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
        # 多路 dhash 触发节流闸:per-camera 独立计时,与单摄 AutomationAgent.trigger_evaluate
        # 行为一致(复用 vision.min_infer_interval_seconds,默认 3s)。_auto_sem 只限同时运行数,
        # 不限触发频率;无此闸连续运动时 0-result 规则会被高频评估轰炸 + 协程堆积。
        self._last_trigger_at: dict[str, float] = {}
        self._min_trigger_interval = max(0.5, float(get_config("vision.min_infer_interval_seconds", 3.0)))
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
        # 全新安装(cameras 表空)且 legacy 迁移未兜底时,插一行默认 USB 保留
        # 即插即用体验。幂等:插了就不空。老用户已迁移(rows 非空)不触发。
        if self._db is not None and not rows:
            import secrets
            await self._db.cameras_insert({
                "id": f"cam_{secrets.token_hex(3)}",
                "name": "默认摄像头",
                "enabled": 1,
                "source_type": "usb",
                "usb_index": 0,
                "display_enabled": 1,
            })
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
        # 参数变了,重建该路让新配置生效
        row = await self._rebuild_stream(camera_id)
        return row  # type: ignore[return-value]

    async def _rebuild_stream(self, camera_id: str) -> dict | None:
        """重建该路 stream(读最新 DB 行):pop 旧 stream → stop → 按 enabled 重 spawn。

        复用于两处:
        - update_camera:配置变更后让新参数生效。
        - _on_camera_ip_changed:discovery 找回新 IP 写入 DB 后,让 worker 用最新
          rtsp_url。worker 在构造时缓存 rtsp_url、不回读 DB,必须重建才能切到新 IP。
        """
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
        落库 display_enabled:新路=1,旧路=0(D4 全局只 1 路)。前端 refetch 后
        拿到正确值,乐观更新不被覆盖回退。
        """
        if self._active_display_id == camera_id:
            return
        old_id = self._active_display_id
        old = self._streams.get(old_id) if old_id else None
        if old is not None:
            old.stop_display()
        new = self._streams.get(camera_id)
        if new is not None:
            new.start_display()
        self._active_display_id = camera_id
        if self._db is not None:
            try:
                await self._db.cameras_update(camera_id, {"display_enabled": 1})
                if old_id and old_id != camera_id:
                    await self._db.cameras_update(old_id, {"display_enabled": 0})
            except Exception:  # noqa: BLE001
                logger.exception("persist display_enabled failed (enable %s)", camera_id)

    async def disable_display(self, camera_id: str) -> None:
        if self._active_display_id == camera_id:
            s = self._streams.get(camera_id)
            if s is not None:
                s.stop_display()
            self._active_display_id = None
            if self._db is not None:
                try:
                    await self._db.cameras_update(camera_id, {"display_enabled": 0})
                except Exception:  # noqa: BLE001
                    logger.exception("persist display_enabled failed (disable %s)", camera_id)

    def set_motion_threshold(self, threshold: int) -> None:
        """全局 dhash 阈值热更新:广播所有路(滑块无 camera_id,作用于全部)。

        与各路 cameras 表 motion_threshold 列不冲突——表列是初始值,本方法是
        运行时滑块热更新,复用 vision.motion_threshold 落盘语义。
        """
        for s in self._streams.values():
            try:
                s.set_motion_threshold(threshold)
            except Exception:  # noqa: BLE001
                logger.exception("set_motion_threshold failed for %s", getattr(s, "camera_id", "?"))

    def set_camera_vl_display_enabled(self, enabled: bool) -> None:
        """全局 AI 预览开关(automation.camera_vl_display_enabled 配置项)。

        enabled=True:无预览路时激活第一个 display_enabled=1 路;
        enabled=False:停当前预览路(保留 _active_display_id 不清,on/off 来回切
        都作用于同一路,符合 D4「全局同一时刻只 1 路预览」)。
        与按路 enable_display/disable_display(切预览到指定路)语义不同——
        本方法是全局总开关,作用于 _active_display_id 那一路。
        """
        if enabled:
            if self._active_display_id is None:
                for cid, s in self._streams.items():
                    cfg = getattr(s, "_config", {}) or {}
                    if cfg.get("display_enabled", 1):
                        s.start_display()
                        self._active_display_id = cid
                        break
        else:
            if self._active_display_id is not None:
                s = self._streams.get(self._active_display_id)
                if s is not None:
                    s.stop_display()
                # 不清 _active_display_id:on 回来恢复同一路

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
                # CameraState 无 online 字段，只有 camera_opened。
                # 原 st.get("online") 恒 False（MCP 工具拿到永远离线），
                # 与 cameras_all() 对齐改用 camera_opened 推断。
                "online": bool(st.get("camera_opened", False)),
            })
        return out

    async def cameras_all(self) -> list[dict]:
        """查 cameras 表完整行 + 合并运行时 online(供前端管理页)。

        与 list_cameras()(4 字段轻量版,给 MCP 工具)的区别:返回完整行
        (含 display_enabled/enabled/source_type/rtsp_url 等),前端卡片与编辑
        弹窗需要。online 从 get_state 的 camera_opened 推断(CameraState 无
        online 字段,原 list_cameras 用 st.get("online") 永远 False 是 bug)。
        """
        if self._db is None:
            return []
        rows = await self._db.cameras_all()
        for row in rows:
            st = self.get_state(row["id"])
            row["online"] = bool(st.get("camera_opened", False)) if st else False
        return rows

    def primary_camera_id(self) -> str | None:
        """无参 /video_feed 取主路:当前预览路优先,否则第一个 enabled 路。

        供 mcp_routes.py 旧 /video_feed(无 camera_id)端点用——多路化后该端点
        需选定主路。有预览路用预览路,否则取 list_cameras()[0]。
        """
        if self._active_display_id:
            return self._active_display_id
        cams = self.list_cameras()
        return cams[0]["id"] if cams else None

    # —— 自动化/工具通道 ——
    def _on_automation_trigger(self, camera_id: str) -> None:
        """worker 线程回调:投递自动化评估到主循环。

        per-camera 节流:同一路距上次触发不足 _min_trigger_interval 直接丢弃,
        与单摄 AutomationAgent.trigger_evaluate 一致(防 0-result 规则被连续运动轰炸)。
        跨线程漏桶节流,read-modify-write 不加锁——偶尔多放一个无碍,与单摄路径同等语义。

        worker 检测到运动时同步调此方法(跨线程);run_coroutine_threadsafe
        把 request_automation_eval 投到主循环跑(与 camera_stream.py 推理投递
        同机制)。manager 须已注入 loop。
        """
        now = time.time()
        if now - self._last_trigger_at.get(camera_id, 0.0) < self._min_trigger_interval:
            return  # 节流窗口内,丢弃(与单摄 trigger_evaluate 一致)
        self._last_trigger_at[camera_id] = now
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
        """discovery 回新 IP:重建该路 stream 让 worker 用最新 rtsp_url。

        之前只打日志(误以为 worker 会自然连新 IP),但 worker 在构造时缓存了
        rtsp_url、IP 变更后不回读 DB,必须重建 stream 才能让新 URL 生效。此回调
        由 async apply_found_ip 在 loop 线程上 sync 调用,用 run_coroutine_threadsafe
        调度重建协程(fire-and-forget,无需等结果)。PTZ per-camera 重连由 PtzRegistry
        懒重连处理。
        """
        logger.info("camera %s ip changed to %s, rebuilding stream", camera_id, new_ip)
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("cannot rebuild stream %s: event loop unavailable", camera_id)
            return
        asyncio.run_coroutine_threadsafe(self._rebuild_stream(camera_id), loop)
