"""飞书扫码一键接入——设备授权式应用注册。

走飞书账号体系的未公开端点 accounts.feishu.cn/oauth/v1/app/registration
（OAuth Device Flow 风格）：init 校验环境 → begin 生成二维码 → poll 换取
client_id/client_secret。协议经 OpenClaw 生产验证
（extensions/feishu/src/app-registration.ts），2026-09 实测开放。

飞书若收紧该端点：本模块抛 FeishuQrSetupError，前端引导改用手动配置
（弹窗内 config_schema 表单），接入主路径不受影响。

会话只存内存：单管理员场景，重复 start 覆盖前会话；服务重启即清空。
"""

import base64
import io
import logging
import time

import httpx
import segno

logger = logging.getLogger(__name__)

_ACCOUNTS_URL = "https://accounts.feishu.cn"
_REGISTRATION_PATH = "/oauth/v1/app/registration"
_REQUEST_TIMEOUT = 10.0
_DEFAULT_INTERVAL = 5        # poll 轮询间隔（秒），begin 实际返回优先
_DEFAULT_EXPIRES_IN = 3600   # 二维码有效期（秒）
_MAX_CONSECUTIVE_ERRORS = 5  # poll 连续异常阈值，达阈值报通道故障


class FeishuQrSetupError(Exception):
    """扫码接入流程错误（message 面向最终用户可读）。"""


_session: dict | None = None
_pending_credentials: tuple[str, str] | None = None


async def _post_registration(payload: dict) -> dict:
    """POST 飞书注册端点（表单编码），返回 JSON dict。非 dict 响应视为协议异常。"""
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_ACCOUNTS_URL}{_REGISTRATION_PATH}",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise FeishuQrSetupError("飞书扫码通道响应异常，请改用手动配置")
    return data


def _qr_svg_data_url(url: str) -> str:
    """把二维码 URL 渲染成 SVG data URL（segno，与首装进度页同一技术栈）。

    在后端渲染而非前端引 QR 库：插件面板组件位于 frontend 项目根之外，
    裸 npm 导入解析不到；后端出图让插件前端保持零依赖。
    """
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


async def start_session() -> dict:
    """发起扫码会话：init 校验 + begin 取二维码。返回前端展示字段。"""
    global _session
    init_data = await _post_registration({"action": "init"})
    if "client_secret" not in (init_data.get("supported_auth_methods") or []):
        raise FeishuQrSetupError("当前飞书环境不支持扫码接入，请改用手动配置")
    begin = await _post_registration({
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    device_code = begin.get("device_code")
    qr_url = begin.get("verification_uri_complete") or begin.get("verification_uri")
    if not device_code or not qr_url:
        raise FeishuQrSetupError("飞书扫码通道响应异常，请改用手动配置")
    interval = max(1, int(begin.get("interval") or _DEFAULT_INTERVAL))
    expires_in = int(begin.get("expires_in") or _DEFAULT_EXPIRES_IN)
    _session = {
        "device_code": device_code,
        "qr_url": qr_url,
        "user_code": str(begin.get("user_code") or ""),
        "expires_at": time.monotonic() + expires_in,
        "interval": interval,
        "last_poll_ts": 0.0,
        "last_status": "pending",
        "consecutive_errors": 0,
    }
    logger.info("飞书扫码会话已建立（user_code=%s，%ds 有效）",
                _session["user_code"], expires_in)
    return {
        "qr_url": qr_url,
        "qr_svg_data_url": _qr_svg_data_url(qr_url),
        "user_code": _session["user_code"],
        "expires_in": expires_in,
        "interval": interval,
    }


async def poll_once() -> dict:
    """按协议轮询一次（内部限速）。

    返回 {status: pending|scanned|denied|expired|success|error, message?}。
    success 时凭证存入 _pending_credentials（consume_result 取走即失效），
    会话即刻关闭，杜绝二次消费。
    """
    global _session, _pending_credentials
    if _session is None:
        return {"status": "expired", "message": "扫码会话不存在，请重新获取二维码"}
    if time.monotonic() >= _session["expires_at"]:
        _session = None
        return {"status": "expired", "message": "二维码已过期，请重新获取"}
    now = time.monotonic()
    if now - _session["last_poll_ts"] < _session["interval"]:
        return {"status": _session["last_status"]}
    _session["last_poll_ts"] = now
    try:
        data = await _post_registration(
            {"action": "poll", "device_code": _session["device_code"]})
    except Exception as exc:  # noqa: BLE001 —— 网络抖动按容错计数，不打断扫码
        logger.warning("飞书扫码 poll 请求失败: %s", exc)
        return _count_poll_error()
    if not isinstance(data, dict):
        return _count_poll_error()

    err = data.get("error")
    if err == "slow_down":
        _session["interval"] += 5
        _session["last_status"] = "pending"
        return {"status": "pending"}
    if err == "authorization_pending":
        _session["last_status"] = "pending"
        return {"status": "pending"}
    if err == "access_denied":
        _session = None
        return {"status": "denied", "message": "手机端已拒绝，可重新扫码"}
    if err == "expired_token":
        _session = None
        return {"status": "expired", "message": "二维码已过期，请重新获取"}
    if data.get("client_id") and data.get("client_secret"):
        _pending_credentials = (str(data["client_id"]), str(data["client_secret"]))
        _session = None
        logger.info("飞书扫码授权成功（app_id=%s…）", str(data["client_id"])[:10])
        return {"status": "success"}
    if data.get("user_info"):  # 已扫码待确认（poll 响应携带 user_info 但尚无凭证）
        _session["last_status"] = "scanned"
        return {"status": "scanned"}
    return _count_poll_error()


def _count_poll_error() -> dict:
    """poll 异常容错：连续 <5 次按上次状态续等，达阈值报通道故障并收会话。"""
    global _session
    if _session is None:
        return {"status": "expired", "message": "扫码会话不存在，请重新获取二维码"}
    _session["consecutive_errors"] += 1
    if _session["consecutive_errors"] >= _MAX_CONSECUTIVE_ERRORS:
        _session = None
        return {"status": "error",
                "message": "飞书扫码通道异常，请稍后重试或改用手动配置"}
    return {"status": _session["last_status"]}


def consume_result() -> tuple[str, str] | None:
    """取走扫码授权凭证 (app_id, app_secret)；取走即失效，防二次消费。"""
    global _pending_credentials
    creds, _pending_credentials = _pending_credentials, None
    return creds


def cancel_session() -> None:
    """丢弃当前扫码会话与未消费凭证。"""
    global _session, _pending_credentials
    _session = None
    _pending_credentials = None
