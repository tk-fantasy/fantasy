"""升级包自动升级（投放即升）。

监视 backups/（与手动安装共用目录）：发现「版本高于当前」且已落稳的
aether-update-*.tar.gz 时自动安装——校验 → load → 重启 → 删包，
全程无需登录页面操作。

设计要点：
- 只升不降/不重装：导出方本机导出的包（版本=当前）也躺在同目录，
  低于等于当前版本的包一律跳过，避免"导出即自装"或误降级。
- 落稳判定：文件 mtime 静默超过 SETTLE_SECONDS 才算拷贝完成
  （GB 级包经 SMB/网盘拷贝可达数分钟，拷一半的包解不开）。
- 关闭开关：config.json update.auto_upgrade=false。
"""
from __future__ import annotations

import asyncio
import json
import logging
import tarfile
import time
from pathlib import Path

from ..core.config import get_config
from ..core.version import get_version
from . import pack_export
from .upgrade import _version_key

logger = logging.getLogger(__name__)

POLL_SECONDS = 60     # 扫描周期
SETTLE_SECONDS = 60   # mtime 静默窗口：拷贝中的包不算落稳

_lock = asyncio.Lock()   # 防止与手动安装同包并发 apply


def auto_upgrade_enabled() -> bool:
    return bool(get_config("update.auto_upgrade", True))


def _peek_manifest_version(path: Path) -> str | None:
    """读包内 manifest.json 的 version（包损坏/缺 manifest 返回 None）。"""
    try:
        with tarfile.open(path, "r:gz") as tf:
            fobj = tf.extractfile("manifest.json")
            if fobj is None:
                return None
            return str(json.loads(fobj.read().decode("utf-8")).get("version") or "")
    except (tarfile.TarError, KeyError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def find_candidate() -> tuple[Path, str] | None:
    """挑出应自动安装的包：合法名 + 已落稳 + 版本严格高于当前，取最新版本。

    返回 (路径, 包版本)；无候选返回 None。
    """
    pack_dir = pack_export.PACK_DIR
    if not pack_dir.exists():
        return None
    current = get_version()
    best: tuple[Path, str] | None = None
    now = time.time()
    for p in pack_dir.iterdir():
        if not p.is_file() or not pack_export.PACK_NAME_RE.match(p.name):
            continue
        if now - p.stat().st_mtime < SETTLE_SECONDS:
            continue   # 还在拷贝/刚放入，等下一轮
        version = _peek_manifest_version(p)
        if not version:
            continue   # 拷了一半或坏包：静默跳过，不动它（人工排查）
        if _version_key(version) <= _version_key(current):
            continue   # 导出方本机的包（版本=当前）或旧包：不自动动
        if best is None or _version_key(version) > _version_key(best[1]):
            best = (p, version)
    return best


async def watcher_loop() -> None:
    """后台循环：扫描 → 自动安装。任何异常只记日志，不让监视器死掉。"""
    while True:
        candidate = None
        try:
            if auto_upgrade_enabled():
                candidate = find_candidate()
                if candidate:
                    path, version = candidate
                    logger.info("Auto-update: applying %s (v%s → v%s)",
                                path.name, get_version(), version)
                    async with _lock:
                        result = await pack_export.apply_local_pack(path.name, "auto")
                    logger.info("Auto-update applied: %s → %s, restarting",
                                result.get("from_version"), result.get("to_version"))
        except Exception:  # noqa: BLE001 — 监视器必须活着
            logger.exception("Auto-update watcher iteration failed")
            # 失败隔离：改名 .failed 防止每分钟重试（大包每次 docker load 很重）。
            # 人工排障后删掉后缀即可重新触发。
            path = candidate[0] if candidate else None
            if path and path.exists():
                try:
                    path.rename(path.with_name(path.name + ".failed"))
                    logger.warning("Auto-update: quarantined failed pack → %s.failed", path.name)
                except OSError:
                    pass
        await asyncio.sleep(POLL_SECONDS)
