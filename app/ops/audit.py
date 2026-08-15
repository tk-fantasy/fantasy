"""运维操作审计日志（09 清单条目 1）。

远程/运维侧的敏感操作（诊断包导出等）追加写 JSONL 到
logs/audit/ops_audit.jsonl，谁、何时、做了什么——交付验收的底线要求。
进程内加锁保证并发追加不交叉。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import BASE_DIR

AUDIT_DIR = BASE_DIR / "logs" / "audit"
AUDIT_FILE = AUDIT_DIR / "ops_audit.jsonl"

_lock = threading.Lock()


def record(operator: str, action: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """追加一条审计记录并返回它（写失败不抛：审计不应阻断业务）。"""
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "operator": operator,
        "action": action,
        "detail": detail or {},
    }
    try:
        with _lock:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry


def tail(limit: int = 50) -> list[dict[str, Any]]:
    """读最近 limit 条审计记录（新的在后）。文件不存在返回空列表。"""
    if not AUDIT_FILE.exists():
        return []
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result = []
    for line in lines[-limit:]:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result
