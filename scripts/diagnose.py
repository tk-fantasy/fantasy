#!/usr/bin/env python3
"""Aether 部署体检 CLI（09 清单条目 3）—— 主机模式兜底工具。

检查逻辑在 app/ops/diagnose.py（与前端运维页 POST /api/ops/diagnose 共享），
本脚本只在应用起不来 / 首次部署时在主机上直接运行。

用法（仓库根目录）：
    python scripts/diagnose.py             # 体检并生成 HTML/JSON 报告
    python scripts/diagnose.py --json-only # 只输出 JSON 到 stdout
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ops.diagnose import run_all

STATUS_LABEL = {"pass": "✅ 通过", "warn": "⚠️ 警告", "fail": "❌ 失败"}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Aether 部署体检报告 {ts}</title>
<style>
body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#222}}
h1{{font-size:20px}} .meta{{color:#666;font-size:13px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td,th{{padding:8px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
tr.pass td:first-child{{color:#1a7f37}} tr.warn td:first-child{{color:#b8860b}} tr.fail td:first-child{{color:#c62828}}
.advice{{color:#555;font-size:13px}}
</style></head><body>
<h1>Aether 部署体检报告</h1>
<p class="meta">{ts} · {env} 模式 · {platform} · Python {pyver}</p>
<table><tr><th style="width:26%">检查项</th><th style="width:12%">结果</th><th>详情 / 怎么办</th></tr>
{rows}
</table>
<p class="meta">汇总：{n_pass} 通过 · {n_warn} 警告 · {n_fail} 失败</p>
</body></html>"""


def render_html(report: dict) -> str:
    rows = []
    for c in report["checks"]:
        advice = f'<div class="advice">怎么办：{c["advice"]}</div>' if c.get("advice") else ""
        rows.append(
            f'<tr class="{c["status"]}"><td>{c["name"]}</td>'
            f'<td>{STATUS_LABEL[c["status"]]}</td>'
            f'<td>{c["detail"]}{advice}</td></tr>'
        )
    s = report["summary"]
    return HTML_TEMPLATE.format(
        ts=report["created_at"], env=report["environment"], platform=report["platform"],
        pyver=sys.version.split()[0], rows="\n".join(rows),
        n_pass=s["pass"], n_warn=s["warn"], n_fail=s["fail"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Aether 部署体检")
    parser.add_argument("--json-only", action="store_true", help="仅输出 JSON 到 stdout，不写文件")
    parser.add_argument("-o", "--outdir", default=".", help="报告输出目录（默认当前目录）")
    args = parser.parse_args()

    report = run_all()

    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for c in report["checks"]:
            line = f'{STATUS_LABEL[c["status"]]}  {c["name"]}: {c["detail"]}'
            if c.get("advice"):
                line += f'\n      ↳ 怎么办: {c["advice"]}'
            print(line)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        outdir = Path(args.outdir)
        json_path = outdir / f"diagnose-report-{stamp}.json"
        html_path = outdir / f"diagnose-report-{stamp}.html"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        html_path.write_text(render_html(report), encoding="utf-8")
        s = report["summary"]
        print(f"\n报告已生成: {html_path} / {json_path}")
        print(f"汇总: {s['pass']} 通过 · {s['warn']} 警告 · {s['fail']} 失败")

    return 1 if report["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
