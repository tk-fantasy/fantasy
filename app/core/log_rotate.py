"""Copy-truncate 式日志轮转 handler。

为什么不用标准 RotatingFileHandler：它轮转靠 os.rename(app.log → app.log.1)。
logs/ 目录 bind-mount 到 Windows 宿主时（Docker Desktop 文件共享层），宿主侧
任何进程（编辑器 / tail / 宿主直跑的后端）持有 app.log 句柄都会让容器内的
rename 被拒（PermissionError）。更糟的是 stdlib 的 doRollover 先关流再改名，
失败后 stream 停在关闭态——之后每条日志都在 shouldRollover→doRollover 里
循环失败，文件日志整体停写（app.log 冻结在 maxBytes）。

copy-truncate：内容复制到 .1（备份链逐级复制）后原地截断主文件。全程只用
open/read/write，不做 rename/remove，跨文件共享层可靠。
"""
from __future__ import annotations

import os
import shutil
from logging.handlers import RotatingFileHandler


def _copy_file(src: str, dst: str) -> None:
    """以 truncate-write 方式复制（不 remove 目标，避开共享层删除限制）。"""
    with open(src, "rb") as s, open(dst, "wb") as d:
        shutil.copyfileobj(s, d)


class CopyTruncateRotatingFileHandler(RotatingFileHandler):
    """轮转 = 备份链复制 + 主文件原地截断，无 rename。"""

    def doRollover(self) -> None:  # noqa: D102 - 覆写 stdlib 行为
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.backupCount > 0:
            base = self.baseFilename
            # .(n-1)→.n … .1→.2：备份链同样用复制（rename 在共享层不可靠）
            for i in range(self.backupCount - 1, 0, -1):
                src, dst = f"{base}.{i}", f"{base}.{i + 1}"
                if os.path.exists(src):
                    _copy_file(src, dst)
            _copy_file(base, f"{base}.1")
        # 主文件原地截断（'wb' 打开即清空；随后 emit 重新 _open，mode='a' 偏移为 0）
        with open(self.baseFilename, "wb"):
            pass
