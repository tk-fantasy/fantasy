from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Callable

import cv2
import numpy as np

from .core.config import get_config
from .services.motion_service import MotionDetector
from .services.priority_service import interactive_priority
from .services.vision_service import VisionService
from .vision import ActionResult


logger = logging.getLogger(__name__)

# 最小有效 JPEG（1x1 灰色像素），用作 MJPEG 流的 keepalive
_TINY_JPEG = cv2.imencode('.jpg', np.full((1, 1, 3), 128, dtype=np.uint8))[1].tobytes()


@dataclass
class CameraState:
    camera_opened: bool = False
    backend_name: str = "unknown"
    frame_width: int = 0
    frame_height: int = 0
    fps: float = 0.0
    worker_fps: float = 0.0
    last_frame_at: float = 0.0
    last_error: str | None = None
    action: str = "idle"
    feedback: str = "等待识别。"
    details: dict | None = None
    confirmed: bool = False
    model_fps: float = 3.0
    motion_distance: int = -1
    motion_threshold: int = 5
    last_infer_at: float = 0.0
    infer_count: int = 0
    infer_busy: bool = False


class CameraStream:
    """摄像头采集 + 运动门控视觉推理。

    采集线程全速跑(MJPEG 预览不卡),但视觉模型只在三种情况被调用:
    1. 画面相对上次推理的参考帧发生运动(dHash 距离超阈值)且距上次推理
       超过 min_infer_interval;
    2. 太久没推理(max_idle_interval 心跳兜底,防止状态彻底过期);
    3. 按需:用户聊天问图时由 vision_chat 路径直接取帧调用。
    用户交互(聊天/问图)期间后台推理主动让位,避免本地 LLM 串行队列
    把用户请求堵在后面。
    """

    def __init__(self, camera_index: int | None = None, vision_service: "VisionService | None" = None) -> None:
        # 摄像头设备号：优先构造参数，其次 config，默认 0
        if camera_index is None:
            camera_index = int(get_config("vision.camera_index", 0))
        self._camera_index = camera_index
        self._recognizer = vision_service or VisionService()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._cap: cv2.VideoCapture | None = None
        self._latest_frame: np.ndarray | None = None
        self._latest_jpeg: bytes | None = None
        self._latest_result = ActionResult("idle", "等待识别。", {"source": "vision", "enabled": self._recognizer.enabled})
        self._infer_busy = False
        self._presence_count = 0
        self._absence_count = 0
        self._presence_threshold = 3

        self._motion = MotionDetector(
            hash_size=int(get_config("vision.motion_hash_size", 16)),
            threshold=int(get_config("vision.motion_threshold", 15)),
        )
        self._motion_check_interval = max(0.05, float(get_config("vision.motion_check_interval_seconds", 0.2)))
        self._min_infer_interval = max(0.5, float(get_config("vision.min_infer_interval_seconds", 3.0)))
        self._max_idle_interval = max(self._min_infer_interval, float(get_config("vision.max_idle_interval_seconds", 60.0)))
        self._last_motion_check = 0.0
        self._last_model_run_at = 0.0
        self._infer_count = 0
        self._infer_started_at: float = 0.0
        self._infer_timeout = max(5.0, float(get_config("vision.infer_timeout_seconds", 45.0)))

        # —— 摄像头连接健壮性参数 ——
        # 记住上次成功打开的 backend，重连时优先复用，省去对 4 个 backend 的全量预热探测
        self._last_success_backend: tuple[str, int, int] | None = None
        # 连续打开失败计数，用于指数退避（避免狂开刷屏，给驱动时间恢复）
        self._consecutive_open_failures = 0
        # 读帧失败时的就地重试次数与间隔（吸收瞬时掉帧，不立即拆设备）
        self._read_retry_count = max(0, int(get_config("vision.read_retry_count", 3)))
        self._read_retry_interval = max(0.02, float(get_config("vision.read_retry_interval_seconds", 0.1)))
        # 释放设备后的冷却时间（Windows 驱动回收句柄需要时间，立即重开会 busy）
        self._release_cooldown = max(0.1, float(get_config("vision.release_cooldown_seconds", 0.8)))
        # 指数退避上限
        self._max_backoff = max(self._release_cooldown, float(get_config("vision.max_backoff_seconds", 15.0)))

        # 慢读降级检测：DSHOW 捕获图损坏后会持续返回 ok=True 但 read≈1000ms，
        # 上面的恢复逻辑只在 ok=False 时拆设备，慢读会永久卡死在 1fps。
        # 连续慢读达到阈值即强制 release+reopen 重建捕获图。
        self._slow_read_streak = 0
        self._slow_read_ms = max(200.0, float(get_config("vision.slow_read_ms_threshold", 500.0)))
        # 降级 read≈1000ms（正常 32ms 的 30 倍），差距极大，连续 3 次即可确诊，
        # 无需等 5 次（5 秒卡顿太久）。3 次≈3 秒恢复。
        self._slow_read_threshold = max(2, int(get_config("vision.slow_read_reopen_threshold", 3)))

        # backend 临时黑名单：慢读重连时把刚降级的 backend 拉黑一段 TTL，
        # _build_candidates 在 TTL 内跳过它，避免重连后又优先选中同一个损坏的
        # 捕获图、反复慢读重连（~18s），直接切到可用 backend（~2s）。TTL 到期后
        # 可再次尝试（DSHOW 捕获图损坏往往是临时的）。
        self._backend_blacklist: dict[str, float] = {}
        self._backend_blacklist_ttl = max(5.0, float(get_config("vision.backend_blacklist_seconds", 30.0)))

        # 规则引擎用的多帧环形缓冲:按 frame_interval_ms 间隔存最近 N 帧,
        # 供条件评估做时间序列理解(如“正在坐下”)。推理完成回调降低规则响应延迟。
        self._frame_count = max(1, int(get_config("vision.vision_use_img_count", 3)))
        self._frame_interval_ms = max(0, int(get_config("vision.frame_interval_ms", 1000)))
        self._frame_buffer: deque[np.ndarray] = deque(maxlen=self._frame_count)
        self._frame_timestamps: deque[float] = deque(maxlen=self._frame_count)
        self._last_buffer_push = 0.0
                
        # 异步帧缓冲队列：避免 frame.copy() 阻塞主循环
        self._buffer_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=10)
        self._buffer_thread: threading.Thread | None = None
        
        # 异步推理调度队列：避免 frame.copy() 阻塞主循环
        self._infer_queue: queue.Queue[tuple[np.ndarray, str]] = queue.Queue(maxsize=5)
        self._infer_scheduler_thread: threading.Thread | None = None
        self._infer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="infer")
                
        self._on_inference_done: Callable[[], None] | None = None
        # 主事件循环引用：运动推理通过 run_coroutine_threadsafe 投到主循环跑，
        # httpx 网络等待时释放 GIL，不再像线程池那样抢 GIL 饿死采集线程。
        # 由 set_event_loop 在应用启动时注入（主循环已运行后）。
        self._loop: asyncio.AbstractEventLoop | None = None
        self._infer_futures: list = []  # 跟踪未完成的推理 future，stop 时取消

        self._state = CameraState(
            details={"source": "vision", "enabled": self._recognizer.enabled},
            motion_threshold=self._motion.threshold,
            model_fps=round(1.0 / self._min_infer_interval, 2),
        )

    def start(self) -> None:
        if self._running:
            return
        logger.info(
            "Starting camera stream",
            extra={
                "camera_index": self._camera_index,
                "model": self._recognizer.model,
                "llm_enabled": self._recognizer.enabled,
                "min_infer_interval": self._min_infer_interval,
                "max_idle_interval": self._max_idle_interval,
                "motion_threshold": self._motion.threshold,
            },
        )
        self._running = True
        # 启动异步帧缓冲线程
        self._start_buffer_thread()
        # 启动异步推理调度线程
        self._start_infer_scheduler()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        # 清空队列，避免停止后仍处理残留帧
        while not self._buffer_queue.empty():
            try:
                self._buffer_queue.get_nowait()
            except queue.Empty:
                break
        while not self._infer_queue.empty():
            try:
                self._infer_queue.get_nowait()
            except queue.Empty:
                break
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        if self._buffer_thread and self._buffer_thread.is_alive():
            self._buffer_thread.join(timeout=2)
        if self._infer_scheduler_thread and self._infer_scheduler_thread.is_alive():
            self._infer_scheduler_thread.join(timeout=2)
        # 取消尚未开始的推理 future（已在跑的无法取消，靠超时安全阀兜底）
        for fut in list(self._infer_futures):
            if not fut.done():
                fut.cancel()
        self._infer_futures.clear()
        self._infer_executor.shutdown(wait=True, cancel_futures=True)
        self._thread = None
        self._buffer_thread = None
        self._infer_scheduler_thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._clear_cached_frame("摄像头已停止")
        logger.info("Stopped camera stream")

    def _mark_camera_closed(self, error_message: str | None = None, keep_cache: bool = False) -> None:
        """标记摄像头不可用。

        keep_cache=True 时保留最近一帧，供前端在短暂中断时继续显示，
        避免画面因瞬时掉帧立即闪烁报错。
        """
        with self._lock:
            self._state.camera_opened = False
            self._state.frame_width = 0
            self._state.frame_height = 0
            self._state.fps = 0.0
            self._state.last_frame_at = 0.0
            self._state.last_error = error_message
            if not keep_cache:
                self._latest_frame = None
                self._latest_jpeg = None

    def _clear_cached_frame(self, error_message: str | None = None) -> None:
        self._mark_camera_closed(error_message, keep_cache=False)

    def get_state(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def get_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg

    def get_latest_frame(self) -> np.ndarray | None:
        """按需问图取当前原始帧(拷贝)。"""
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def get_recent_frames(self, count: int | None = None) -> list[np.ndarray]:
        """取最近 N 帧(拷贝列表),供规则引擎做时间序列条件评估。默认取全部缓冲。"""
        with self._lock:
            frames = list(self._frame_buffer)
        if count is not None and len(frames) > count:
            frames = frames[-count:]
        return [f.copy() for f in frames]

    def _start_buffer_thread(self) -> None:
        """启动独立的帧缓冲更新线程，避免阻塞视频流主循环。"""
        self._buffer_thread = threading.Thread(target=self._buffer_worker, daemon=True)
        self._buffer_thread.start()
        logger.info("Buffer update thread started")

    def _start_infer_scheduler(self) -> None:
        """启动独立的推理调度线程，处理帧拷贝和推理启动。"""
        self._infer_scheduler_thread = threading.Thread(target=self._infer_scheduler_worker, daemon=True)
        self._infer_scheduler_thread.start()
        logger.info("Inference scheduler thread started")

    def _buffer_worker(self) -> None:
        """独立线程处理帧缓冲更新，从队列中取帧并异步写入缓冲。"""
        while self._running:
            try:
                frame = self._buffer_queue.get(timeout=0.1)
                with self._lock:
                    self._frame_buffer.append(frame.copy())
                    self._frame_timestamps.append(time.time())
            except queue.Empty:
                continue

    def _infer_scheduler_worker(self) -> None:
        """独立线程处理推理调度：从队列取帧、降分辨率、拷贝、启动推理线程。"""
        while self._running:
            try:
                frame, trigger = self._infer_queue.get(timeout=0.1)
                if self._loop is None or self._loop.is_closed():
                    logger.warning("No event loop injected, falling back to thread executor (trigger=%s)", trigger)
                    self._infer_executor.submit(self._run_inference, frame)
                    continue
                # cv2.resize 持 GIL 但 224x224 降采样 <1ms，可忽略。
                # 真正的 GIL 瓶颈（encode_frame_b64 的 imencode+base64）在客户端里已
                # 用 asyncio.to_thread 包住。httpx 网络等待也释放 GIL。
                # run_coroutine_threadsafe 把推理协程投到主事件循环跑，
                # 与 10s tick 的 evaluate() 走完全相同的释放-GIL 路径。
                future = asyncio.run_coroutine_threadsafe(
                    self._run_inference_async(frame), self._loop
                )
                self._infer_futures.append(future)
                future.add_done_callback(lambda f: self._infer_futures.remove(f) if f in self._infer_futures else None)
                logger.debug("Inference scheduled: trigger=%s", trigger)
            except queue.Empty:
                continue
            except Exception:
                logger.exception("Inference scheduler error")
                # 异常时重置 _infer_busy，避免推理永久卡死
                with self._lock:
                    self._infer_busy = False

    def set_on_inference_done(self, callback: Callable[[], None]) -> None:
        """注册视觉推理完成回调,用于触发规则评估(降低响应延迟)。"""
        self._on_inference_done = callback

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """注入主事件循环，运动推理通过 run_coroutine_threadsafe 投到主循环跑。

        必须在主循环已运行后调用（如 lifespan startup 里）。
        """
        self._loop = loop

    def mjpeg_generator(self):
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        last_jpeg = None
        keepalive_counter = 0
        while self._running:
            frame = self.get_jpeg()
            if frame is None:
                # 没有新帧时，每 100 次循环（约 5 秒）发一个 1x1 空白 JPEG 作为 keepalive
                keepalive_counter += 1
                if keepalive_counter >= 100:
                    # 最小有效 JPEG（1x1 灰色像素）
                    yield boundary + _TINY_JPEG + b"\r\n"
                    keepalive_counter = 0
                time.sleep(0.05)
                continue
            # 只在帧变化时发送，避免重复发送相同帧
            if frame != last_jpeg:
                yield boundary + frame + b"\r\n"
                last_jpeg = frame
                keepalive_counter = 0
            # 无论帧是否变化都要 sleep 让出 GIL：
            # 否则当 read 偶发变慢、帧长时间不变时，本循环会忙等自旋
            # 疯狂调 get_jpeg()+持 GIL，把摄像头读帧线程饿死（FPS 崩到 ~1）。
            time.sleep(1 / 24)  # 限制到 24fps

    def _blacklist_backend(self, backend_name: str) -> None:
        """把 backend 临时拉黑，_build_candidates 在 TTL 内跳过它。

        DSHOW 捕获图损坏后短期内重开仍降级，拉黑强制切到其他 backend，把反复
        慢读重连（~18s）压到一次切换（~2s）。invalid/unavailable 名忽略。
        """
        if not backend_name or backend_name == "unavailable":
            return
        self._backend_blacklist[backend_name] = time.time() + self._backend_blacklist_ttl
        logger.info("Backend %s blacklisted for %.0fs", backend_name, self._backend_blacklist_ttl)

    def _build_candidates(self) -> list[tuple[str, int, int]]:
        """构建 backend 候选列表。

        上次成功打开的组合排最前，重连时优先复用，省去对 4 个 backend 的
        全量预热探测（每个 backend 预热读要约 1 秒，全扫要 4 秒）。

        刚因慢读降级被拉黑的 backend 会被跳过（TTL 内不重试损坏的捕获图）。
        全部在黑名单内则忽略黑名单，保证总有候选可试。
        """
        full = [
            ("dshow:0", 0, cv2.CAP_DSHOW),
            ("dshow:1", 1, cv2.CAP_DSHOW),
            ("msmf:0", 0, cv2.CAP_MSMF),
            ("msmf:1", 1, cv2.CAP_MSMF),
        ]
        # 顺带清理过期黑名单项，避免字典无限增长
        now = time.time()
        expired = [k for k, exp in self._backend_blacklist.items() if exp <= now]
        for k in expired:
            del self._backend_blacklist[k]
        alive = [c for c in full if c[0] not in self._backend_blacklist]
        if not alive:
            alive = list(full)
        if self._last_success_backend is not None and self._last_success_backend in alive:
            alive.remove(self._last_success_backend)
            return [self._last_success_backend, *alive]
        return alive

    def _open_camera(self) -> cv2.VideoCapture:
        # 网络流优先：配置了 rtsp_url 就走 WiFi/IP 摄像头，绕开 USB+DSHOW 那套
        # （持 GIL 的 read、驱动句柄 2 分钟回收、捕获图粘性损坏等问题在 RTSP 下不存在）。
        rtsp_url = self._resolve_rtsp_url()
        if rtsp_url:
            return self._open_network_stream(rtsp_url)
        for backend_name, camera_index, backend in self._build_candidates():
            logger.info("Trying camera backend: %s (index=%d)", backend_name, camera_index)
            cap = cv2.VideoCapture(camera_index, backend)
            if not cap.isOpened():
                cap.release()
                logger.warning("Failed to open camera backend: %s (index=%d)", backend_name, camera_index)
                continue
            # 设置较低的分辨率以提高帧率
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
            # 显式协商 30fps：DSHOW 在帧率未知时会回退到 ~1s/帧的阻塞等待，
            # 表现为 cap.get(CAP_PROP_FPS)==0 且 read 持续 1000ms。
            cap.set(cv2.CAP_PROP_FPS, 30)
            # 只缓冲 1 帧：read 总是返回最新帧，避免推理持 GIL 期间 worker
            # imencode 跟不上导致 OpenCV 内部缓冲积压、read 陷入阻塞模式。
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            time.sleep(0.3)
            valid_frame = None
            for _ in range(12):
                _t_probe = time.time()
                ok, frame = cap.read()
                _probe_ms = (time.time() - _t_probe) * 1000
                if ok and frame is not None:
                    # 预热读到帧但耗时超阈值：捕获图已降级（DSHOW 损坏图典型 read≈1000ms）。
                    # 不能选这个 backend，否则重连后立刻又慢读卡死、反复重连拖到 18 秒。
                    # 拉黑它并跳到下一个候选。
                    if _probe_ms > self._slow_read_ms:
                        logger.warning(
                            "Backend %s warmup read slow (%.0fms), skipping degraded capture graph",
                            backend_name, _probe_ms,
                        )
                        self._blacklist_backend(backend_name)
                        valid_frame = None
                        break
                    valid_frame = frame
                    break
                time.sleep(0.08)
            if valid_frame is not None:
                self._camera_index = camera_index
                # 记住成功的 backend，下次重连优先复用
                self._last_success_backend = (backend_name, camera_index, backend)
                with self._lock:
                    self._state.backend_name = backend_name
                logger.info(
                    "Camera backend ready: %s (index=%d, frame=%dx%d)",
                    backend_name, camera_index,
                    int(valid_frame.shape[1]), int(valid_frame.shape[0]),
                )
                return cap
            cap.release()
            logger.warning("Camera backend produced no valid frame: %s (index=%d)", backend_name, camera_index)
        with self._lock:
            self._state.backend_name = "unavailable"
        return cv2.VideoCapture()

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """脱敏 URL 中的用户名密码，避免凭证泄漏到日志。"""
        import re
        return re.sub(r"(://[^:]+):[^@]+@", r"\1:***@", url)

    def _resolve_rtsp_url(self) -> str:
        """从 config + .env 拼出完整 RTSP URL（含鉴权）。

        config.json 只存不带凭证的 base url + 用户名 + 密码的 env 变量名，
        密码本身在 .env 里（不进 git）。这样 config.json 即使被分享/提交
        也不会泄漏摄像头密码。
        """
        base = str(get_config("vision.rtsp_url", "")).strip()
        if not base:
            return ""
        user = str(get_config("vision.rtsp_username", "")).strip()
        pwd_env = str(get_config("vision.rtsp_password_env", "")).strip()
        pwd = os.getenv(pwd_env, "") if pwd_env else ""
        if not user or not pwd:
            # 没配凭证就裸连（部分摄像头 RTSP 不要求鉴权）
            return base
        # 把 rtsp://host/path → rtsp://user:pwd@host/path
        if "://" not in base:
            return base
        scheme, rest = base.split("://", 1)
        return f"{scheme}://{user}:{pwd}@{rest}"

    def _open_network_stream(self, url: str) -> cv2.VideoCapture:
        """打开 RTSP/HTTP 网络流（WiFi/IP 摄像头）。

        网络流和本地 USB 摄像头有本质区别：
        - read 是网络 I/O（recv），等待时释放 GIL，不会像 DSHOW 那样持 GIL 阻塞 1000ms
        - 断流是即时 EOF（ok=False），不是粘性的 isOpened()==True 假死
        - 重连只需重新发请求，秒级恢复，不依赖 Windows 驱动回收句柄
        所以这里不需要 backend 候选/黑名单/慢读降级那套复杂逻辑。
        """
        backend_name = "rtsp"
        safe_url = self._sanitize_url(url)
        logger.info("Opening network stream: %s", safe_url)
        # 强制 RTSP 媒体流走 TCP。默认 UDP RTP 在 Docker 桥��网络下会被 NAT 丢弃，
        # 表现为信令端口 554 通但 30s 拿不到一帧。TCP 牺牲少许延迟换可靠性，
        # 对视觉推理场景完全可接受。可通过 vision.rtsp_transport 改回 udp。
        transport = str(get_config("vision.rtsp_transport", "tcp")).strip().lower() or "tcp"
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{transport}"
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            logger.warning("Failed to open network stream: %s", safe_url)
            with self._lock:
                self._state.backend_name = "unavailable"
            return cv2.VideoCapture()
        # 缓冲 1 帧：始终取最新帧，避免推理期间积压
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        time.sleep(0.3)
        # 预热读：RTSP 首帧可能慢（握手+I 帧），多试几次
        valid_frame = None
        for _ in range(15):
            ok, frame = cap.read()
            if ok and frame is not None:
                valid_frame = frame
                break
            time.sleep(0.1)
        if valid_frame is not None:
            with self._lock:
                self._state.backend_name = backend_name
            logger.info(
                "Network stream ready: %s (frame=%dx%d)",
                safe_url, int(valid_frame.shape[1]), int(valid_frame.shape[0]),
            )
            return cap
        cap.release()
        logger.warning("Network stream produced no valid frame: %s", safe_url)
        with self._lock:
            self._state.backend_name = "unavailable"
        return cv2.VideoCapture()

    def _worker(self) -> None:
        frame_count = 0
        last_log_time = time.time()
        while self._running:
            try:
                if self._cap is None or not self._cap.isOpened():
                    self._cap = self._open_camera()
                    if not self._cap.isOpened():
                        # 指数退避：连续失败时拉长重开间隔（封顶 _max_backoff），
                        # 给 Windows 驱动时间回收句柄，避免狂开刷屏
                        self._consecutive_open_failures += 1
                        backoff = min(
                            self._release_cooldown * (2 ** min(self._consecutive_open_failures - 1, 4)),
                            self._max_backoff,
                        )
                        # 保留最后一帧给前端宽限期，不立即清空画面
                        self._mark_camera_closed("无法打开电脑摄像头", keep_cache=True)
                        logger.error(
                            "Unable to open camera, retrying in %.1fs (attempt %d)",
                            backoff, self._consecutive_open_failures,
                        )
                        time.sleep(backoff)
                        continue
                    # 成功打开，重置失败计数
                    self._consecutive_open_failures = 0

                _t_read = time.time()
                ok, frame = self._cap.read()
                _read_ms = (time.time() - _t_read) * 1000
                # 慢读降级检测：DSHOW 捕获图损坏会持续返回 ok=True 但 read≈1000ms，
                # 现有恢复逻辑只在 ok=False 时拆设备，慢读会永久卡死在 1fps。
                # 连续慢读达到阈值即强制 release+reopen 重建捕获图。
                if ok and frame is not None and _read_ms > self._slow_read_ms:
                    self._slow_read_streak += 1
                    logger.debug("Slow cap.read: %.0fms (streak=%d)", _read_ms, self._slow_read_streak)
                    if self._slow_read_streak >= self._slow_read_threshold:
                        logger.warning(
                            "Slow cap.read %.0fms for %d consecutive frames, reopening camera",
                            _read_ms, self._slow_read_streak,
                        )
                        # 拉黑刚降级的 backend，重连时跳过它直接切到可用 backend，
                        # 否则会优先重选同一个损坏捕获图、反复慢读重连（~18s）。
                        self._blacklist_backend(self._state.backend_name)
                        self._slow_read_streak = 0
                        if self._cap is not None:
                            self._cap.release()
                        self._cap = None
                        time.sleep(self._release_cooldown)
                        self._mark_camera_closed("摄像头卡顿，正在重连", keep_cache=True)
                        continue
                else:
                    # 读到正常帧（快或失败由下面分支处理），重置慢读计数
                    self._slow_read_streak = 0
                if not ok or frame is None:
                    # 瞬时掉帧：先就地重试几次，不立即拆设备（不清缓存、不释放）
                    recovered = False
                    for _ in range(self._read_retry_count):
                        if not self._running:
                            break
                        time.sleep(self._read_retry_interval)
                        ok, frame = self._cap.read()
                        if ok and frame is not None:
                            recovered = True
                            break
                    if not recovered:
                        # 多次重试仍失败，才释放设备并重新打开
                        logger.warning(
                            "Failed to read camera frame after %d retries, releasing device",
                            self._read_retry_count,
                        )
                        if self._cap is not None:
                            self._cap.release()
                        self._cap = None
                        # 释放后冷却，给 Windows 驱动时间回收句柄再重开
                        time.sleep(self._release_cooldown)
                        # 保留最后一帧给前端宽限期
                        self._mark_camera_closed("读取摄像头画面失败", keep_cache=True)
                        continue
                    logger.debug("Recovered after read retry")
                    # recovered：落到下面的正常处理流程

                self._maybe_schedule_inference(frame)
                with self._lock:
                    result = self._latest_result
                display_result = self._resolve_display_result(result)
                display_frame = self._prepare_display_frame(frame)
                # 从 config 读取 JPEG 质量，默认 50（降低以提高帧率）
                jpeg_quality = int(get_config("vision.jpeg_quality", 50))
                encoded, jpeg = cv2.imencode(".jpg", display_frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
                if not encoded:
                    logger.warning("Failed to encode preview frame")
                    continue

                with self._lock:
                    self._latest_frame = frame
                    self._latest_jpeg = jpeg.tobytes()
                    # 按 frame_interval_ms 间隔把帧存进环形缓冲(避免缓冲全是相邻同帧)
                    now_ms = time.time() * 1000
                    if not self._frame_timestamps or (now_ms - self._last_buffer_push) >= self._frame_interval_ms:
                        try:
                            # 异步入队，不阻塞主循环
                            self._buffer_queue.put_nowait(frame)
                            self._last_buffer_push = now_ms
                        except queue.Full:
                            pass  # 丢弃旧帧，保持最新
                    self._state.camera_opened = True
                    self._state.frame_width = int(frame.shape[1])
                    self._state.frame_height = int(frame.shape[0])
                    self._state.fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
                    self._state.last_frame_at = time.time()
                    self._state.last_error = None
                    self._state.action = display_result.action
                    self._state.feedback = display_result.feedback
                    self._state.details = display_result.details
                    self._state.confirmed = display_result.action not in {"idle", "no_event", "waiting_confirm"}
                    self._state.last_infer_at = self._last_model_run_at
                    self._state.infer_count = self._infer_count
                    self._state.infer_busy = self._infer_busy
                
                # 每10秒记录一次帧率，用于诊断"循环播放几帧"问题
                frame_count += 1
                now = time.time()
                if now - last_log_time >= 10.0:
                    fps = frame_count / (now - last_log_time)
                    self._state.worker_fps = round(fps, 1)
                    logger.info("Camera worker FPS: %.1f (frames=%d, infer_busy=%s)",
                               fps, frame_count, self._infer_busy)
                    frame_count = 0
                    last_log_time = now

                # 动态调整 sleep：如果帧处理已经很快，sleep 到 24fps；否则不 sleep
                frame_time = time.time() - now
                target_frame_time = 1 / 30  # 目标 30fps
                if frame_time < target_frame_time:
                    time.sleep(target_frame_time - frame_time)
            except Exception:
                logger.exception("Camera worker crashed")
                self._clear_cached_frame("摄像头线程异常")
                time.sleep(0.5)

    def _maybe_schedule_inference(self, frame: np.ndarray) -> None:
        """运动门控:有运动或太久没更新才把帧送给视觉模型。"""
        if not self._recognizer.enabled:
            return
        now = time.time()
        if now - self._last_motion_check < self._motion_check_interval:
            return
        self._last_motion_check = now

        moved, distance = self._motion.assess(frame)

        # 判断 + 赋值：必须在同一个锁内，保证原子性
        with self._lock:
            self._state.motion_distance = distance
            if self._infer_busy:
                # 安全阀：推理线程可能 hang（LLM 超时未生效），超时后强制重置标记
                if self._infer_started_at > 0 and (time.time() - self._infer_started_at) > self._infer_timeout:
                    logger.warning("Inference timed out after %.1fs, resetting _infer_busy",
                                   time.time() - self._infer_started_at)
                    self._infer_busy = False
                    self._infer_started_at = 0.0
                else:
                    return
            if interactive_priority.active():
                return
            since_last = now - self._last_model_run_at
            if moved and since_last >= self._min_infer_interval:
                trigger = "motion"
            elif since_last >= self._max_idle_interval:
                trigger = "heartbeat"
            else:
                return
            self._infer_busy = True
            self._last_model_run_at = now
            self._infer_count += 1

        # 锁外：有开销或涉及其他锁的操作
        self._motion.commit_reference()
        try:
            # 异步入队，不阻塞主循环（帧拷贝和推理启动在独立线程中执行）
            self._infer_queue.put_nowait((frame, trigger))
        except queue.Full:
            # 入队失败时必须重置 _infer_busy，否则标记永久卡死，后续推理全部跳过
            with self._lock:
                self._infer_busy = False
            logger.warning("Inference queue full, dropping frame (trigger=%s)", trigger)

    def _run_inference(self, frame: np.ndarray) -> None:
        started = time.time()
        with self._lock:
            self._infer_started_at = started
        try:
            result = self._recognizer.classify_frame(frame)
            logger.info(
                "Inference result updated",
                extra={
                    "action": result.action,
                    "feedback": result.feedback,
                    "elapsed_seconds": round(time.time() - started, 2),
                },
            )
            with self._lock:
                self._latest_result = result
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inference failed")
            with self._lock:
                self._latest_result = ActionResult("idle", f"模型识别失败: {exc}", {"source": "vision", "enabled": self._recognizer.enabled})
        finally:
            with self._lock:
                self._infer_busy = False
                self._infer_started_at = 0.0
        # 推理完成后触发规则评估回调(在推理线程里调,回调内部负责跨线程调度)
        if self._on_inference_done is not None:
            try:
                self._on_inference_done()
            except Exception:  # noqa: BLE001
                logger.exception("on_inference_done callback failed")

    async def _run_inference_async(self, frame: np.ndarray) -> None:
        """异步推理，跑在主事件循环里（由 run_coroutine_threadsafe 投递）。

        与 _run_inference 的区别：直接 await classify_frame_async，不经过
        asyncio.run + 线程池。httpx 网络等待释放 GIL，encode_frame_b64 也已
        用 to_thread 包住，不再抢 GIL 饿死摄像头采集线程。
        """
        started = time.time()
        with self._lock:
            self._infer_started_at = started
        try:
            result = await self._recognizer.classify_frame_async(frame)
            logger.info(
                "Inference result updated",
                extra={
                    "action": result.action,
                    "feedback": result.feedback,
                    "elapsed_seconds": round(time.time() - started, 2),
                },
            )
            with self._lock:
                self._latest_result = result
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inference failed")
            with self._lock:
                self._latest_result = ActionResult("idle", f"模型识别失败: {exc}", {"source": "vision", "enabled": self._recognizer.enabled})
        finally:
            with self._lock:
                self._infer_busy = False
                self._infer_started_at = 0.0
        # 推理完成后触发规则评估回调（已在主循环里，但回调内部仍用
        # call_soon_threadsafe 调度，二次投递无害且保持与同步路径一致）
        if self._on_inference_done is not None:
            try:
                self._on_inference_done()
            except Exception:  # noqa: BLE001
                logger.exception("on_inference_done callback failed")

    def _resolve_display_result(self, result: ActionResult) -> ActionResult:
        if not self._recognizer.enabled:
            return ActionResult(
                action="idle",
                feedback="视觉模型未启用，当前只有摄像头画面，没有识别。",
                details={**(result.details or {}), "enabled": False},
            )

        has_event = result.action not in {"idle", "no_event"}
        if has_event:
            self._presence_count += 1
            self._absence_count = 0
        else:
            self._absence_count += 1
            self._presence_count = 0

        # 根据运动距离判断是否触发了推理
        motion_triggered = self._state.motion_distance >= self._motion.threshold

        if self._presence_count >= self._presence_threshold:
            return ActionResult(
                action=result.action,
                feedback=result.feedback,
                details={**(result.details or {}), "presence_frames": self._presence_count, "presence_threshold": self._presence_threshold},
            )

        if self._absence_count >= self._presence_threshold:
            # 如果触发了推理，显示LLM的observation；否则显示"画面平静"
            feedback = result.feedback if motion_triggered else "画面平静，未检测到明显事件。"
            return ActionResult(
                action="idle",
                feedback=feedback,
                details={**(result.details or {}), "absence_frames": self._absence_count, "presence_threshold": self._presence_threshold},
            )

        if has_event:
            return ActionResult(
                action="waiting_confirm",
                feedback=f"检测到事件，正在确认…",
                details={**(result.details or {}), "presence_frames": self._presence_count, "presence_threshold": self._presence_threshold},
            )

        # 如果触发了推理，显示LLM的observation；否则显示"画面平静"
        feedback = result.feedback if motion_triggered else "画面平静，等待新事件。"
        return ActionResult(
            action="idle",
            feedback=feedback,
            details={**(result.details or {}), "presence_frames": 0, "presence_threshold": self._presence_threshold},
        )

    @staticmethod
    def _prepare_display_frame(frame: np.ndarray) -> np.ndarray:
        return cv2.convertScaleAbs(frame, alpha=1.15, beta=10)
