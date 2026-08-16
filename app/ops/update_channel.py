"""在线检查更新与一键升级（运维页「版本与升级」的在线通道）。

更新源是任意静态 HTTP 地址（OSS / COS / GitHub Releases / 自建 nginx 都行），
放两个文件，都由 scripts/build-update-pack.py 产出、发布时直接拷贝：

- update-channel.json —— 渠道清单：最新版本号、升级包文件名、整包 sha256、大小
- aether-update-<版本>.tar.gz —— 升级包本身

config.json 的 update.manifest_url 存渠道清单地址（运维页可填，先留空后补）。

两层校验互不替代：渠道清单里的 pack_sha256 只管「下载传输是否完整」，
包内部 manifest 的镜像 sha256 + min_compatible 由 upgrade.verify_pack 把关。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import urljoin

import httpx

from ..core.config import get_config, update_config_section
from ..core.exceptions import AppException
from ..core.version import get_version
from . import audit, upgrade

logger = logging.getLogger(__name__)

MANIFEST_MAX_BYTES = 1024 * 1024        # 渠道清单很小，超 1MB 视为异常响应
FETCH_TIMEOUT = httpx.Timeout(10.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)


def get_manifest_url() -> str:
    return str(get_config("update.manifest_url", "") or "").strip()


def set_manifest_url(url: str) -> str:
    url = (url or "").strip()
    if url:
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise AppException(
                "更新源地址必须是 http(s) URL", code="update_bad_url", http_status=400
            )
    update_config_section("update", {"manifest_url": url})
    return url


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


async def fetch_channel_manifest() -> dict:
    """拉取并校验渠道清单。任何问题都抛 AppException（前端展示 message）。"""
    url = get_manifest_url()
    if not url:
        raise AppException("尚未配置更新源地址", code="update_not_configured", http_status=400)
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.content
    except httpx.HTTPError as e:
        raise AppException(
            f"更新源不可达：{e}", code="update_fetch_failed", http_status=502
        ) from e
    if len(raw) > MANIFEST_MAX_BYTES:
        raise AppException("渠道清单响应异常（超过 1MB）", code="update_bad_manifest", http_status=502)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise AppException(
            "渠道清单不是合法 JSON（应为 build-update-pack.py 产出的 update-channel.json）",
            code="update_bad_manifest", http_status=502,
        ) from e
    if not isinstance(data, dict) or not data.get("version") or not data.get("pack"):
        raise AppException(
            "渠道清单缺少 version/pack 字段", code="update_bad_manifest", http_status=502
        )
    return data


def resolve_pack_url(channel: dict) -> str:
    """pack 为文件名时相对渠道清单地址解析；为完整 URL 时原样使用。"""
    pack = str(channel["pack"])
    if pack.startswith(("http://", "https://")):
        return pack
    return urljoin(get_manifest_url(), pack)


async def check_update() -> dict:
    """比对渠道版本与当前版本。只读操作，不写审计。"""
    if not get_manifest_url():
        return {"status": "not_configured", "current": get_version()}
    try:
        channel = await fetch_channel_manifest()
    except AppException as e:
        return {"status": "error", "current": get_version(), "message": e.message}

    current = get_version()
    latest = str(channel["version"])
    min_compat = str(channel.get("min_compatible") or latest)
    result = {
        "current": current,
        "latest": {
            "version": latest,
            "min_compatible": min_compat,
            "notes": str(channel.get("notes") or ""),
            "size_bytes": channel.get("size_bytes") or 0,
            "created_at": channel.get("created_at") or "",
        },
        "docker_socket": upgrade.DOCKER_SOCK.exists(),
    }
    if _version_key(latest) <= _version_key(current):
        return {**result, "status": "up_to_date"}
    if _version_key(min_compat) > _version_key(current):
        # 有新版但当前版本太旧，跨不了：提示走中间版本，与 verify_pack 口径一致
        return {
            **result,
            "status": "incompatible",
            "message": f"新版本 v{latest} 要求最低兼容 v{min_compat}，当前 v{current} 需先升级中间版本",
        }
    return {**result, "status": "available"}


async def download_and_apply(operator: str) -> dict:
    """一键升级：拉渠道清单 → 下载包 → 整包 sha256 抽检 → 复用 apply_upgrade。"""
    channel = await fetch_channel_manifest()
    pack_url = resolve_pack_url(channel)
    expect_sha = str(channel.get("pack_sha256") or "").strip().lower()
    size_hint = int(channel.get("size_bytes") or 0)
    if size_hint and size_hint > upgrade.MAX_PACK_BYTES:
        raise AppException(
            f"升级包大小（{size_hint / 1024**3:.1f}GB）超过上限",
            code="update_pack_too_large", http_status=400,
        )

    tmp_dir = upgrade.make_temp_pack_dir()
    tmp_file = Path(pack_url.split("?")[0].rstrip("/").split("/")[-1] or "pack.tar.gz")
    tmp_file = tmp_dir / (tmp_file.name if tmp_file.name.endswith(".tar.gz") else "pack.tar.gz")
    try:
        h = hashlib.sha256()
        received = 0
        try:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                async with client.stream("GET", pack_url) as resp:
                    resp.raise_for_status()
                    with tmp_file.open("wb") as f:
                        async for chunk in resp.aiter_bytes(8 * 1024 * 1024):
                            received += len(chunk)
                            if received > upgrade.MAX_PACK_BYTES:
                                raise AppException(
                                    "下载超出 4GB 上限，已中止", code="update_pack_too_large", http_status=400
                                )
                            f.write(chunk)
                            h.update(chunk)
        except httpx.HTTPError as e:
            raise AppException(
                f"下载升级包失败：{e}", code="update_download_failed", http_status=502
            ) from e

        if expect_sha and h.hexdigest() != expect_sha:
            raise AppException(
                "下载的升级包 sha256 与渠道清单不一致（文件损坏或被篡改），已中止",
                code="update_sha_mismatch", http_status=400,
            )
        if not expect_sha:
            logger.warning(
                "Channel manifest has no pack_sha256; skipped transfer integrity check (%s)", pack_url
            )

        audit.record(operator, "update_channel_apply", {
            "url": pack_url, "size_bytes": received,
            "to_version": str(channel["version"]),
        })
        # 包内校验（镜像 sha256 + min_compatible）→ load → tag latest → 重启
        return await upgrade.apply_upgrade(tmp_file, operator)
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
