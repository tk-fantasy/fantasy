"""部署体检核心（09 清单条目 3 的共享实现）。

被两处复用：
- app/ops/diagnose.py::run_all()           —— /api/ops/diagnose（前端运维页按钮）
- scripts/diagnose.py                       —— 主机 CLI（应用起不来时的兜底）

容器内感知：aether 容器里没有 HA/MQTT（它们是隔壁容器），端口检查用
compose 服务名（homeassistant/mqtt）而非 127.0.0.1；本进程端口仍查
127.0.0.1。NTP：容器内没有 timedatectl/w32tm，改用 HTTP Date 头与
可信站点对时（容器与宿主共享内核时钟，偏差即宿主偏差）。

每项检查返回 {name, status: pass|warn|fail, detail, advice}。
"""
from __future__ import annotations

import concurrent.futures
import json
import platform
import shutil
import socket
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from ..core.config import BASE_DIR

CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "app" / "data" / "aether.db"

PASS, WARN, FAIL = "pass", "warn", "fail"

IN_CONTAINER = Path("/.dockerenv").exists()

# (标签, host, port)：容器内走 compose 服务名，主机模式全走 127.0.0.1
SERVICE_TARGETS = (
    [("aether 后端", "127.0.0.1", 8010), ("Home Assistant", "homeassistant", 8123),
     ("MQTT (mosquitto)", "mqtt", 1884), ("启动进度页", "127.0.0.1", 8011)]
    if IN_CONTAINER else
    [("aether 后端", "127.0.0.1", 8010), ("Home Assistant", "127.0.0.1", 8123),
     ("MQTT (mosquitto)", "127.0.0.1", 1884), ("启动进度页", "127.0.0.1", 8011)]
)

MIN_DISK_GB = 2.0
MIN_MEM_MB = 2048
TIME_SKEW_TOLERANCE_SEC = 90


def _result(name, status, detail, advice=""):
    return {"name": name, "status": status, "detail": detail, "advice": advice}


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ==================== 端口 ====================

def check_ports() -> list[dict]:
    results = []
    for label, host, port in SERVICE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=2):
                results.append(_result(f"端口 {port}（{label}）", PASS, "已监听"))
        except OSError:
            results.append(_result(
                f"端口 {port}（{label}）", FAIL, "未监听",
                advice="docker compose ps 看该服务是否起来；首次部署先 docker compose up -d --build",
            ))
    return results


# ==================== HA ====================

def check_ha(config: dict) -> list[dict]:
    url = (config.get("ha", {}).get("url") or "").strip()
    if not url:
        return [_result("Home Assistant 地址", WARN, "config.json 未配置 ha.url",
                        advice="完成初始引导或高级设置里配置 HA")]
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


# ==================== 摄像头 RTSP（两层判定） ====================

def _load_cameras(config: dict) -> list[tuple[str, str]]:
    """(名称, rtsp_url)：多路摄像头表优先，回退旧版单摄配置。"""
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
        # 第一层：TCP。不通即网络问题，与凭据无关
        if not _tcp_reachable(host, port):
            results.append(_result(label, FAIL, f"TCP {host}:{port} 不通（网络层不可达）",
                                   advice="检查摄像头通电/网线、IP 是否漂移（DHCP 续租后常见）、VLAN 是否隔离"))
            continue
        # 第二层：取流。网络通而取不到 → 凭据错或流格式不支持
        if cv2 is None:
            results.append(_result(label, WARN, f"{host}:{port} 网络通；未安装 opencv，跳过取流深检",
                                   advice="在应用 venv/容器内运行可深检（opencv-python)"))
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


# ==================== DNS ====================

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


# ==================== 资源 ====================

