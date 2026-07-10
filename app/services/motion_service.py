from __future__ import annotations

import threading

import cv2
import numpy as np


def compute_dhash(frame_bgr: np.ndarray, hash_size: int = 16) -> np.ndarray:
    """计算差值感知哈希(dHash),返回 hash_size*hash_size 的布尔数组。

    差值感知哈希算法,用 cv2/numpy 实现,
    避免引入 PIL/imagehash 依赖。
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (hash_size + 1, hash_size), interpolation=cv2.INTER_AREA)
    return resized[:, 1:] > resized[:, :-1]


def hamming_distance(hash1: np.ndarray, hash2: np.ndarray) -> int:
    return int(np.count_nonzero(hash1 != hash2))


class MotionDetector:
    """基于 dHash 的轻量运动检测,代价仅一次缩放,微秒级。

    比较对象是"上一次送模型推理的参考帧",而不是上一帧——
    这样缓慢入画的目标累积差异后依然能触发。
    """

    def __init__(self, hash_size: int = 16, threshold: int = 15) -> None:
        self._hash_size = hash_size
        self._threshold = threshold
        self._reference_hash: np.ndarray | None = None
        self._last_hash: np.ndarray | None = None
        self._lock = threading.Lock()

    @property
    def threshold(self) -> int:
        return self._threshold

    def assess(self, frame_bgr: np.ndarray) -> tuple[bool, int]:
        """返回 (是否运动, 与参考帧的汉明距离);尚无参考帧时返回 (True, -1)。"""
        current = compute_dhash(frame_bgr, self._hash_size)
        with self._lock:
            self._last_hash = current
            if self._reference_hash is None:
                return True, -1
            distance = hamming_distance(current, self._reference_hash)
        return distance > self._threshold, distance

    def commit_reference(self) -> None:
        """把最近一次 assess 的帧设为新参考帧(在推理真正发生后调用)。"""
        with self._lock:
            if self._last_hash is not None:
                self._reference_hash = self._last_hash
