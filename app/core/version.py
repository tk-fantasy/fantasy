"""应用版本号（09 清单条目 2）。

单一来源：仓库根 version.json（scripts/build-update-pack.py 打包与
scripts/upgrade.sh 兼容性检查用同一份）。文件缺失/损坏时回退
0.0.0-dev，保证源码直接跑（pip 环境）不报错。
"""
from __future__ import annotations

import json
from functools import lru_cache

from .config import BASE_DIR

VERSION_PATH = BASE_DIR / "version.json"
FALLBACK_VERSION = "0.0.0-dev"


@lru_cache(maxsize=1)
def get_version() -> str:
    try:
        data = json.loads(VERSION_PATH.read_text(encoding="utf-8"))
        return str(data.get("version") or FALLBACK_VERSION)
    except (OSError, json.JSONDecodeError):
        return FALLBACK_VERSION