def check_resources() -> list[dict]:
    results = []
    try:
        usage = shutil.disk_usage(BASE_DIR)
        free_gb = usage.free / 1024**3
        if free_gb >= MIN_DISK_GB:
            results.append(_result("磁盘剩余", PASS, f"{free_gb:.1f} GB"))
        else:
            results.append(_result("磁盘剩余", FAIL, f"仅 {free_gb:.1f} GB（<{MIN_DISK_GB}GB）",
                                   advice="清理 docker 镜像（docker image prune）与日志 logs/"))
    except OSError as e:
        results.append(_result("磁盘剩余", WARN, f"无法读取：{e}"))

    total_mb, _avail = _memory()
    if total_mb is None:
        results.append(_result("内存", WARN, "无法读取（非 Linux/Windows 或权限受限）"))
    elif total_mb >= MIN_MEM_MB:
        results.append(_result("内存", PASS, f"{total_mb} MB"))
    else:
        results.append(_result("内存", FAIL, f"{total_mb} MB（<{MIN_MEM_MB}MB）",
                               advice="Aether + HA + 视觉推理至少需要 2GB；关停无关容器或换内存更大的设备"))

    import os

    arch = platform.machine() or "未知"
    if "arm" in arch.lower() or "aarch64" in arch.lower():
        note = "；ARM 设备请在主机跑 scripts/check_arm_backend.py 验证 opencv 后端"
    else:
        note = ""
    results.append(_result("CPU 架构", PASS, arch + note + (f"；CPU {os.cpu_count()} 核" if os.cpu_count() else "")))
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


# ==================== 时间同步 ====================

def check_clock() -> list[dict]:
    """时间校验：先试系统工具（主机模式），容器内/工具不可用时用 HTTP Date 对时。"""
    import subprocess

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
            if needle.lower() in text.lower():
                return [_result("NTP 时间同步", PASS, "已同步")]
            return [_result("NTP 时间同步", WARN, "显示未同步",
                            advice="Linux: timedatectl set-ntp true；Windows: w32tm /resync")]
        except (OSError, subprocess.TimeoutExpired):
            continue
    tool_unavailable = True

    # HTTP Date 对时：容器内没有 NTP 工具，用可信站点的响应头比对本地时钟
    for url in ("http://www.baidu.com", "http://www.qq.com"):
        try:
            req = urllib.request.Request(url, method="HEAD")
            resp = urllib.request.urlopen(req, timeout=4)
            date_header = resp.headers.get("Date")
            if not date_header:
                continue
            from email.utils import parsedate_to_datetime

            remote = parsedate_to_datetime(date_header).timestamp()
            skew = abs(remote - datetime.now(timezone.utc).timestamp())
            if skew <= TIME_SKEW_TOLERANCE_SEC:
                return [_result("NTP 时间同步", PASS, f"已同步（偏差 {skew:.0f}s，经 HTTP 对时）")]
            return [_result("NTP 时间同步", FAIL, f"系统时间偏差 {skew / 60:.0f} 分钟",
                            advice="树莓派无 RTC，依赖 NTP：联网后 timedatectl set-ntp true；时间不准会导致 HTTPS 证书校验失败")]
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return [_result("NTP 时间同步", WARN, "无法校时（无 NTP 工具且外网不可达）",
                    advice="手动核对系统时间；断电重启后必须联网对时一次")]


def _decode_console_bytes(data: bytes) -> str:
    """控制台输出解码：中文 Windows 是 GBK，Linux 一般 UTF-8；都失败则替换非法字节。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ==================== 汇总入口 ====================

def run_all(timeout: float = 40.0) -> dict:
    """并行跑全部检查，返回报告 dict（前端渲染 / CLI 输出共用）。"""
    config = _load_config()
    checks: list[dict] = []
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
                checks.extend(fut.result(timeout=timeout))
            except Exception as e:  # noqa: BLE001
                checks.append(_result("检查组异常", FAIL, str(e)))
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": "container" if IN_CONTAINER else "host",
        "platform": platform.platform(),
        "checks": checks,
        "summary": {
            "pass": sum(1 for c in checks if c["status"] == PASS),
            "warn": sum(1 for c in checks if c["status"] == WARN),
            "fail": sum(1 for c in checks if c["status"] == FAIL),
        },
    }
