#!/usr/bin/env python3
"""发版辅助 —— 升 version.json 并给出发布步骤清单。

版本号是升级机制的命门：不升版本，导出的包与接收方当前版本相同，
「只升不降」规则会让自动升级永远不触发。

用法（仓库根目录）：
    python scripts/new-version.py 1.1.0 --notes "新增xx；修复yy"
    python scripts/new-version.py 2.0.0 --min-compatible 1.5.0 --notes "破坏性改动"

版本号建议（语义化）：
    1.0.0 → 1.0.1  修 bug
    1.0.0 → 1.1.0  加功能
    1.0.0 → 2.0.0  大改/不兼容（同时升 min_compatible）

变更汇总不知道写什么：git log --oneline -20 看提交记录提炼。
之后：commit 推送 → docker compose up -d --build → 运维页「一键导出升级包」→ 发包。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "version.json"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.\-_]+)?$")


def apply_bump(data: dict, version: str, min_compatible: str | None, notes: str) -> dict:
    """校验并写入新版本字段，返回新 dict（不落盘，便于测试）。"""
    if not VERSION_RE.match(version):
        raise ValueError(f"版本号格式应为 x.y.z（如 1.1.0），收到: {version}")
    if min_compatible and not VERSION_RE.match(min_compatible):
        raise ValueError(f"min_compatible 格式应为 x.y.z，收到: {min_compatible}")
    out = dict(data)
    old = out.get("version", "?")
    out["version"] = version
    if min_compatible:
        out["min_compatible"] = min_compatible
    if notes:
        out["notes"] = notes
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="升 version.json 版本号（发版第一步）")
    parser.add_argument("version", help="新版本号，如 1.1.0")
    parser.add_argument("--notes", default="", help="变更汇总（写入 version.json，导出时随包发布）")
    parser.add_argument("--min-compatible", default="", help="最低兼容版本（破坏性改动时指定）")
    args = parser.parse_args()

    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[new-version] 读 {VERSION_FILE} 失败: {e}", file=sys.stderr)
        return 1
    old_version = str(data.get("version") or "?")
    try:
        data = apply_bump(data, args.version, args.min_compatible.strip(), args.notes.strip())
    except ValueError as e:
        print(f"[new-version] {e}", file=sys.stderr)
        return 1
    VERSION_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[new-version] v{old_version} → v{args.version} 已写入 version.json。接下来：")
    print("  1. git add version.json && git commit -m 'chore(release): v%s'" % args.version)
    print("  2. docker compose up -d --build        # 本机构建并运行新版本")
    print("  3. 运维页 → 升级包分发 → 一键导出升级包  # 产出 aether-update-%s.tar.gz" % args.version)
    print("  4. 下载发给对方 → 对方放进 Aether/backups/ 即自动升级")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
