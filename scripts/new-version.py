#!/usr/bin/env python3
"""发版辅助 —— 升 version.json 并自动生成变更说明。

版本号是升级机制的命门：不升版本，导出的包与接收方当前版本相同，
「只升不降」规则会让自动升级永远不触发。

用法（仓库根目录）：
    python scripts/new-version.py 1.1.0                # 变更说明自动取 git 提交记录
    python scripts/new-version.py 1.1.0 --notes "..."  # 手写覆盖
    python scripts/new-version.py 2.0.0 --min-compatible 1.5.0  # 破坏性改动

变更说明 = 上个发版 commit（记录在 version.json 的 commit 字段）以来的
git log --oneline；导出升级包时随包发布，接收方在升级包列表可见。

版本号建议（语义化）：
    1.0.0 → 1.0.1  修 bug
    1.0.0 → 1.1.0  加功能
    1.0.0 → 2.0.0  大改/不兼容（同时升 min_compatible）

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


def current_commit() -> str:
    """当前 HEAD 短哈希（不在 git 仓库/无 git 返回空串）。"""
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=15,
                           cwd=str(VERSION_FILE.parent))
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_log_since(last_commit: str) -> str:
    """上个发版 commit 到现在的提交记录（oneline），即自动变更说明。

    无 last_commit（首次用）/git 不可用/区间无效时退回最近 20 条。
    """
    import subprocess

    def _run(args: list[str]) -> str:
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=30,
                               cwd=str(VERSION_FILE.parent))
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    log = _run(["git", "log", "--oneline", f"{last_commit}..HEAD"]) if last_commit else ""
    if not log:
        log = _run(["git", "log", "--oneline", "-20"])
    return log


def main() -> int:
    parser = argparse.ArgumentParser(description="升 version.json 版本号（发版第一步）")
    parser.add_argument("version", help="新版本号，如 1.1.0")
    parser.add_argument("--notes", default="",
                        help="变更汇总；不填则自动取上个版本以来的 git 提交记录")
    parser.add_argument("--min-compatible", default="", help="最低兼容版本（破坏性改动时指定）")
    args = parser.parse_args()

    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[new-version] 读 {VERSION_FILE} 失败: {e}", file=sys.stderr)
        return 1
    old_version = str(data.get("version") or "?")

    # 变更说明：手填优先，否则自动 = 上个发版以来的提交记录
    notes = args.notes.strip() or git_log_since(str(data.get("commit") or ""))

    try:
        data = apply_bump(data, args.version, args.min_compatible.strip(), notes)
    except ValueError as e:
        print(f"[new-version] {e}", file=sys.stderr)
        return 1
    head = current_commit()
    if head:
        data["commit"] = head   # 下次发版从这里算变更区间
    VERSION_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[new-version] v{old_version} → v{args.version} 已写入 version.json")
    if not args.notes.strip():
        print(f"[new-version] 变更说明（自动取自 git，共 {len(notes.splitlines())} 条提交）：")
        for line in notes.splitlines()[:10]:
            print(f"    {line}")
    print("接下来：")
    print("  1. git add version.json && git commit -m 'chore(release): v%s'" % args.version)
    print("  2. docker compose up -d --build        # 本机构建并运行新版本")
    print("  3. 运维页 → 升级包分发 → 一键导出升级包  # 产出 aether-update-%s.tar.gz" % args.version)
    print("  4. 下载发给对方 → 对方放进 Aether/backups/ 即自动升级")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
