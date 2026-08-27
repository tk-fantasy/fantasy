"""test-camera 测试插件。

上传（或指定）一个视频 → 注册一路虚拟摄像头（与真实摄像头同等地位）→
按视频 FPS 采样抽帧推给宿主 → 帧进入真实视觉链路（dHash 运动触发 →
VL 规则评估 → 动作执行，默认演练）。

无视频库、无状态文件：同一时刻只播一个视频——上传新的自动替换并
循环播放；上传目录（宿主注入 AETHER_PLUGIN_UPLOAD_DIR）里的旧文件
直接删除。动作演练开关（real_exec）只存内存，插件重启回到默认演练
（安全方向）。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

# 插件子进程经 PYTHONPATH 拿到项目根（宿主 plugin_process 注入）
from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.stdio_runtime import run_stdio_plugin

import cv2

_log = logging.getLogger("test-camera")

# 帧推送采样率上限：视频原 FPS 再高也只抽这个频率。15fps 预览已流畅
# （识别链路用不到更高），640 长边 q70 下 stdio 带宽约 1MB/s，宿主解码
# 开销占单核几个百分点，均为本机管道可忽略量级。
MAX_PUSH_FPS = 15.0
# 推送帧长边（等比缩放）：识别 448 内足够，预览清晰
PUSH_MAX_SIDE = 640
PUSH_JPEG_QUALITY = 70
# 发送队列上限：超时未发出去就丢帧（防背压堆积）
SEND_QUEUE_MAX = 4


class TestCameraPlugin(IntegrationPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.camera_id: str = ""
        self.current_video: dict | None = None   # {path, name}
        self.real_exec: bool = False
        self.camera_name: str = "测试摄像头"
        self._lock = threading.Lock()
        # 线程安全的帧发送队列：播放线程 put，事件循环里的 sender 协程 get。
        # 不用 asyncio.Queue——它非线程安全，跨线程 put 会丢唤醒。
        self._send_queue: queue.Queue[str] = queue.Queue(maxsize=SEND_QUEUE_MAX)
        self._play_seq = 0  # 播放代次：换视频/重播时旧循环自然退出
        self._loop: asyncio.AbstractEventLoop | None = None  # setup 时捕获
        self._sent = 0
        self._dropped = 0

    # ---------- 生命周期 ----------

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        self.manifest = manifest_dict
        self.register_method("playback.set", self._m_playback_set)
        self.register_method("playback.restart", self._m_playback_restart)
        self.register_method("playback.status", self._m_playback_status)
        self.register_method("config.set", self._m_config_set)
        self.register_method("config.get", self._m_config_get)
        # setup 在事件循环内被调（run_stdio_plugin），此处捕获主循环供播放线程投递
        self._loop = asyncio.get_running_loop()
        self._loop.create_task(self._startup())

    async def _startup(self) -> None:
        """注册虚拟摄像头 + 拉起发送循环。"""
        assert self.host is not None
        spec = {
            "name": self.camera_name,
            "display_enabled": 1,
            "flags": {"real_exec": self.real_exec},
        }
        try:
            result = await self.host.camera.register(spec)
            self.camera_id = str(result.get("camera_id", ""))
            _log.info("虚拟摄像头已注册: %s", self.camera_id)
        except Exception:  # noqa: BLE001
            _log.exception("虚拟摄像头注册失败（宿主无 camera 权限方法？）")
            return
        self._loop.create_task(self._sender_loop())

    # ---------- 播放 ----------

    def _restart_playback(self) -> None:
        """代次 +1 让旧播放循环退出，新循环在 executor 里拉起。

        可能从事件循环（RPC handler）或播放线程自身调用：统一经 call_soon_threadsafe
        把 run_in_executor 投回主循环，避免跨线程直接操作 loop。
        """
        self._play_seq += 1
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if not (self.camera_id and self.current_video):
            return

        def _launch():
            loop.run_in_executor(None, self._play_worker, self._play_seq)
        try:
            loop.call_soon_threadsafe(_launch)
        except RuntimeError:
            pass  # 循环已关（进程退出中）

    def _cleanup_old_uploads(self, keep_path: str) -> None:
        """上传目录里只保留当前视频，其余（旧上传）直接删除。"""
        upload_dir = os.environ.get("AETHER_PLUGIN_UPLOAD_DIR", "")
        if not upload_dir:
            return
        try:
            root = Path(upload_dir)
            if not root.is_dir():
                return
            keep = Path(keep_path).resolve()
            for f in root.iterdir():
                if f.is_file() and f.resolve() != keep:
                    f.unlink(missing_ok=True)
                    _log.info("已清理旧上传视频: %s", f.name)
        except OSError:
            _log.warning("清理旧上传文件失败", exc_info=True)

    async def _m_playback_set(self, params: dict) -> dict:
        """设置要播放的视频：校验可打开 → 记录为当前视频（清旧文件）→ 起播。"""
        path = str(params.get("path", "")).strip()
        name = str(params.get("name", "")).strip() or Path(path).name
        if not path:
            return {"error": "path required"}
        p = Path(path)
        if not p.is_file():
            return {"error": f"文件不存在（路径以 Aether 所在环境为准，"
                             f"Docker 部署时是容器内路径）: {path}"}
        probe = cv2.VideoCapture(str(p))
        ok = probe.isOpened()
        fps = probe.get(cv2.CAP_PROP_FPS) or 0.0
        frames = probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        probe.release()
        if not ok:
            return {"error": f"无法打开视频（opencv/ffmpeg 不支持该格式）: {path}"}
        with self._lock:
            self.current_video = {
                "path": str(p.resolve()), "name": name,
                "fps": round(float(fps), 2),
                "duration_s": round(frames / fps, 1) if fps else 0.0,
            }
        # 上传目录里的旧视频直接删（只留当前这个）
        self._cleanup_old_uploads(str(p.resolve()))
        self._restart_playback()
        return {"ok": True, "video": self.current_video}

    async def _m_playback_restart(self, params: dict) -> dict:
        if not self.camera_id or not self.current_video:
            return {"error": "当前没有在播的视频，先上传或指定一个"}
        self._restart_playback()
        return {"ok": True}

    async def _m_playback_status(self, params: dict) -> dict:
        with self._lock:
            current = dict(self.current_video) if self.current_video else None
        return {
            "camera_id": self.camera_id,
            "current": current,
            "playing": bool(current),
            "sent": self._sent,
            "dropped": self._dropped,
        }

    def _play_worker(self, seq: int) -> None:
        """线程：cv2 按视频 FPS 读帧 → 采样限频 → JPEG base64 → 发送队列。

        代次检查贯穿全程：换视频/重播后旧线程在下一个采样点自然退出。
        播完自动循环（测试场景常驻）。队列是 threading 的 queue.Queue，
        可安全跨线程 put；事件循环里的 _sender_loop 轮询 get。
        """
        with self._lock:
            video = dict(self.current_video) if self.current_video else None
        if not video or not self.camera_id:
            return
        cap = cv2.VideoCapture(video["path"])
        if not cap.isOpened():
            _log.warning("打开视频失败: %s", video["path"])
            return
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        src_fps = max(1.0, float(src_fps))
        sample_interval = 1.0 / min(src_fps, MAX_PUSH_FPS)
        step = max(1, int(src_fps * sample_interval))  # 每 step 帧取 1 帧
        last_send = 0.0
        sent = 0
        dropped = 0
        idx = 0
        _log.info("开始播放 %s (fps=%.1f, sample=%.2fs)",
                  video["name"], src_fps, sample_interval)
        while seq == self._play_seq:
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # 循环播放
                idx = 0
                continue
            idx += 1
            if idx % step:
                continue
            now = time.monotonic()
            if now - last_send < sample_interval:
                continue
            b64 = self._encode_frame(frame)
            if b64 is None:
                continue
            try:
                self._send_queue.put_nowait(b64)
                sent += 1
            except queue.Full:
                dropped += 1
            last_send = now
            self._sent, self._dropped = sent, dropped
            time.sleep(sample_interval)
        cap.release()
        _log.info("播放结束/停止 (seq=%d, sent=%d, dropped=%d)", seq, sent, dropped)

    def _encode_frame(self, frame) -> str | None:
        """等比缩放 + JPEG + base64。"""
        h, w = frame.shape[:2]
        scale = min(1.0, PUSH_MAX_SIDE / max(h, w))
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), PUSH_JPEG_QUALITY])
        if not ok:
            return None
        return base64.b64encode(buf.tobytes()).decode("ascii")

    async def _sender_loop(self) -> None:
        """常驻发送循环：轮询线程安全队列 → host.camera.push_frame 反向 RPC。

        轮询而非阻塞 get：queue.Queue 的阻塞 get 在 asyncio 协程里会冻结事件循环。
        50ms 间隔远小于 15fps 的 66ms 帧间隔，轮询相位抖动不超过半帧。
        """
        assert self.host is not None
        while True:
            try:
                b64 = self._send_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            try:
                result = await self.host.camera.push_frame(self.camera_id, b64)
                if not result.get("ok"):
                    _log.debug("push_frame rejected: %s", result)
            except Exception:  # noqa: BLE001
                _log.debug("push_frame failed", exc_info=True)

    # ---------- 配置 ----------

    async def _m_config_set(self, params: dict) -> dict:
        if "real_exec" in params:
            self.real_exec = bool(params["real_exec"])
        if "camera_name" in params and str(params["camera_name"]).strip():
            self.camera_name = str(params["camera_name"]).strip()
        # 同步运行时标志到宿主（演练开关即时生效）
        if self.camera_id and "real_exec" in params:
            try:
                await self.host.camera.set_flags(self.camera_id, {
                    "real_exec": self.real_exec,
                })
            except Exception:  # noqa: BLE001
                _log.warning("set_flags 同步失败", exc_info=True)
        return {"ok": True}

    async def _m_config_get(self, params: dict) -> dict:
        return {
            "real_exec": self.real_exec,
            "camera_name": self.camera_name,
            "camera_id": self.camera_id,
        }


if __name__ == "__main__":
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    if os.environ.get("AETHER_TEST_CAMERA_DEBUG"):
        logging.basicConfig(level=logging.DEBUG)
    asyncio.run(run_stdio_plugin(TestCameraPlugin, _manifest_path))
