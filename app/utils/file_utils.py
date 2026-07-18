from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: Path, data: str, encoding: str = "utf-8") -> None:
    """原子写入：先写临时文件，再 rename 覆盖目标文件。

    进程崩溃时最多损坏临时文件，不会破坏已有数据。
    Windows 上如果目标文件被占用导致 replace 失败，会清理临时文件。

    Docker bind-mount 单文件（如 ./config.json:/aether/config.json）下
    os.replace 会报 "Device or resource busy" —— 此时退化为 truncate+write
    直接覆盖目标文件内容（非原子，但 bind-mount 下唯一可行方案）。
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(data, encoding=encoding)
    try:
        os.replace(tmp_path, path)
    except OSError:
        # Windows 上文件被占用时 replace 会失败，清理临时文件
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        # Docker bind-mount 单文件场景：os.replace 报 EBUSY，
        # 退化为直接 truncate+write（非原子，但能写进去）
        try:
            path.write_text(data, encoding=encoding)
        except OSError:
            raise
