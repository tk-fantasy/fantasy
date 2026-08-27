"""虚拟摄像头流：帧由插件进程注入，其余管线全复用 CameraStream。

用途（test-camera 测试插件）：把视频文件的帧当作一路摄像头送进真实链路——
dHash 运动门控、视觉规则评估（VL）、MJPEG 预览、get_recent_frames 全部
无差别生效。本类只替换"帧从哪来"：不打开任何采集设备，消费
enqueue_frame 注入的帧队列；超过 _frame_timeout 秒没有新帧视为离线
（保留最后一帧给前端宽限期，与真实摄像头断流行为一致）。
"""
from __future__ import annotations

import logging
import queue
import time

from .camera_stream import CameraStream

logger = logging.getLogger(__name__)


class VirtualCameraStream(CameraStream):
    """帧注入型 CameraStream。"""

    def __init__(self, *args, frame_timeout: float = 15.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._inject_queue: queue.Queue = queue.Queue(maxsize=8)
        self._frame_timeout = max(1.0, float(frame_timeout))
        self._last_inject_at = 0.0
        # 注入侧丢帧计数（enqueue_frame 队满丢弃），诊断推送背压用
        self._dropped_frames = 0

    def start(self) -> None:
        if self._running:
            return
        with self._lock:
            self._state.backend_name = "virtual"
        super().start()

    def enqueue_frame(self, frame) -> bool:
        """外部注入一帧（BGR ndarray）。队列满丢弃（防背压阻塞插件 RPC）。

        返回是否入队成功——调用方（CameraManager.push_frame）据此统计丢帧。
        """
        try:
            self._inject_queue.put_nowait(frame)
            return True
        except queue.Full:
            self._dropped_frames += 1
            return False

    def _worker(self) -> None:
        """消费注入帧队列 → 走与真实采集完全相同的 _process_frame 管线。"""
        last_log_time = time.time()
        while self._running:
            try:
                try:
                    frame = self._inject_queue.get(timeout=1.0)
                except queue.Empty:
                    # 没有新帧：短暂超时不算离线，持续超时才标记
                    if (self._last_inject_at > 0
                            and time.time() - self._last_inject_at > self._frame_timeout):
                        self._mark_camera_closed("测试插件未推送帧", keep_cache=True)
                    continue
                self._last_inject_at = time.time()
                if frame is None or getattr(frame, "size", 0) == 0:
                    continue
                self._process_frame(frame)
                now = time.time()
                if now - last_log_time >= 10.0:
                    logger.info(
                        "Virtual camera worker alive (camera_id=%s, queue=%d, dropped=%d)",
                        self.camera_id, self._inject_queue.qsize(), self._dropped_frames,
                    )
                    last_log_time = now
            except Exception:  # noqa: BLE001
                logger.exception("Virtual camera worker crashed")
                time.sleep(0.5)
