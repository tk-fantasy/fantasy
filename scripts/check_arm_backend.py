"""ARM opencv RTSP/ffmpeg 后端预检(D2)。

目标机 ARM A55 上 opencv-python 的 wheel 可能未带 ffmpeg,
导致 RTSP over TCP 打不开或花屏。本脚本:
1. 打印 cv2.getBuildInformation() 的 FFMPEG/GStreamer 标志
2. 实测能否用当前 backend 打开一路 RTSP(可选,传 --rtsp)

用法:
    python scripts/check_arm_backend.py
    python scripts/check_arm_backend.py --rtsp rtsp://user:pwd@192.168.1.10/stream

退出码:0 = 有 ffmpeg/gstreamer 可用;1 = 都没有(需换 GStreamer 手动构建或改后端)。
"""
from __future__ import annotations

import argparse
import sys


def _check_build_info() -> dict:
    import cv2
    info = cv2.getBuildInformation()
    flags = {}
    for line in info.splitlines():
        line = line.strip()
        if line.startswith("FFMPEG:"):
            flags["ffmpeg"] = line.split(":", 1)[1].strip() == "YES"
        if line.startswith("GStreamer:"):
            flags["gstreamer"] = line.split(":", 1)[1].strip() == "YES"
    return flags


def _try_open(rtsp: str | None) -> bool:
    import cv2
    if not rtsp:
        return True  # 跳过实测
    cap = cv2.VideoCapture(rtsp, cv2.CAP_FFMPEG)
    ok = cap.isOpened()
    if ok:
        ret, _ = cap.read()
        ok = ret
    cap.release()
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtsp", default=None, help="可选,实测打开一路 RTSP")
    args = parser.parse_args()

    flags = _check_build_info()
    print("后端标志:", flags)
    if args.rtsp:
        opened = _try_open(args.rtsp)
        print(f"RTSP 实测打开({args.rtsp}):", opened)
        if not opened:
            return 1
    if not flags.get("ffmpeg") and not flags.get("gstreamer"):
        print("⚠️ ffmpeg 和 GStreamer 都未编入,RTSP 大概率打不开。")
        print("   方案:1) 换带 ffmpeg 的 opencv 源码构建;2) 改用 GStreamer pipeline 后端。")
        return 1
    if not flags.get("ffmpeg"):
        print("⚠️ 无 ffmpeg,RTSP over TCP 低延迟参数(OPENCV_FFMPEG_CAPTURE_OPTIONS)无效。")
        print("   需改走 GStreamer 后端(CAP_GSTREAMER + pipeline)。在 camera_stream.py _open_network_stream 备分支。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
