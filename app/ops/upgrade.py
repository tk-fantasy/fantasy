"""离线升级的应用侧实现（运维页「版本与升级」按钮的后端）。

流程与 scripts/upgrade.sh 等价，但全部经 Docker Engine API（unix socket）完成：
1. 校验升级包：manifest.json（版本/sha256/min_compatible）+ 镜像 tar 哈希
2. 版本兼容检查：包的 min_compatible > 当前 version.json → 拒绝
3. docker load 镜像（aether-app:<ver>）
4. docker tag aether-app:<ver> → aether-app:latest（compose 固定引用 latest）
5. 延迟 restart aether 容器 → 响应先送达，容器由 Docker 拉起新版本

前置依赖：compose 挂载 /var/run/docker.sock（虚拟设备开关同款前提），
image 字段固定 aether-app:latest。升级历史追加到 backups/upgrade-history.jsonl。
"""
from __future__ import annotations

import hashlib
import json
import logging
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..core.config import BASE_DIR
from ..core.version import get_version
from . import audit

logger = logging.getLogger(__name__)

DOCKER_SOCK = Path("/var/run/docker.sock")
HISTORY_FILE = BASE_DIR / "backups" / "upgrade-history.jsonl"
CONTAINER_NAME = "aether"
IMAGE_REPO = "aether-app"

MAX_PACK_BYTES = 4 * 1024**3  # 4GB 上限，防误传超大文件占满磁盘


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


def verify_pack(pack_path: Path) -> dict:
    """校验升级包：结构 + sha256 + 版本兼容。返回 manifest（不通过则抛异常）。"""
    if pack_path.stat().st_size > MAX_PACK_BYTES:
        raise ValueError("升级包超过 4GB 上限")
    try:
        with tarfile.open(pack_path, "r:gz") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read().decode("utf-8"))
            images = manifest.get("images") or []
            if not images or not images[0].get("sha256"):
                raise ValueError("manifest.json 缺少镜像校验信息")
            image_meta = images[0]
            image_name = image_meta.get("file") or "images/aether.tar"
            if image_name.startswith("/") or ".." in image_name.split("/"):
                raise ValueError(f"manifest 镜像路径非法: {image_name}")

            # sha256 校验对象是包内的镜像 tar 成员（与 build-update-pack.py /
            # upgrade.sh 的语义一致，不是整个包文件的哈希）
            fobj = tf.extractfile(image_name)
            if fobj is None:
                raise ValueError(f"包内缺镜像文件: {image_name}")
            h = hashlib.sha256()
            for chunk in iter(lambda: fobj.read(8 * 1024 * 1024), b""):
                h.update(chunk)
    except (tarfile.TarError, KeyError, json.JSONDecodeError) as e:
        # 注：函数内主动抛的 ValueError 不属于上述类型，直接向上传播
        raise ValueError(f"升级包结构不合法（缺 manifest.json 或损坏）：{e}") from e

    if h.hexdigest() != image_meta["sha256"]:
        raise ValueError("升级包校验失败（sha256 不匹配，文件可能传输出错）")

    # 版本兼容：低于最低兼容版本拒绝
    min_compat = manifest.get("min_compatible", manifest["version"])
    current = get_version()
    if _version_key(min_compat) > _version_key(current):
        raise ValueError(
            f"当前版本 {current} 低于升级包要求的最低兼容版本 {min_compat}，"
            "请联系支持获取中间版本"
        )
    return manifest


async def _docker(method: str, path: str, timeout: float = 300.0, **kw) -> httpx.Response:
    if not DOCKER_SOCK.exists():
        raise RuntimeError("docker.sock 不可用（需按部署文档挂载）")
    transport = httpx.AsyncHTTPTransport(uds=str(DOCKER_SOCK))
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        return await client.request(method, f"http://localhost{path}", **kw)


async def apply_upgrade(pack_path: Path, operator: str) -> dict:
    """校验 → docker load → tag latest → 延迟重启。返回给前端的进度摘要。"""
    manifest = verify_pack(pack_path)
    new_version = manifest["version"]

    # 1. docker load（异步生成器流式传 tar：同步文件对象会让 AsyncClient 报
    #    "Attempted to send an sync request"，此前该路径未被真实执行过）
    async def _pack_stream():
        with pack_path.open("rb") as f:
            while chunk := f.read(8 * 1024 * 1024):
                yield chunk

    resp = await _docker("POST", "/images/load", params={"quiet": 1},
                         content=_pack_stream(), timeout=600.0)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"docker load 失败（HTTP {resp.status_code}）：{resp.text[:200]}")

    # 2. tag → latest（compose 固定引用 aether-app:latest）
    resp = await _docker(
        "POST", f"/images/{IMAGE_REPO}:{new_version}/tag",
        params={"repo": IMAGE_REPO, "tag": "latest"},
    )
    if resp.status_code not in (201,):
        raise RuntimeError(f"docker tag 失败（HTTP {resp.status_code}）：{resp.text[:200]}")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "operator": operator,
        "from_version": get_version(),
        "to_version": new_version,
        "notes": manifest.get("notes", ""),
    }
    _append_history(record)
    audit.record(operator, "upgrade_apply", {k: record[k] for k in ("from_version", "to_version")})

    # 3. 延迟重启：先把 HTTP 响应发出去
    def _restart_soon():
        import asyncio

        async def _do():
            try:
                r = await _docker("POST", f"/containers/{CONTAINER_NAME}/restart", params={"t": 5})
                logger.info("Upgrade restart status: %s", r.status_code)
            except Exception:  # noqa: BLE001
                logger.exception("Upgrade restart failed（可手动 docker restart aether）")

        try:
            asyncio.new_event_loop().run_until_complete(_do())
        except Exception:  # noqa: BLE001
            logger.exception("Upgrade restart thread failed")

    timer = threading.Timer(4.0, _restart_soon)
    timer.daemon = True
    timer.start()

    logger.info("Upgrade applied: %s → %s, restart scheduled", record["from_version"], new_version)
    return {
        "from_version": record["from_version"],
        "to_version": new_version,
        "notes": record["notes"],
        "restarting": True,
    }


def _append_history(record: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def upgrade_history(limit: int = 10) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result = []
    for line in lines[-limit:]:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    result.reverse()
    return result
