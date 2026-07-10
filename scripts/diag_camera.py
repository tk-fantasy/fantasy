"""摄像头诊断脚本 — 隔离 OpenCV 层面能否打开设备，不依赖 FastAPI。

用法（在项目根目录）：
    D:/anaconda/envs/yolo/python.exe scripts/diag_camera.py

输出每个 backend 的 isOpened / 首帧 / 分辨率 / 连续读帧情况，并给出建议。
"""
from __future__ import annotations

import sys
import time

import cv2


def probe(label: str, index: int, backend: int) -> None:
    print(f"\n--- {label} (index={index}) ---")
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        print(f"  [X] isOpened = False  → 打不开")
        cap.release()
        return

    # 试设分辨率
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
    time.sleep(0.3)

    # 预热读帧（最多 12 次，和应用逻辑一致）
    first_ok, first_frame = False, None
    t0 = time.time()
    for i in range(12):
        ok, frame = cap.read()
        if ok and frame is not None:
            first_ok, first_frame = True, frame
            print(f"  [OK] 首帧成功，第 {i+1} 次读到，耗时 {time.time()-t0:.2f}s")
            break
        time.sleep(0.08)

    if not first_ok:
        print(f"  [X] isOpened=True 但连续 12 次读不到帧（预热失败）")
        cap.release()
        return

    h, w = first_frame.shape[:2]
    print(f"  分辨率: {w}x{h}, 实际 fps 设置: {cap.get(cv2.CAP_PROP_FPS)}")

    # 连续读 30 帧看稳定性
    ok_cnt, fail_cnt = 0, 0
    t1 = time.time()
    for _ in range(30):
        ok, frame = cap.read()
        if ok and frame is not None:
            ok_cnt += 1
        else:
            fail_cnt += 1
    dt = time.time() - t1
    print(f"  连续 30 帧: 成功 {ok_cnt}, 失败 {fail_cnt}, 用时 {dt:.2f}s, 实测 {ok_cnt/dt:.1f} fps")

    cap.release()
    print(f"  release 完成")


def main() -> None:
    print("=== Aether 摄像头诊断 ===")
    print(f"OpenCV {cv2.__version__}")
    info = cv2.getBuildInformation()
    print(f"  DSHOW 后端: {'可用' if 'DirectShow' in info else '不可用'}")
    print(f"  MSMF  后端: {'可用' if 'MSMF' in info else '不可用'}")

    # 系统里能看到几个摄像头索引（试 0~3）
    print("\n=== 探测可用 backend / index ===")
    backends = []
    if 'DirectShow' in info:
        backends.append(("DSHOW", cv2.CAP_DSHOW))
    if 'MSMF' in info:
        backends.append(("MSMF", cv2.CAP_MSMF))

    if not backends:
        print("\n[X] 你的 OpenCV 没有任何可用的摄像头后端！需要重装带 DSHOW/MSMF 的 opencv-python")
        sys.exit(1)

    for idx in range(4):
        for name, b in backends:
            cap = cv2.VideoCapture(idx, b)
            opened = cap.isOpened()
            cap.release()
            if opened:
                print(f"  index={idx} backend={name}: 可打开 ✓")

    print("\n=== 逐个详细探测 ===")
    for name, b in backends:
        for idx in range(2):
            probe(f"{name}:{idx}", idx, b)

    print("\n=== 建议 ===")
    print("- 把『能成功读到首帧且 30 帧成功率高』的那组 (backend, index) 填进 config.json:")
    print('    "vision": { "camera_index": <index> }')
    print("- 注意：Aether 代码里 msmf 后端若不可用，会自动跳过，不影响。")
    print("- 如果全部 [X]，常见原因：被其它程序占用(会议软件/相机App)、隐私权限、驱动问题。")


if __name__ == "__main__":
    main()
