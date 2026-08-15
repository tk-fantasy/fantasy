"""数据出网策略服务（09 清单条目 4：模型出网策略与双模式声明）。

三档模式（config.json egress_policy.mode）：
- cloud  云端模式（默认，现状）：对话文本经 HTTPS 发往云端模型厂商；
        摄像头画面与设备控制指令不出局域网
- hybrid 混合模式：对话走云端，视觉/向量等敏感角色应指向内网模型端点
- local  纯内网模式：全部模型端点必须在内网（如 Ollama/vLLM），断网可用

模式切换是声明 + 提示层面的（徽标/警告），不拦截请求：用户把 chat 指向
公网但选了 local 时，状态接口返回 warning，由前端和交付验收发现。

声明确认（引导页"我已知晓"）记录 sha256 摘要 + 时间 + 操作人到数据库
kv（egress_confirm），作为交付留痕；确认记录不随模式切换失效，但切换
后 GET 会提示"当前模式与已确认声明不一致"。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..core.config import get_config, update_config_section
from ..core.exceptions import AppException

logger = logging.getLogger(__name__)

MODES = ("cloud", "hybrid", "local")
MODE_LABELS = {"cloud": "云端对话", "hybrid": "混合模式", "local": "纯内网"}

# 声明文案版本：文案实质变化时 +1，旧确认记录视为需要重新确认
DECLARATION_VERSION = 1

# local 模式下所有角色端点都必须在内网；hybrid 只约束敏感角色
SENSITIVE_ROLES = ("vision", "embed", "stt")

CONFIRM_KV_KEY = "egress_confirm"


def get_mode() -> str:
    mode = str(get_config("egress_policy.mode", "cloud") or "cloud")
    return mode if mode in MODES else "cloud"


def set_mode(mode: str) -> str:
    if mode not in MODES:
        raise AppException(
            f"mode 必须是 {'/'.join(MODES)} 之一", code="egress_invalid_mode", http_status=400
        )
    update_config_section("egress_policy", {"mode": mode})
    logger.info("Egress policy mode set to %s", mode)
    return mode


def is_private_host(hostname: str) -> bool:
    """判断主机是否属于内网。

    - IP 字面量：私网/回环/链路本地/CGNAT 段
    - localhost / 单标签主机名（无点）：视为内网（Docker 服务名如 ollama、homeassistant）
    - 带域名的主机名：视为公网（不做 DNS 解析，避免状态接口被慢解析拖住）
    """
    hostname = (hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return "." not in hostname
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip in ipaddress.ip_network("100.64.0.0/10")  # Tailscale/CGNAT
    )


def _classify_url(base_url: str) -> dict:
    host = urlparse((base_url or "").strip()).hostname or ""
    return {
        "base_url": base_url or "",
        "host": host,
        "private": is_private_host(host) if host else None,  # None = 未配置
    }


def endpoint_report() -> list[dict]:
    """按角色报告当前生效模型端点的内外网归属（不解析密钥）。"""
    providers = get_config("providers", {}) or {}
    keys = {k.get("id"): k for k in (get_config("llm_keys", []) or [])}
    report = []
    for role in ("chat", "summary", "vision", "embed", "stt"):
        provider = providers.get(role) or {}
        entry = keys.get(provider.get("key_id"))
        base_url = entry.get("base_url", "") if entry else ""
        item = {"role": role, **_classify_url(base_url)}
        item["configured"] = bool(base_url)
        report.append(item)
    return report


def warnings_for(report: list[dict], mode: str) -> list[str]:
    """当前模式与端点配置的矛盾提示（不拦截，供前端与验收参考）。"""
    result = []
    if mode == "local":
        public = [r["role"] for r in report if r["configured"] and r["private"] is False]
        if public:
            result.append(
                f"纯内网模式下以下角色仍指向公网端点：{'、'.join(public)}，"
                "断网后对应功能不可用，请改指向内网模型（如 Ollama）"
            )
    elif mode == "hybrid":
        public = [
            r["role"] for r in report
            if r["role"] in SENSITIVE_ROLES and r["configured"] and r["private"] is False
        ]
        if public:
            result.append(
                f"混合模式下敏感角色（{'、'.join(public)}）建议指向内网端点，"
                "当前仍为公网"
            )
    return result


def _confirm_hash(mode: str, username: str, ts: str) -> str:
    return hashlib.sha256(
        f"v{DECLARATION_VERSION}|{mode}|{username}|{ts}".encode("utf-8")
    ).hexdigest()


async def confirm_declaration(mode: str, username: str) -> dict:
    """记录声明确认（hash + 时间 + 操作人）到数据库 kv。"""
    if mode not in MODES:
        raise AppException(
            f"mode 必须是 {'/'.join(MODES)} 之一", code="egress_invalid_mode", http_status=400
        )
    from ..core.database import Database

    record = {
        "mode": mode,
        "version": DECLARATION_VERSION,
        "confirmed_by": username,
        # ISO 格式带时区，交付留痕可直接读
        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    record["hash"] = _confirm_hash(mode, username, record["confirmed_at"])
    db = Database.get()
    await db.kv_set(CONFIRM_KV_KEY, json.dumps(record, ensure_ascii=False))
    logger.info("Egress declaration confirmed by %s for mode %s", username, mode)
    return record


async def get_confirm_record() -> dict | None:
    """读取已确认的声明记录；未确认或 DB 未就绪返回 None。"""
    try:
        from ..core.database import Database

        db = Database.get()
        raw = await db.kv_get(CONFIRM_KV_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def policy_status() -> dict:
    """GET /api/egress 的聚合返回。"""
    mode = get_mode()
    report = endpoint_report()
    record = await get_confirm_record()
    confirmed = bool(record and record.get("version") == DECLARATION_VERSION)
    notes = []
    if record and record.get("version") != DECLARATION_VERSION:
        notes.append("声明文案已更新，请重新确认")
    if record and record.get("mode") != mode:
        notes.append(
            f"当前模式（{MODE_LABELS.get(mode, mode)}）与已确认声明"
            f"（{MODE_LABELS.get(record.get('mode'), '?')}）不一致，请重新确认"
        )
    return {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "modes": [{"mode": m, "label": MODE_LABELS[m]} for m in MODES],
        "confirmed": confirmed,
        "confirmed_at": record.get("confirmed_at") if record else None,
        "confirmed_by": record.get("confirmed_by") if record else None,
        "endpoints": report,
        "warnings": warnings_for(report, mode),
        "notes": notes,
        "declaration_version": DECLARATION_VERSION,
    }
