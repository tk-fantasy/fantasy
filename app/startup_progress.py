"""冷启动进度上报：在主服务 HTTP 端口就绪前，提供一个轻量进度端口。

后端冷启动期间（导入 faiss / 初始化服务 / 构建 RAG 索引）主端口 8010 尚未监听，
前端无法通过 /api/health 询问状态。本模块在最早期起一个仅用标准库的 HTTP 服务，
随启动推进更新当前阶段，供加载页轮询展示真实进度。

- 端口：8011（仅用标准库 http.server，daemon 线程）
- 路径：/progress 或 /api/startup-progress
- 返回：{"stage": str, "ready": bool, "elapsed_sec": float}
- 绑定地址：默认 127.0.0.1（仅本机）；容器部署设 STARTUP_PROGRESS_HOST=0.0.0.0 暴露给宿主
- 绑定失败时静默跳过，绝不阻断主启动
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

_PROGRESS_PORT = 8011
# 默认仅本机回环；容器内需置 0.0.0.0 才能让宿主浏览器访问加载进度
_PROGRESS_HOST = os.getenv("STARTUP_PROGRESS_HOST", "127.0.0.1")


class _State:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stage = "正在启动..."
        self._ready = False
        self._start_ts = time.monotonic()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def set(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def mark_ready(self) -> None:
        with self._lock:
            self._ready = True
            self._stage = "就绪"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "stage": self._stage,
                "ready": self._ready,
                "elapsed_sec": round(time.monotonic() - self._start_ts, 1),
            }

    def start(self) -> None:
        if self._server is not None:
            return
        state = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默访问日志
                pass

            def _respond(self) -> None:
                payload = json.dumps(state.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                if self.path.split("?")[0] in ("/progress", "/api/startup-progress"):
                    self._respond()
                else:
                    self.send_response(404)
                    self.end_headers()

        try:
            self._server = ThreadingHTTPServer((_PROGRESS_HOST, _PROGRESS_PORT), _Handler)
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="startup-progress",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "Startup progress server on http://%s:%d/progress",
                _PROGRESS_HOST,
                _PROGRESS_PORT,
            )
        except OSError as e:
            # 端口被占用等：不阻断主启动，仅记录
            logger.warning("Startup progress server not started: %s", e)
            self._server = None

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None


startup_progress = _State()
