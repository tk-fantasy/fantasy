"""升级包一键导出与本地安装（运维页「升级包分发」的后端）。

发布方（开发者机）：把当前运行的 aether-app:latest 镜像 docker save 导出，
按 build-update-pack.py 的包结构打成 aether-update-<版本>.tar.gz 放 backups/，
运维页下载后经微信/网盘发给接收方。

接收方：把收到的包文件放进宿主 Aether/backups/ 目录，运维页识别后一键安装 ——
复用 upgrade.verify_pack / apply_upgrade（sha256 + min_compatible 校验 →
docker load → tag latest → 自动重启），成功后自动删除安装包。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..core.config import BASE_DIR
from ..core.exceptions import AppException
from ..core.version import get_version
from . import audit, upgrade

logger = logging.getLogger(__name__)

PACK_DIR = BASE_DIR / "backups"          # 宿主 ./backups（已 bind-mount，双方共用）
STAGING_DIR = PACK_DIR / ".export-staging"
# 严格包名（安装侧防路径穿越：只认这个形状，名字直接拼路径也安全）
PACK_NAME_RE = re.compile(r"^aether-update-[0-9][0-9A-Za-z.\-_]*\.tar\.gz$")
GZIP_LEVEL = 6   # 镜像层未压缩、gz 收益大；9 太慢，6 是体积/速度折中

# 导出任务状态（单任务；POST 启动、GET 轮询）
_state: dict = {"status": "idle", "staged_bytes": 0, "total_bytes": 0,
                "file": "", "error": ""}


def _read_min_compatible() -> str:
    try:
        data = json.loads((BASE_DIR / "version.json").read_text(encoding="utf-8"))
        return str(data.get("min_compatible") or get_version())
    except (OSError, json.JSONDecodeError):
        return get_version()


def export_status() -> dict:
    s = dict(_state)
    if s["status"] == "done":
        p = PACK_DIR / s["file"]
        s["size_bytes"] = p.stat().st_size if p.exists() else 0
    return s


async def start_export(operator: str, notes: str = "") -> dict:
    """启动导出任务（后台跑，进度走 export_status）。已在跑则 409。"""
    if _state["status"] == "running":
        raise AppException("已有导出任务在跑", code="pack_export_busy", http_status=409)
    if not upgrade.DOCKER_SOCK.exists():
        raise AppException("docker.sock 不可用（需按部署文档挂载）", http_status=400)

    _state.update(status="running", staged_bytes=0, total_bytes=0, file="", error="")
    asyncio.create_task(_export_job(operator, notes))
    return {"started": True}


async def _export_job(operator: str, notes: str) -> None:
    """导出主流程：tag 版本号 → 流式 docker save → manifest → tar.gz。"""
    version = get_version()
    try:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        for old in STAGING_DIR.iterdir():
            old.unlink(missing_ok=True)

        # 1. 当前 latest 补打 <版本> tag：apply_upgrade 侧 docker load 后按
        #    aether-app:<版本> → latest 切换，包内镜像必须带版本 tag
        resp = await upgrade._docker(
            "POST", f"/images/{upgrade.IMAGE_REPO}:latest/tag",
            params={"repo": upgrade.IMAGE_REPO, "tag": version},
        )
        if resp.status_code != 201:
            raise RuntimeError(f"docker tag 失败（HTTP {resp.status_code}）：{resp.text[:200]}")

        # 2. 大小预估（进度条分母；镜像层未压缩大小）
        resp = await upgrade._docker("GET", f"/images/{upgrade.IMAGE_REPO}:latest/json")
        _state["total_bytes"] = int(resp.json().get("Size") or 0)

        # 3. 流式 docker save → staging/images/aether.tar（边写边算 sha256）
        images_dir = STAGING_DIR / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        image_tar = images_dir / "aether.tar"
        h = hashlib.sha256()
        received = 0
        transport = httpx.AsyncHTTPTransport(uds=str(upgrade.DOCKER_SOCK))
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(600.0)) as client:
            async with client.stream("GET", f"http://localhost/images/{upgrade.IMAGE_REPO}:{version}/get") as r:
                if r.status_code != 200:
                    raise RuntimeError(f"docker save 失败（HTTP {r.status_code}）")
                with image_tar.open("wb") as f:
                    async for chunk in r.aiter_bytes(8 * 1024 * 1024):
                        f.write(chunk)
                        h.update(chunk)
                        received += len(chunk)
                        _state["staged_bytes"] = received

        # 4. manifest + 打包（tar.gz CPU 密集，放线程池别卡事件循环）
        manifest = {
            "version": version,
            "min_compatible": _read_min_compatible(),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "notes": notes,
            "images": [{
                "name": upgrade.IMAGE_REPO, "tag": version,
                "file": "images/aether.tar",
                "sha256": h.hexdigest(),
                "size_bytes": image_tar.stat().st_size,
            }],
        }
        (STAGING_DIR / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        out_name = f"aether-update-{version}.tar.gz"
        await asyncio.get_running_loop().run_in_executor(
            None, _make_tar_gz, STAGING_DIR, PACK_DIR / out_name)

        _state["file"] = out_name
        _state["status"] = "done"
        audit.record(operator, "pack_export", {
            "version": version, "file": out_name,
            "size_bytes": (PACK_DIR / out_name).stat().st_size,
        })
        logger.info("Update pack exported: %s", out_name)
    except Exception as e:  # noqa: BLE001 — 状态接口要把失败原因带给前端
        _state["status"] = "error"
        _state["error"] = str(e)
        logger.exception("Pack export failed")


def _make_tar_gz(staging: Path, out: Path) -> None:
    import tarfile

    with tarfile.open(out, "w:gz", compresslevel=GZIP_LEVEL) as tf:
        tf.add(staging / "manifest.json", arcname="manifest.json")
        tf.add(staging / "images" / "aether.tar", arcname="images/aether.tar")


# ==================== 接收方：本地包扫描与安装 ====================

def scan_local_packs() -> list[dict]:
    """扫描 backups/ 下已投放的升级包（接收方把微信收的文件放这里）。"""
    if not PACK_DIR.exists():
        return []
    packs = []
    for p in sorted(PACK_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and PACK_NAME_RE.match(p.name):
            st = p.stat()
            packs.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return packs


def local_pack_path(name: str) -> Path:
    """包名 → 路径。名字不合规直接拒绝（防穿越）。"""
    if not PACK_NAME_RE.match(name):
        raise AppException("非法的升级包文件名", code="pack_bad_name", http_status=400)
    return PACK_DIR / name


async def apply_local_pack(name: str, operator: str) -> dict:
    """安装 backups/ 里的升级包：校验 → load → tag → 重启 → 删包。"""
    path = local_pack_path(name)
    if not path.exists():
        raise AppException("升级包不存在（放到 Aether/backups/ 后刷新列表）",
                           code="pack_not_found", http_status=404)
    result = await upgrade.apply_upgrade(path, operator)
    # apply_upgrade 已排队延迟重启；重启前删掉安装包（用户流程要求装完即清）
    try:
        path.unlink()
    except OSError:
        logger.warning("Failed to remove installed pack: %s", path)
    audit.record(operator, "pack_install", {
        "pack": name, "to_version": result.get("to_version", ""),
    })
    return result
