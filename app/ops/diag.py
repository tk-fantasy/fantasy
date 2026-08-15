"""诊断包导出（09 清单条目 1）。

一键打包脱敏后的运行状态为 zip，客户传回即可远程排障：
- config/config_sanitized.json — config.json 脱敏版（密钥打码 + 个人信息占位）
- system/system_info.json     — 平台/版本/磁盘/内存；docker.json — 容器状态
- logs/*.log                  — 最近日志（每文件取尾部，总量封顶）
- README.txt                  — 内容与脱敏范围说明

脱敏范围（交付约定）：密钥类字段（token/api_key/password/private_key/secret）
与个人信息（家庭住址、户主称呼、设备 MAC）必须脱敏；设备名、实体 ID、
IP 地址、日志内容保留（排障必需）。

同一逻辑供 API（/api/ops/diagnostics）与命令行（scripts/export_diag.py）
共用，保证两种入口脱敏一致。
"""
from __future__ import annotations

import io
import json
import logging
import platform
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import BASE_DIR
from . import audit

logger = logging.getLogger(__name__)

LOGS_DIR = BASE_DIR / "logs"
DOCKER_SOCK = Path("/var/run/docker.sock")

# 单文件日志取尾部上限与整包日志总量上限
PER_LOG_TAIL_BYTES = 2 * 1024 * 1024
TOTAL_LOG_BUDGET = 10 * 1024 * 1024

# 字段名含这些关键词 → 值打码（大小写不敏感，匹配任意层级）
SENSITIVE_KEY_MARKERS = ("token", "api_key", "private_key", "password", "secret", "authorization")

# home 段的个人信息字段 → 整体替换为占位符（键名保留，便于看出配置过）
PII_HOME_FIELDS = ("home_name", "owner_name", "province", "city", "district", "address")

# 其他散落的个人信息字段：段名 → 字段名集合
PII_FIELDS_BY_SECTION: dict[str, set[str]] = {
    "vision": {"device_mac"},
    "home": set(PII_HOME_FIELDS),
}


def mask_value(value: Any) -> str:
    """凭证打码：长值保留首尾各 4 字符（便于人工核对是否填错），短值全遮。"""
    s = str(value)
    if len(s) > 12:
        return f"{s[:4]}****{s[-4:]}"
    return "****"


def sanitize_config(cfg: dict[str, Any], section: str = "") -> dict[str, Any]:
    """深度遍历 config，密钥字段打码、个人信息占位；保留其余结构与值。"""
    result: dict[str, Any] = {}
    pii_fields = PII_FIELDS_BY_SECTION.get(section, set())
    for key, value in cfg.items():
        key_lower = str(key).lower()
        if isinstance(value, dict):
            result[key] = sanitize_config(value, section=key)
        elif isinstance(value, list):
            # llm_keys 数组里只有 api_key_env 变量名（无明文），但仍按元素脱敏防御性处理
            result[key] = [
                sanitize_config(item, section=section) if isinstance(item, dict) else item
                for item in value
            ]
        elif any(marker in key_lower for marker in SENSITIVE_KEY_MARKERS):
            result[key] = mask_value(value) if value else value
        elif key in pii_fields and value:
            result[key] = "[已脱敏]"
        else:
            result[key] = value
    return result


def collect_system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "node": platform.node(),
    }
    try:
        usage = shutil.disk_usage(BASE_DIR)
        info["disk_free_gb"] = round(usage.free / 1024**3, 2)
        info["disk_total_gb"] = round(usage.total / 1024**3, 2)
    except OSError:
        pass
    info["mem_total_mb"], info["mem_available_mb"] = _memory_info()
    info["cpu_count"] = __import__("os").cpu_count()
    return info


def _memory_info() -> tuple[Any, Any]:
    """内存信息：Linux 读 /proc/meminfo；Windows 用 ctypes；失败返回 (None, None)。"""
    try:
        meminfo = {}
        for line in (Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()):
            name, _, rest = line.partition(":")
            meminfo[name.strip()] = int(rest.strip().split()[0]) // 1024  # kB → MB
        return meminfo.get("MemTotal"), meminfo.get("MemAvailable")
    except OSError:
        pass
    try:
        import ctypes
        import ctypes.wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.wintypes.DWORD),
                ("dwMemoryLoad", ctypes.wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
            ] + [(f"_pad{i}", ctypes.c_uint64) for i in range(6)]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullTotalPhys // 1024**2, stat.ullAvailPhys // 1024**2
    except Exception:
        return None, None


