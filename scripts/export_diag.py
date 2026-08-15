#!/usr/bin/env python3
"""命令行版诊断包导出（09 清单条目 1）。

树莓派上直接运行（与界面按钮同一套脱敏逻辑）：

    python scripts/export_diag.py                    # 当前目录生成 aether-diag-*.zip
    python scripts/export_diag.py -o /tmp/diag.zip   # 指定输出路径

在仓库根目录运行；无网络依赖，离线可用。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Aether 脱敏诊断包导出")
    parser.add_argument("-o", "--output", default="", help="输出 zip 路径（默认当前目录）")
    args = parser.parse_args()

    from app.ops.diag import build_diagnostic_package

    data, filename = await build_diagnostic_package(operator="cli")
    out = Path(args.output) if args.output else Path.cwd() / filename
    out.write_bytes(data)
    print(f"诊断包已生成: {out} ({len(data) / 1024:.1f} KB)")
    print("密钥与个人信息已脱敏，可直接发给支持人员。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
