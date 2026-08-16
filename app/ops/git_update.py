"""git 一键升级（运维页「git 一键升级」的后端）。

思路：不在 aether 容器里装 git/compose，而是经 docker.sock 拉起一次性升级容器
（aether-git-updater 镜像，compose profile:ops 构建），挂载宿主仓库与 docker.sock，
执行 scripts/update-from-git.sh（check 模式只 fetch 比对）。结果 JSON 与日志写到
宿主 logs/ 下 —— aether 已挂载该目录，状态接口直接读文件即可，无需容器间通信。

令牌存 config.json update.git_token（仅经环境变量注入升级容器，任何接口不回明文）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from ..core.config import BASE_DIR, get_config, update_config_section
from ..core.exceptions import AppException
from . import audit, upgrade
from .upgrade import _docker

logger = logging.getLogger(__name__)

UPDATER_IMAGE = "aether-git-updater:latest"
RUN_CONTAINER = "aether-git-update-run"
RESULT_FILE = BASE_DIR / "logs" / "git-update-result.json"
LOG_FILE = BASE_DIR / "logs" / "git-update.log"
CONTAINER_LABEL = "com.docker.compose.project.working_dir"


# ==================== 配置 ====================

def get_git_token() -> str:
    return str(get_config("update.git_token", "") or "").strip()


def set_git_token(token: str) -> bool:
    update_config_section("update", {"git_token": (token or "").strip()})
    return bool((token or "").strip())


def get_repo_path_cached() -> str:
    return str(get_config("update.git_repo_path", "") or "").strip()


# ==================== 仓库路径探测 ====================

async def resolve_repo_path() -> str:
    """宿主仓库目录：config 覆盖项优先，否则读 aether 容器的 compose working_dir label。"""
    cached = get_repo_path_cached()
    if cached:
        return cached
    if not upgrade.DOCKER_SOCK.exists():
        raise AppException("docker.sock 不可用，无法定位仓库路径", http_status=400)
    resp = await _docker("GET", f"/containers/{upgrade.CONTAINER_NAME}/json")
    if resp.status_code != 200:
        raise AppException(
            f"读取容器信息失败（HTTP {resp.status_code}），请在高级配置指定仓库路径",
            http_status=502,
        )
    labels = resp.json().get("Config", {}).get("Labels") or {}
    workdir = str(labels.get(CONTAINER_LABEL) or "").strip()
    if not workdir:
        raise AppException(
            "无法从容器 label 探测宿主仓库路径，请在下方指定仓库绝对路径",
            http_status=400,
        )
    return workdir


# ==================== 升级容器编排 ====================

def _to_docker_path(p: str) -> str:
    """宿主路径 → Docker 可接受的 bind mount 源形式。

    Windows 下探测到的 label 是 "D:\\Aether" 反斜杠形式，Docker API 只认
    "D:/Aether"（正斜杠）；Linux 路径原样返回。
    """
    return p.replace("\\", "/") if "\\" in p else p


def _container_payload(mode: str, repo_path: str, token: str) -> dict:
    """一次性升级容器的创建参数。/result 映射宿主 logs/（结果与日志落点）。

    注意 bind 源必须是宿主路径：本应用跑在 aether 容器里，BASE_DIR 是容器内
    路径（/aether），不能直接当挂载源（Docker 会当宿主路径解析出空目录）。
    logs 与仓库同在宿主仓库目录下，从 repo_path 推导。
    """
    repo_path = _to_docker_path(repo_path)
    logs_path = f"{repo_path.rstrip('/')}/logs"
    return {
        "Image": UPDATER_IMAGE,
        "name": RUN_CONTAINER,
        "AttachStdout": False,
        "AttachStderr": False,
        "WorkingDir": "/repo",
        "Env": [
            f"MODE={mode}",
            f"GIT_TOKEN={token}",
            "RESULT_FILE=/result/git-update-result.json",
            "LOG_FILE=/result/git-update.log",
            "HEALTH_URL=http://host.docker.internal:8010/api/health",
        ],
        "Labels": {"aether.git-update": mode},
        "HostConfig": {
            "Binds": [
                f"{repo_path}:/repo",
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{logs_path}:/result",
            ],
            "ExtraHosts": ["host.docker.internal:host-gateway"],
            "AutoRemove": False,   # 留尸体供排障；下次拉起前统一清理
        },
    }


async def _run_updater(mode: str, timeout: float) -> dict:
    """拉起一次性升级容器跑到结束（check 同步等；apply 也同步等，见调用方）。"""
    token = get_git_token()
    if not token:
        raise AppException("尚未配置 Gitee 访问令牌", http_status=400)
    if not upgrade.DOCKER_SOCK.exists():
        raise AppException("docker.sock 不可用（需按部署文档挂载）", http_status=400)
    repo_path = await resolve_repo_path()

    # 清理上一次的残留容器/结果（同名容器存在则 Docker 会 409）
    old = await _docker("GET", f"/containers/{RUN_CONTAINER}/json", timeout=10.0)
    if old.status_code == 200:
        await _docker("DELETE", f"/containers/{RUN_CONTAINER}", params={"force": "1"}, timeout=10.0)
    for f in (RESULT_FILE, LOG_FILE):
        f.unlink(missing_ok=True)

    resp = await _docker(
        "POST", "/containers/create", params={"name": RUN_CONTAINER},
        json=_container_payload(mode, repo_path, token), timeout=30.0,
    )
    if resp.status_code not in (200, 201):
        detail = resp.text[:200]
        if "No such image" in detail:
            raise AppException(
                "升级镜像 aether-git-updater 不存在，请先在宿主执行："
                "docker compose --profile ops up -d --build git-updater",
                http_status=400,
            )
        raise RuntimeError(f"创建升级容器失败（HTTP {resp.status_code}）：{detail}")

    resp = await _docker("POST", f"/containers/{RUN_CONTAINER}/start", timeout=30.0)
    if resp.status_code not in (204, 304):
        raise RuntimeError(f"启动升级容器失败（HTTP {resp.status_code}）：{resp.text[:200]}")

    # 轮询等容器退出（check 秒级；apply 主机上构建可达数分钟）
    import asyncio

    waited = 0.0
    step = 2.0
    while waited < timeout:
        info = await _docker("GET", f"/containers/{RUN_CONTAINER}/json", timeout=10.0)
        if info.status_code == 404:
            break
        if info.json().get("State", {}).get("Running") is False:
            break
        await asyncio.sleep(step)
        waited += step

    result = read_result()
    # 容器跑完就删（结果已在宿主 logs/，日志也在；留着只会挡下一次）
    await _docker("DELETE", f"/containers/{RUN_CONTAINER}", params={"force": "1"}, timeout=10.0)
    if result is None:
        raise AppException(
            f"升级容器 {timeout:.0f}s 内未完成或未产出结果，详见 logs/git-update.log",
            http_status=504,
        )
    return result


async def check_git_update() -> dict:
    """fetch + 比对当前/远程 commit（不改动仓库）。"""
    return await _run_updater("check", timeout=120.0)


async def apply_git_update(operator: str) -> dict:
    """执行 update-from-git.sh：拉取 → 重建 → 健康自检 → 失败自动回退。

    同步等容器退出（脚本含 240s 健康等待 + 回退重建，最长约 8~10 分钟）；
    aether 容器在重建窗口会重启，连接断开由前端处理（轮询 /api/health）。
    """
    result = await _run_updater("apply", timeout=900.0)
    audit.record(operator, "git_update_apply", {
        "status": result.get("status"),
        "to_version": result.get("to_version", ""),
    })
    return result


def read_result() -> dict | None:
    """读升级容器写下的结果 JSON（无文件/损坏返回 None）。"""
    try:
        return json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_log_tail(lines: int = 200) -> str:
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


async def status_git_update() -> dict:
    """当前无升级容器（用完即删）；状态 = 最近一次结果 + 日志尾。"""
    return {
        "result": read_result(),
        "log_tail": read_log_tail(),
        "token_configured": bool(get_git_token()),
    }
