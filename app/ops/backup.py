"""应用侧备份/恢复（运维页按钮的后端实现）。

备份范围（zip 内结构与 scripts/backup.sh 对齐，两边的包可互相恢复）：
- config.json          系统配置
- .env                 密钥（LLM key / JWT_SECRET 等）
- data/                应用数据：SQLite（一致性快照）、jwt secret、emoji/rag 索引

不在范围内：ha_config / mosquitto（宿主侧文件，容器内不可见）——整机备份
仍用 scripts/backup.sh，恢复用 scripts/restore.sh（含 HA/MQTT）。

SQLite 快照：以只读连接 + sqlite3 backup API 生成一致性副本（WAL 模式下
直接复制 db/wal/shm 三件套有撕裂风险）。恢复 = 校验包 → 关库 → 覆写 →
进程退出（容器 restart: unless-stopped 自动拉起，即完成"重启生效"）。
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from ..core.config import BASE_DIR
from . import audit

logger = logging.getLogger(__name__)

BACKUP_DIR = BASE_DIR / "backups"
DATA_DIR = BASE_DIR / "app" / "data"
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"

KEEP = 3
NAME_PATTERN = re.compile(r"^aether-backup-\d{8}-\d{6}\.tar\.gz$")
# zip 条目白名单前缀（防路径穿越：仅接受这三个入口）
ALLOWED_PREFIXES = ("config.json", ".env", "data/")


def _valid_name(name: str) -> bool:
    return bool(NAME_PATTERN.match(name))


def _disk_guard(extra_bytes: int) -> None:
    usage = shutil.disk_usage(BACKUP_DIR if BACKUP_DIR.exists() else BASE_DIR)
    if usage.free < extra_bytes * 1.2:
        raise RuntimeError(
            f"磁盘剩余 {usage.free / 1024**3:.1f}GB，不足备份预估 {extra_bytes * 1.2 / 1024**3:.1f}GB"
        )


def _sqlite_snapshot(dest: Path) -> bool:
    """把 aether.db 以一致性快照写到 dest；无库返回 False。"""
    src = DATA_DIR / "aether.db"
    if not src.exists():
        return False
    # 只读连接 + backup API：WAL 下也能拿到一致快照
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)
    try:
        target = sqlite3.connect(dest)
        try:
            conn.backup(target)
        finally:
            target.close()
    finally:
        conn.close()
    return True


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return 0


def create_backup(operator: str = "unknown") -> dict:
    """创建备份（tar.gz，结构与 backup.sh 对齐），保留最近 KEEP 份。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    needed = _dir_size(DATA_DIR) + _dir_size(CONFIG_PATH) + _dir_size(ENV_PATH)
    _disk_guard(needed)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"aether-backup-{ts}.tar.gz"
    out = BACKUP_DIR / name

    with tempfile.TemporaryDirectory(prefix="aether-bak-") as td:
        tdp = Path(td)
        has_db = _sqlite_snapshot(tdp / "aether.db")
        with tarfile.open(out, "w:gz") as tf:
            if CONFIG_PATH.exists():
                tf.add(CONFIG_PATH, arcname="config.json")
            if ENV_PATH.exists():
                tf.add(ENV_PATH, arcname=".env")
            # data/：数据库快照 + 其余运行时文件（索引、jwt secret 等），
            # 跳过 WAL/SHM 伴生文件（已并入快照）
            def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
                if ti.name in ("data/aether.db-wal", "data/aether.db-shm"):
                    return None
                return ti

            tf.add(DATA_DIR, arcname="data", filter=_filter)
            if has_db:
                tf.add(tdp / "aether.db", arcname="data/aether.db")

    _prune_old()
    size = out.stat().st_size
    audit.record(operator, "backup_create", {"name": name, "size_bytes": size})
    logger.info("Backup created: %s (%d bytes)", name, size)
    return {"name": name, "size_bytes": size}


def _prune_old(keep: int = KEEP) -> None:
    backups = sorted(
        (p for p in BACKUP_DIR.iterdir() if _valid_name(p.name)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        old.unlink(missing_ok=True)
        logger.info("Pruned old backup: %s", old.name)


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    result = []
    for p in sorted(BACKUP_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if _valid_name(p.name):
            st = p.stat()
            result.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
    return result


def delete_backup(name: str, operator: str = "unknown") -> bool:
    if not _valid_name(name):
        raise ValueError("非法的备份文件名")
    target = BACKUP_DIR / name
    if not target.exists():
        return False
    target.unlink()
    audit.record(operator, "backup_delete", {"name": name})
    return True


def validate_backup(name: str) -> dict:
    """校验包完整性并返回内容清单（不落盘任何东西）。"""
    if not _valid_name(name):
        raise ValueError("非法的备份文件名")
    path = BACKUP_DIR / name
    if not path.exists():
        raise FileNotFoundError("备份不存在")
    entries: list[str] = []
    with tarfile.open(path, "r:gz") as tf:
        for member in tf.getmembers():
            # 目录条目 tar 里存为 "data"（无斜杠），归一化后判白名单
            norm = member.name.rstrip("/")
            # 白名单 + 显式拒绝穿越
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise ValueError(f"备份内含非法路径: {member.name}")
            if not (norm in ("data",) or norm.startswith(ALLOWED_PREFIXES)):
                raise ValueError(f"备份内含非白名单条目: {member.name}")
            entries.append(member.name)
    return {
        "name": name,
        "has_config": any(e == "config.json" for e in entries),
        "has_env": any(e == ".env" for e in entries),
        "has_data": any(e.startswith("data") for e in entries),
        "entry_count": len(entries),
    }


def restore_backup(name: str, operator: str = "unknown") -> dict:
    """应用备份并触发重启（os._exit → 容器 restart 策略拉起）。

    调用前应先给用户确认；本函数内部再做一次完整校验。
    """
    info = validate_backup(name)  # 校验不通过直接抛
    path = BACKUP_DIR / name
    audit.record(operator, "backup_restore", {"name": name, **info})

    with tempfile.TemporaryDirectory(prefix="aether-restore-") as td:
        tdp = Path(td)
        with tarfile.open(path, "r:gz") as tf:
            tf.extractall(tdp)  # 成员已白名单校验，无穿越

        from ..utils.file_utils import atomic_write

        if (tdp / "config.json").exists():
            atomic_write(CONFIG_PATH, (tdp / "config.json").read_text(encoding="utf-8"))
        if (tdp / ".env").exists():
            atomic_write(ENV_PATH, (tdp / ".env").read_text(encoding="utf-8"))

        # data/：先挪走旧的（失败可回退），再拷新的。
        # 数据库连接必须已由调用方（路由层）关闭——本函数只做同步文件操作。
        if (tdp / "data").exists():
            trash = BACKUP_DIR / f".restore-old-{datetime.now().strftime('%H%M%S')}"
            if DATA_DIR.exists():
                shutil.move(str(DATA_DIR), str(trash))
            shutil.copytree(tdp / "data", DATA_DIR)

    # 3. 延迟退出让 HTTP 响应先发出去；容器 restart: unless-stopped 会拉起新进程
    def _exit_soon():
        logger.warning("Restore applied, exiting for restart (backup=%s)", name)
        import os

        os._exit(0)

    timer = threading.Timer(2.0, _exit_soon)
    timer.daemon = True
    timer.start()
    return {"restored": True, "name": name, "restarting": True, **info}
