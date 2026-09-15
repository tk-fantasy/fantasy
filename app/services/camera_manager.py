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
from ..virtual_camera_stream import VirtualCameraStream

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
        # —— 插件虚拟摄像头（帧注入型，不入 cameras 表）——
        # plugin_id → 该插件注册的虚拟路信息 {camera_id, spec}
        self._virtual_cams: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # —— HA 分层路由（阶段2）：检测与触发外包给 HA ——
        # 挂了 ha_camera_entity/ha_motion_entity 任一的路都登记在 _ha_rows
        # camera_id -> row；反查索引用于事件总线回调定位摄像头
        self._ha_rows: dict[str, dict] = {}           # camera_id -> row（含混合路）
        self._ha_camera_entity_map: dict[str, str] = {}  # ha_camera_entity -> camera_id
        self._motion_entity_map: dict[str, str] = {}  # ha_motion_entity -> camera_id
        self._ha_availability: dict[str, bool] = {}   # camera_id -> HA 实体可用（事件+60s 兜底）
        self._unsub_events = None                     # 事件总线订阅取消函数
        self._avail_task: asyncio.Task | None = None  # 可用性兜底刷新循环
        if discovery_service is not None:
            discovery_service.set_on_ip_changed(self._on_camera_ip_changed)

    def __getattr__(self, name: str):
        """兼容单测 __new__ 绕过 __init__ 的轻量构造（本仓既定范式）。

        仅对 HA 分层路由的状态属性生效：首次访问时在实例上补空容器，
        不影响 __init__ 正常装配的实例，也未列名的属性照常抛 AttributeError。
        """
        if name in ("_ha_rows", "_ha_camera_entity_map", "_motion_entity_map",
                    "_ha_availability"):
            value: dict = {}
            object.__setattr__(self, name, value)
            return value
        if name in ("_unsub_events", "_avail_task"):
            object.__setattr__(self, name, None)
            return None
        raise AttributeError(
            f"{type(self).__name__!s} object has no attribute {name!r}")

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
        if not rows:
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
            # （HA-only 路无常驻解码器，_spawn 返回 None，跳过预览）
            if stream is not None and not display_activated and row.get("display_enabled", 1):
                stream.start_display()
                self._active_display_id = row["id"]
                display_activated = True
        # 首次播种 HA 路可用性（事件流入前的初值）+ 启动 60s 兜底刷新
        await self._refresh_ha_availability()
        if self._avail_task is None or self._avail_task.done():
            self._avail_task = asyncio.create_task(
                self._ha_availability_loop(), name="camera-ha-availability")

    async def _spawn(self, row: dict) -> CameraStream | None:
        """根据 cameras 行构造一路并启动 worker(抓帧 + 运动检测)。

        HA-only 路（挂 ha_camera_entity 且无 rtsp_url）不 spawn 常驻解码器：
        抓帧走 camera_proxy、在线随 HA 实体——空 rtsp/usb 配置的 CameraStream
        会去抓本机 /dev/video0，必须在这里拦下。返回 None 表示该路无 worker。
        """
        cid = row["id"]
        self._sync_ha_camera_row(row)
        if self._is_ha_only(row):
            logger.info(
                "camera %s bound to HA entity %s (no resident decoder)",
                cid, row.get("ha_camera_entity"))
            return None
        stream = CameraStream(
            camera_id=cid,
            config=row,
            vision_service=self._vision_service,
            on_automation_trigger=self._on_automation_trigger,
            discovery_service=self._discovery_service,
        )
        if self._loop is not None:
            stream.set_event_loop(self._loop)
        # 滑块落盘值(vision.motion_threshold)优先于 cameras 表列的初始值：
        # 滑块接口承诺「重启后保持」，而 worker 启动只读表列——不在此回放的话，
        # 调完滑块一重启就静默回退旧值（UI 显示新值、实际跑旧值）。
        # 配置里没有该键（从未用过滑块）时不动，尊重每路各自的表列值。
        from ..core.config import get_config as _get_config
        _vision_cfg = _get_config("vision", {}) or {}
        if "motion_threshold" in _vision_cfg:
            try:
                stream.set_motion_threshold(int(_vision_cfg["motion_threshold"]))
            except Exception:
                logger.exception("apply vision.motion_threshold to %s failed", cid)
        self._streams[cid] = stream
        stream.start()
        return stream

    # —— 插件虚拟摄像头（帧注入，真实链路）——
    #
    # 生命周期：插件进程 setup → camera.register（拿到确定性 camera_id）→
    # 周期 camera.push_frame 注入帧 → 插件停止/崩溃/禁用 → camera.unregister
    # （supervisor 的 on-stopped 回调兜底）。帧进入 VirtualCameraStream 后走
    # 与真实采集完全相同的管线：dhash 运动触发、规则评估、MJPEG 预览。
    # 不写 cameras 表——虚拟路只存在于运行时（重启 Aether 后插件重注册即恢复），
    # CRUD/持久化语义由插件自己的 state 承担。

    async def register_virtual_camera(self, plugin_id: str, spec: dict | None = None) -> dict:
        """注册一路虚拟摄像头。幂等：重复注册（插件重启）先注销旧路再建。

        spec: {name, display_enabled, motion_threshold, vision_min_infer_interval,
               vision_max_idle_interval, vision_use_img_count, frame_interval_ms,
               flags:{real_exec 等插件自定义运行时标志}}
        返回 {camera_id, name}——camera_id 由 plugin_id 确定性生成（vcam_<plugin_id>），
        重启不变，规则绑定的 camera_id 稳定。
        """
        spec = dict(spec or {})
        camera_id = f"vcam_{plugin_id}"
        if plugin_id in self._virtual_cams:
            await self.unregister_plugin_cameras(plugin_id)
        stream = VirtualCameraStream(
            camera_id=camera_id,
            config=spec,
            vision_service=self._vision_service,
            on_automation_trigger=self._on_automation_trigger,
        )
        if self._loop is not None:
            stream.set_event_loop(self._loop)
        self._streams[camera_id] = stream
        self._virtual_cams[plugin_id] = {
            "camera_id": camera_id,
            "spec": spec,
            "flags": dict(spec.get("flags") or {}),
        }
        stream.start()
        logger.info("Virtual camera registered: %s (plugin=%s)", camera_id, plugin_id)
        return {"camera_id": camera_id, "name": spec.get("name", camera_id)}

    async def unregister_plugin_cameras(self, plugin_id: str) -> bool:
        """注销某插件的全部虚拟路（插件停止/禁用/删除时调用）。"""
        info = self._virtual_cams.pop(plugin_id, None)
        if info is None:
            return False
        camera_id = info["camera_id"]
        stream = self._streams.pop(camera_id, None)
        if stream:
            try:
                stream.stop()
            except Exception:
                logger.exception("stop virtual stream %s failed", camera_id)
        if self._active_display_id == camera_id:
            self._active_display_id = None
        logger.info("Virtual camera unregistered: %s (plugin=%s)", camera_id, plugin_id)
        return True

    def push_frame(self, camera_id: str, jpeg_b64: str) -> dict:
        """向虚拟路注入一帧（JPEG base64 → imdecode → enqueue）。

        帧编码在插件侧完成（缩放/JPEG/压缩一次），宿主只解码入队，开销小。
        返回 {ok, dropped}——队满丢弃时 dropped=True（插件据此感知背压）。
        """
        import base64
        import cv2
        import numpy as np
        stream = self._streams.get(camera_id)
        if not isinstance(stream, VirtualCameraStream):
            return {"ok": False, "error": f"virtual camera not found: {camera_id}"}
        try:
            buf = np.frombuffer(base64.b64decode(jpeg_b64), dtype=np.uint8)
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"frame decode failed: {exc}"}
        if frame is None:
            return {"ok": False, "error": "frame decode returned None"}
        ok = stream.enqueue_frame(frame)
        return {"ok": True, "dropped": not ok}

    def _find_virtual_by_camera(self, camera_id: str) -> tuple[str, dict] | None:
        # getattr 容错：部分单测用 __new__ 绕过 __init__ 构造 manager，
        # 无 _virtual_cams 属性时按"无虚拟路"处理
        for pid, info in (getattr(self, "_virtual_cams", None) or {}).items():
            if info["camera_id"] == camera_id:
                return pid, info
        return None

    def is_virtual_camera(self, camera_id: str) -> bool:
        return self._find_virtual_by_camera(camera_id) is not None

    def set_virtual_flag(self, camera_id: str, key: str, value) -> bool:
        """设置某虚拟路的运行时标志（如 real_exec）。持久化由插件自己负责。"""
        found = self._find_virtual_by_camera(camera_id)
        if found is None:
            return False
        _, info = found
        info["flags"][key] = value
        return True

    def get_virtual_flag(self, camera_id: str, key: str, default=None):
        found = self._find_virtual_by_camera(camera_id)
        if found is None:
            return default
        return found[1]["flags"].get(key, default)

    def _virtual_rows(self) -> list[dict]:
        """虚拟路合成为 cameras 表完整行形状（cameras_all 合并用）。

        与 DB 行字段对齐（前端摄像头设置页直接消费这些字段），
        source_type='test' 标识来源；enabled/online 由 stream 状态决定。
        """
        rows = []
        for pid, info in self._virtual_cams.items():
            spec = info["spec"]
            cid = info["camera_id"]
            stream = self._streams.get(cid)
            st = stream.get_state() if stream else {}
            rows.append({
                "id": cid,
                "name": spec.get("name", "测试摄像头"),
                "enabled": 1,
                "sort_order": 9000 + len(rows),  # 排在真实摄像头之后
                "source_type": "test",
                "usb_index": None,
                "rtsp_url": "",
                "rtsp_username": "",
                "rtsp_password": "",  # nosec - 空字符串为未配置哨兵，非凭证
                "area": "",
                "device_mac": "",
                "discovery_enabled": 0,
                "ptz_enabled": 0,
                "display_enabled": int(spec.get("display_enabled", 1)),
                "ha_camera_entity": "",
                "ha_motion_entity": "",
                "plugin_id": pid,
                "online": bool(st.get("camera_opened", False)),
                "virtual": True,
            })
        return rows

    def set_event_loop(self, loop) -> None:
        self._loop = loop
        for s in self._streams.values():
            s.set_event_loop(loop)

    def stop(self) -> None:
        for s in self._streams.values():
            try:
                s.stop()
            except Exception:
                logger.exception("stop camera %s failed", getattr(s, "camera_id", "?"))
        if self._avail_task is not None:
            self._avail_task.cancel()
            self._avail_task = None
        if self._unsub_events is not None:
            self._unsub_events()
            self._unsub_events = None

    # —— HA 分层路由（阶段2）——

    @staticmethod
    def _is_ha_only(row: dict) -> bool:
        """挂了 ha_camera_entity 且无 rtsp_url：无本地取流需求的路。"""
        return bool(str(row.get("ha_camera_entity") or "").strip()
                    and not str(row.get("rtsp_url") or "").strip())

    def _sync_ha_camera_row(self, row: dict) -> None:
        """（重）登记/改绑一路的 HA 实体映射。实体字段可改，先清旧反查条目。"""
        cid = row["id"]
        prev = self._ha_rows.get(cid)
        if prev is not None:
            self._ha_camera_entity_map.pop(str(prev.get("ha_camera_entity") or ""), None)
            self._motion_entity_map.pop(str(prev.get("ha_motion_entity") or ""), None)
        cam_e = str(row.get("ha_camera_entity") or "").strip()
        motion_e = str(row.get("ha_motion_entity") or "").strip()
        if cam_e or motion_e:
            self._ha_rows[cid] = row
            if cam_e:
                self._ha_camera_entity_map[cam_e] = cid
                self._ha_availability.setdefault(cid, False)
        else:
            self._ha_rows.pop(cid, None)
            self._ha_availability.pop(cid, None)
        if motion_e:
            self._motion_entity_map[motion_e] = cid

    def _drop_ha_camera(self, camera_id: str) -> None:
        prev = self._ha_rows.pop(camera_id, None)
        if prev is not None:
            self._ha_camera_entity_map.pop(str(prev.get("ha_camera_entity") or ""), None)
            self._motion_entity_map.pop(str(prev.get("ha_motion_entity") or ""), None)
        self._ha_availability.pop(camera_id, None)

    def bind_event_service(self, des) -> None:
        """挂接 HA 事件总线：binary_sensor 运动触发 + camera 实体可用性。

        binary_sensor 只在真实翻转时收到事件（事件源已过滤 attribute-only），
        流量低；映射命中在回调内查表，摄像头增删改无需重订订阅。
        """
        if des is None or self._unsub_events is not None:
            return
        self._unsub_events = des.subscribe(
            self._on_ha_state_event, domains={"binary_sensor", "camera"},
            name="camera-manager")

    async def _on_ha_state_event(self, event: dict) -> None:
        entity_id = str(event.get("entity_id", ""))
        domain = str(event.get("domain", ""))
        value = str((event.get("new_state") or {}).get("state", ""))
        if domain == "binary_sensor":
            if value == "on" and entity_id in self._motion_entity_map:
                await self.on_ha_motion_trigger(entity_id)
        elif domain == "camera":
            cid = self._ha_camera_entity_map.get(entity_id)
            if cid is not None:
                self._ha_availability[cid] = value not in ("unavailable", "unknown", "")

    async def _refresh_ha_availability(self) -> None:
        """从 states 快照刷新 HA 路可用性（启动播种 + 60s 兜底，WS 事件掉线自愈）。"""
        if not self._ha_camera_entity_map or self._ha_service is None:
            return
        states = await self._ha_service.get_states_snapshot()
        by_id = {s.get("entity_id"): str(s.get("state", "")) for s in states}
        for entity_id, cid in self._ha_camera_entity_map.items():
            value = by_id.get(entity_id)
            if value is not None:
                self._ha_availability[cid] = value not in ("unavailable", "unknown")

    async def _ha_availability_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._refresh_ha_availability()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("ha availability refresh failed", exc_info=True)

    async def fetch_camera_proxy_frames(self, entity_id: str, n: int = 1) -> list:
        """经 HA /api/camera_proxy 连抓 n 帧（JPEG → ndarray）。失败返回 []。

        单帧分析一张快照就够；n>1 时帧间隔 0.25s 给画面留变化空间。
        """
        import cv2
        import numpy as np
        client = getattr(self._ha_service, "_client", None)
        if client is None or not hasattr(client, "camera_proxy"):
            return []
        frames = []
        for i in range(max(1, n)):
            try:
                jpeg = await client.camera_proxy(entity_id)
                img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    frames.append(img)
            except Exception:  # noqa: BLE001
                logger.warning("camera_proxy fetch failed for %s", entity_id, exc_info=True)
                break
            if i < n - 1:
                await asyncio.sleep(0.25)
        return frames

    async def fetch_camera_proxy_frames_for_camera(self, camera_id: str, n: int = 2) -> list:
        """按 camera_id 经 HA 抓帧（该路须挂 ha_camera_entity）。未挂/失败返回 []。

        供 vision_chat 聊天问图与规则评估取帧用——调用方不必自己查行取实体。
        """
        row = self._ha_rows.get(camera_id) or {}
        entity = str(row.get("ha_camera_entity") or "").strip()
        if not entity:
            return []
        return await self.fetch_camera_proxy_frames(entity, n)

    async def _fetch_eval_frames(self, camera_id: str) -> list:
        """评估取帧分层：挂 ha_camera_entity 优先 camera_proxy，失败/未挂回退常驻流。"""
        row = self._ha_rows.get(camera_id) or {}
        try:
            n = max(1, min(3, int(row.get("vision_use_img_count") or 3)))
        except (TypeError, ValueError):
            n = 3
        frames = await self.fetch_camera_proxy_frames_for_camera(camera_id, n)
        if frames:
            return frames
        # proxy 失败 → 常驻解码流兜底（混合路的 RTSP 还活着就继续服务）
        return self.get_recent_frames(camera_id, 3)

    async def on_ha_motion_trigger(self, motion_entity: str) -> None:
        """HA binary_sensor 运动事件路径：与本地 dHash 在同一节流闸与评估入口汇合。

        在事件总线回调（主循环协程）执行。双触发源（HA 事件 + 本地 dHash）
        共用 _last_trigger_at 漏桶——先到者放行，后到者在窗口内被丢弃，
        下游 request_automation_eval 完全一致，零改动。
        """
        camera_id = self._motion_entity_map.get(motion_entity)
        if camera_id is None:
            return
        now = time.time()
        if now - self._last_trigger_at.get(camera_id, 0.0) < self._min_trigger_interval:
            return
        self._last_trigger_at[camera_id] = now
        frames = await self._fetch_eval_frames(camera_id)
        if not frames:
            logger.warning(
                "HA motion trigger for %s: no frames (proxy and resident stream both empty)",
                camera_id)
            return
        await self.request_automation_eval(camera_id, frames)

    # —— CRUD(转发 DB + 增删 stream)——
    async def create_camera(self, data: dict) -> dict:
        import secrets
        data.setdefault("id", f"cam_{secrets.token_hex(3)}")
        await self._db.cameras_insert(data)
        if data.get("enabled", 1):
            await self._spawn(data)
        return data

    # 影响取流连接的字段：变了必须重建 worker 才生效(worker 构造时缓存这些值)。
    # ha_camera_entity/ha_motion_entity 不动 RTSP 连接，但决定「spawn 还是 HA-only」
    # 与实体映射表，重建路径统一处理最简单（混合路偶发多一次重连，可接受）。
    _STREAM_FIELDS = frozenset({
        "enabled", "source_type", "usb_index",
        "rtsp_url", "rtsp_username", "rtsp_password",
        "motion_hash_size", "motion_threshold", "motion_check_interval",
        "vision_min_infer_interval", "vision_max_idle_interval",
        "vision_use_img_count", "frame_interval_ms",
        "ha_camera_entity", "ha_motion_entity",
    })

    async def update_camera(self, camera_id: str, fields: dict) -> dict:
        row = await self._db.cameras_get(camera_id)
        if row is None:
            raise KeyError(camera_id)
        await self._db.cameras_update(camera_id, fields)
        # 只有流相关字段变了才重建(拆连接重开)。改名称/区域/PTZ/关注项等
        # 不碰连接——重连瞬间的 401 在部分 IPC 上会触发防爆破锁定,没必要不冒。
        touched_stream = any(
            f in self._STREAM_FIELDS and row.get(f) != fields.get(f) for f in fields
        )
        if touched_stream:
            row = await self._rebuild_stream(camera_id)
        else:
            row = await self._db.cameras_get(camera_id)
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
            except Exception:
                logger.exception("stop old stream %s failed", camera_id)
        row = await self._db.cameras_get(camera_id)
        if row and row.get("enabled", 1):
            await self._spawn(row)
        else:
            # 禁用/删除：HA 实体映射一并清掉（事件回调不再命中）
            self._drop_ha_camera(camera_id)
        return row

    async def delete_camera(self, camera_id: str) -> bool:
        old = self._streams.pop(camera_id, None)
        if old:
            try:
                old.stop()
            except Exception:
                logger.exception("stop stream %s on delete failed", camera_id)
        if self._active_display_id == camera_id:
            self._active_display_id = None
        self._drop_ha_camera(camera_id)
        return await self._db.cameras_delete(camera_id)

    # —— AI 预览单例(D4)——
    async def _persist_display(self, camera_id: str, value: int) -> None:
        """落库 display_enabled:虚拟路写运行时 spec(cameras_all 回显用,不入 DB),
        真实路写 cameras 表。失败只记日志,不影响预览切换。
        """
        try:
            found = self._find_virtual_by_camera(camera_id)
            if found is not None:
                found[1]["spec"]["display_enabled"] = value
            elif self._db is not None:
                await self._db.cameras_update(camera_id, {"display_enabled": value})
        except Exception:
            logger.exception("persist display_enabled failed (%s=%d)", camera_id, value)

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
        await self._persist_display(camera_id, 1)
        if old_id and old_id != camera_id:
            await self._persist_display(old_id, 0)

    async def disable_display(self, camera_id: str) -> None:
        if self._active_display_id == camera_id:
            s = self._streams.get(camera_id)
            if s is not None:
                s.stop_display()
            self._active_display_id = None
            await self._persist_display(camera_id, 0)

    def set_motion_threshold(self, threshold: int) -> None:
        """全局 dhash 阈值热更新:广播所有路(滑块无 camera_id,作用于全部)。

        落盘 vision.motion_threshold 会在 _spawn（含重启后的启动路径）回放，
        滑块值跨重启保持；cameras 表列 motion_threshold 只是每路初始默认。
        """
        for s in self._streams.values():
            try:
                s.set_motion_threshold(threshold)
            except Exception:
                logger.exception("set_motion_threshold failed for %s", getattr(s, "camera_id", "?"))

    def set_camera_vl_display_enabled(self, enabled: bool) -> None:
        """全局 AI 预览开关(automation.camera_vl_display_enabled 配置项)。

        enabled=True:优先恢复 off 之前的那一路(off 不清 _active_display_id);
        该路已删除/无记录时,回退激活第一个 display_enabled=1 路。
        enabled=False:停当前预览路,保留 _active_display_id 不清(on/off 来回切
        都作用于同一路,符合 D4「全局同一时刻只 1 路预览」)。
        与按路 enable_display/disable_display(切预览到指定路)语义不同——
        本方法是全局总开关,作用于 _active_display_id 那一路。
        """
        if enabled:
            if self._active_display_id is not None:
                s = self._streams.get(self._active_display_id)
                if s is not None:
                    s.start_display()
                    return
                self._active_display_id = None  # 路已被删,回退扫描
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
        if s is not None:
            return s.get_state()
        # HA-only 路（无常驻解码器）：camera_opened 语义映射为 HA 实体可用性，
        # 告警监控（alert_service 连续 2 拍离线报警/恢复补发）零改动即覆盖 HA 路。
        row = self._ha_rows.get(camera_id)
        if row is not None and self._is_ha_only(row):
            online = self._ha_availability.get(camera_id, False)
            return {
                "camera_id": camera_id, "online": online,
                "camera_opened": online, "source": "ha",
            }
        # stream 不存在（删除中/重建窗口）也补 camera_opened 键：调用方
        # （告警监控）用 st.get("camera_opened") 判在线，缺键同样按离线
        # 记拍，显式补上语义一致且不依赖 get() 的 None 隐式回退。
        return {"camera_id": camera_id, "online": False, "camera_opened": False}

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
        # HA-only 路无 stream，单独补录（在线随 HA 实体可用性）
        for cid, row in self._ha_rows.items():
            if cid in self._streams:
                continue  # 混合路已在上面
            st = self.get_state(cid)
            out.append({
                "id": cid,
                "name": row.get("name", cid),
                "area": row.get("area", ""),
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
            return self._virtual_rows()
        rows = await self._db.cameras_all()
        for row in rows:
            st = self.get_state(row["id"])
            row["online"] = bool(st.get("camera_opened", False)) if st else False
            row["virtual"] = False
        # 合并插件虚拟路（与真实路同形状，前端/规则/预览无差别消费）
        rows.extend(self._virtual_rows())
        return rows

    def primary_camera_id(self) -> str | None:
        """取主路:当前预览路优先,否则第一个 enabled 路。

        多路化后「无 camera_id 的默认路」语义统一收敛到这里。
        有预览路用预览路,否则取 list_cameras()[0]。
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
            # 管道拆分:运动触发只评 vision 规则;time/weather 由
            # AutomationAgent 非视觉兜底循环独立评估,不再顺带。
            await self._automation_service.evaluate(
                frames=frames, camera_id=camera_id, rule_types=("vision",)
            )
        except Exception:
            logger.exception("automation eval failed for %s", camera_id)

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