async def collect_docker_status() -> dict[str, Any] | None:
    """docker ps（容器名/状态/镜像），socket 不可用返回 None。复用 simulator 的 UDS 方案。"""
    if not DOCKER_SOCK.exists():
        return None
    import httpx

    try:
        transport = httpx.AsyncHTTPTransport(uds=str(DOCKER_SOCK))
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            resp = await client.get(
                "http://localhost/containers/json",
                params={"all": "1", "limit": "50"},
            )
            resp.raise_for_status()
            return [
                {
                    "name": (c.get("Names") or [""])[0].lstrip("/"),
                    "state": c.get("State"),
                    "image": c.get("Image"),
                }
                for c in resp.json()
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("diag docker ps failed: %s", exc)
        return {"error": str(exc)}


def collect_log_files(budget: int = TOTAL_LOG_BUDGET) -> list[tuple[str, bytes]]:
    """logs/ 下的日志按 mtime 新→旧取尾部，总量不超 budget。

    返回 (文件名, 尾部内容 bytes)。不含 audit 子目录（其中可能有操作人
    用户名，且排障用不上）。
    """
    if not LOGS_DIR.exists():
        return []
    files = sorted(
        (p for p in LOGS_DIR.iterdir() if p.is_file() and p.suffix in (".log", ".log.1", ".txt")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    collected: list[tuple[str, bytes]] = []
    remaining = budget
    for path in files:
        if remaining <= 0:
            break
        try:
            size = path.stat().st_size
            take = min(size, PER_LOG_TAIL_BYTES, remaining)
            with path.open("rb") as f:
                if size > take:
                    f.seek(size - take)
                data = f.read(take)
        except OSError:
            continue
        collected.append((path.name, data))
        remaining -= take
    return collected


README_TEXT = """Aether 诊断包
================

内容：
- config/config_sanitized.json  脱敏后的系统配置
- system/system_info.json       系统信息（平台/磁盘/内存/CPU）
- system/docker.json            Docker 容器状态（如可用）
- logs/                         最近日志（每文件取尾部，总量封顶 10MB）

脱敏范围：
- 密钥类字段（token / api_key / password / private_key / secret）已打码
- 个人信息（家庭名称、户主称呼、住址、设备 MAC）已替换为 [已脱敏]
- 设备名、实体 ID、内网 IP 与日志内容保留（远程排障必需）

生成时间：{ts}
操作人：{operator}
"""


async def build_diagnostic_package(operator: str = "unknown") -> tuple[bytes, str]:
    """生成诊断包 zip（内存中），并写审计记录。返回 (zip_bytes, filename)。"""
    ts = datetime.now(timezone.utc)
    manifest = {
        "app": "aether",
        "created_at": ts.isoformat(timespec="seconds"),
        "operator": operator,
        "sanitization": "keys_masked+pii_redacted",
    }
    docker_status = await collect_docker_status()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        sanitized = sanitize_config(_full_config())
        zf.writestr(
            "config/config_sanitized.json",
            json.dumps(sanitized, ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "system/system_info.json",
            json.dumps(collect_system_info(), ensure_ascii=False, indent=2),
        )
        if docker_status is not None:
            zf.writestr(
                "system/docker.json",
                json.dumps(docker_status, ensure_ascii=False, indent=2),
            )
        for name, data in collect_log_files():
            zf.writestr(f"logs/{name}", data)
        zf.writestr("README.txt", README_TEXT.format(ts=ts.isoformat(), operator=operator))

    filename = f"aether-diag-{ts.strftime('%Y%m%d-%H%M%S')}.zip"
    audit.record(
        operator,
        "diag_export",
        {"filename": filename, "size_bytes": buf.getbuffer().nbytes},
    )
    return buf.getvalue(), filename


def _full_config() -> dict[str, Any]:
    """读 config.json 原始内容（不经 env override，诊断只关心落盘配置）。"""
    from ..core.config import CONFIG_PATH

    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
