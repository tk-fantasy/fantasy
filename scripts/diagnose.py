#!/usr/bin/env python3
"""Aether 部署体检脚本（09 清单条目 3）。

客户现场（树莓派等）一键体检：端口占用、HA/摄像头可达性、DNS、磁盘/内存、
时间同步。每项输出「通过 / 警告 / 失败 + 怎么办」三段式，同时生成：

- diagnose-report-<时间戳>.json  结构化结果（脚本/平台二次消费）
- diagnose-report-<时间戳>.html  自包含 HTML 报告（客户截图即可回传）

用法（仓库根目录）：
    python scripts/diagnose.py            # 体检并生成报告
    python scripts/diagnose.py --json-only # 只输出 JSON 到 stdout

设计：不 import app.*（独立于应用进程，应用起不来时也能体检）；
opencv 缺失时跳过 RTSP 深检（TCP 层仍检测）；单检查超时 3~5s，总时长 <30s。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "app" / "data" / "aether.db"

PASS, WARN, FAIL = "pass", "warn", "fail"
STATUS_LABEL = {PASS: "✅ 通过", WARN: "⚠️ 警告", FAIL: "❌ 失败"}

# 核心服务端口（docker-compose.yml 对外发布）
SERVICE_PORTS = [
    ("aether 后端", 8010),
    ("Home Assistant", 8123),
    ("MQTT (mosquitto)", 1884),
    ("启动进度页", 8011),
]


def _result(name, status, detail, advice=""):
    return {"name": name, "status": status, "detail": detail, "advice": advice}


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ==================== 检查项 ====================

def check_ports() -> list[dict]:
    results = []
    for label, port in SERVICE_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                results.append(_result(f"端口 {port}（{label}）", PASS, f"已监听"))
        except OSError:
            results.append(_result(
                f"端口 {port}（{label}）", FAIL, "未监听",
                advice=f"docker compose ps 看该服务是否起来；首次部署先 docker compose up -d --build",
            ))
    return results


def check_ha(config: dict) -> list[dict]:
    url = (config.get("ha", {}).get("url") or "").strip()
    if not url:
        return [_result("Home Assistant 地址", WARN, "config.json 未配置 ha.url", advice="完成初始引导或高级设置里配置 HA")]
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/", method="GET")
        urllib.request.urlopen(req, timeout=4)
        return [_result("Home Assistant 可达", PASS, f"{url} 响应正常")]
    except urllib.error.HTTPError as e:
        # 401 也证明服务活着（只是没带 token）
        return [_result("Home Assistant 可达", PASS, f"{url} 响应 HTTP {e.code}（服务在线）")]
    except (urllib.error.URLError, OSError, ValueError) as e:
        return [_result("Home Assistant 可达", FAIL, f"{url} 不可达：{e}",
                        advice="检查 HA 容器是否运行、地址是否写对（Docker 内用服务名 homeassistant:8123）")]


def _load_cameras(config: dict) -> list[tuple[str, str]]:
    """(名称, rtsp_url)：优先多路摄像头表，回退旧版单摄配置。"""
    rows = []
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
            try:
                cur = conn.execute(
                    "SELECT name, rtsp_url FROM cameras "
                    "WHERE enabled=1 AND source_type='rtsp' AND rtsp_url != ''"
                )
                rows = [(n or "(未命名)", u) for n, u in cur.fetchall()]
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    legacy = (config.get("vision", {}).get("rtsp_url") or "").strip()
    if not rows and legacy:
        rows = [("旧版单摄配置", legacy)]
    return rows


def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_cameras(config: dict) -> list[dict]:
    cameras = _load_cameras(config)
    if not cameras:
        return [_result("摄像头 RTSP", WARN, "没有启用的 RTSP 摄像头（跳过）",
                        advice="如需视觉功能，在摄像头设置里添加 RTSP 路路")]
    results = []
    cv2 = None
    try:
        import cv2  # noqa: F401
        cv2 = True
    except ImportError:
        cv2 = None

    for name, rtsp_url in cameras:
        parsed = urlparse(rtsp_url)
        host, port = parsed.hostname or "", parsed.port or 554
        label = f"摄像头「{name}」"
        if not _tcp_reachable(host, port):
            results.append(_result(label, FAIL, f"TCP {host}:{port} 不通（网络层不可达）",
                                   advice="检查摄像头通电/网线、IP 是否漂移（DHCP 续租后常见）、VLAN 是否隔离"))
            continue
        if cv2 is None:
            results.append(_result(label, WARN, f"{host}:{port} 网络通；未安装 opencv，跳过取流深检",
                                   advice="在应用 venv 里运行本脚本可深检（pip install opencv-python-headless）"))
            continue
        try:
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            ok, _ = cap.read()
            cap.release()
            if ok:
                results.append(_result(label, PASS, "取流成功"))
            else:
                results.append(_result(label, FAIL, "网络通但取流失败（多为凭据错误或流格式不支持）",
                                       advice="核对摄像头用户名/密码；用 VLC 打开该 rtsp 地址交叉验证"))
        except Exception as e:  # noqa: BLE001
            results.append(_result(label, FAIL, f"取流异常：{e}", advice="用 VLC 打开该 rtsp 地址交叉验证"))
    return results


def check_dns(config: dict) -> list[dict]:
    """云端模式依赖 DNS 解析模型厂商域名。"""
    hosts = []
    for key in config.get("llm_keys", []) or []:
        h = urlparse((key.get("base_url") or "")).hostname
        if h:
            hosts.append(h)
    weather_host = (config.get("weather", {}).get("host") or "").strip()
    if weather_host:
        hosts.append(weather_host)
    if not hosts:
        return [_result("DNS 解析", WARN, "无已配置的外部域名（纯内网部署可忽略）")]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {host: pool.submit(_resolve, host) for host in dict.fromkeys(hosts)}
        for host, fut in futures.items():
            ok, info = fut.result()
            if ok:
                results.append(_result(f"DNS：{host}", PASS, f"解析到 {info}"))
            else:
                results.append(_result(f"DNS：{host}", FAIL, f"解析失败：{info}",
                                       advice="检查路由器 DNS / 上游网络；纯内网模式可忽略"))
    return results


def _resolve(host: str) -> tuple[bool, str]:
    try:
        return True, socket.gethostbyname(host)
    except OSError as e:
        return False, str(e)


def check_resources() -> list[dict]:
    results = []
    try:
        usage = shutil.disk_usage(BASE_DIR)
        free_gb = usage.free / 1024**3
        if free_gb >= 2:
            results.append(_result("磁盘剩余", PASS, f"{free_gb:.1f} GB"))
        else:
            results.append(_result("磁盘剩余", FAIL, f"仅 {free_gb:.1f} GB（<2GB）",
                                   advice="清理 docker 镜像（docker image prune）与日志 logs/"))
    except OSError as e:
        results.append(_result("磁盘剩余", WARN, f"无法读取：{e}"))

    total_mb, _avail = _memory()
    if total_mb is None:
        results.append(_result("内存", WARN, "无法读取（非 Linux/Windows 或权限受限）"))
    elif total_mb >= 2048:
        results.append(_result("内存", PASS, f"{total_mb} MB"))
    else:
        results.append(_result("内存", FAIL, f"{total_mb} MB（<2GB）",
                               advice="Aether + HA + 视觉推理至少需要 2GB；关停无关容器或换内存更大的设备"))

    arch = platform.machine() or "未知"
    if "arm" in arch.lower() or "aarch64" in arch.lower():
        note = "；ARM 设备请先跑 scripts/check_arm_backend.py 验证 opencv 后端"
    else:
        note = ""
    results.append(_result("CPU 架构", PASS, arch + note))
    return results


def _memory() -> tuple[int | None, int | None]:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            k, _, rest = line.partition(":")
            info[k.strip()] = int(rest.strip().split()[0]) // 1024
        return info.get("MemTotal"), info.get("MemAvailable")
    except (OSError, ValueError):
        pass
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            # MEMORYSTATUSEX：2 个 DWORD + 7 个 UINT64（共 64 字节），pad 必须是 5
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("_pad", ctypes.c_uint64 * 5)]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            raise OSError("GlobalMemoryStatusEx failed")
        return stat.ullTotalPhys // 1024**2, stat.ullAvailPhys // 1024**2
    except Exception:
        return None, None


def check_clock() -> list[dict]:
    """NTP 时间同步（RTSP/HTTPS 证书对时间敏感）。"""
    cmds = [
        (["timedatectl", "show", "-p", "NTPSynchronized", "--value"], "yes"),
        (["w32tm", "/query", "/status", "/verbose"], "已同步"),
    ]
    for cmd, needle in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=5)
            if out.returncode != 0:
                continue
            text = _decode_console_bytes(out.stdout)
            synced = needle.lower() in text.lower()
            if synced:
                return [_result("NTP 时间同步", PASS, "已同步")]
            return [_result("NTP 时间同步", WARN, "显示未同步",
                            advice="Linux: timedatectl set-ntp true；Windows: w32tm /resync。时间不准会导致 HTTPS 证书校验失败")]
        except (OSError, subprocess.TimeoutExpired):
            continue
    return [_result("NTP 时间同步", WARN, "无法查询同步状态",
                    advice="手动核对系统时间；树莓派无 RTC，依赖 NTP，断电重启后必须联网对时")]


def _decode_console_bytes(data: bytes) -> str:
    """控制台输出解码：中文 Windows 是 GBK，Linux 一般 UTF-8；都失败则替换非法字节。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ==================== 报告输出 ====================

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
<p class="meta">{ts} · {platform} · Python {pyver}</p>
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
    n_pass = sum(1 for c in report["checks"] if c["status"] == PASS)
    n_warn = sum(1 for c in report["checks"] if c["status"] == WARN)
    n_fail = sum(1 for c in report["checks"] if c["status"] == FAIL)
    return HTML_TEMPLATE.format(
        ts=report["created_at"], platform=report["platform"], pyver=report["python"],
        rows="\n".join(rows), n_pass=n_pass, n_warn=n_warn, n_fail=n_fail,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Aether 部署体检")
    parser.add_argument("--json-only", action="store_true", help="仅输出 JSON 到 stdout，不写文件")
    parser.add_argument("-o", "--outdir", default=".", help="报告输出目录（默认当前目录）")
    args = parser.parse_args()

    config = _load_config()
    checks: list[dict] = []
    # 网络/IO 类并行跑，压住总时长
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(check_ports),
            pool.submit(check_ha, config),
            pool.submit(check_cameras, config),
            pool.submit(check_dns, config),
            pool.submit(check_resources),
            pool.submit(check_clock),
        ]
        for fut in futures:
            try:
                checks.extend(fut.result(timeout=40))
            except Exception as e:  # noqa: BLE001
                checks.append(_result("检查组异常", FAIL, str(e)))

    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "checks": checks,
    }
    n_fail = sum(1 for c in checks if c["status"] == FAIL)
    n_warn = sum(1 for c in checks if c["status"] == WARN)

    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for c in checks:
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
        print(f"\n报告已生成: {html_path} / {json_path}")
        print(f"汇总: {len(checks) - n_fail - n_warn} 通过 · {n_warn} 警告 · {n_fail} 失败")

    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
