"""虚拟设备（模拟器 + MQTT）控制路由。

通过 Docker Engine API（unix socket）停止/启动 ``aether-simulator`` 和
``mosquitto`` 容器，前端「高级配置」页据此提供一键开关。

前提：aether 容器挂载了宿主 docker socket（见 docker-compose.yml 的
``/var/run/docker.sock`` volume）。socket 不可用时返回 ``available: false``，
前端据此隐藏开关并提示。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from ..core.api_models import ApiResponse
from ..core.auth import get_current_admin
from ..container import AppContainer, get_container

logger = logging.getLogger(__name__)

router = APIRouter()

# 容器名与 docker-compose.yml 的 container_name 保持一致
SIMULATOR_CONTAINER = "aether-simulator"
MQTT_CONTAINER = "mosquitto"
DOCKER_SOCK = Path("/var/run/docker.sock")


def docker_socket_available() -> bool:
    """docker.sock 是否存在（未挂载则视为不可用）。"""
    return DOCKER_SOCK.exists()


async def _docker_request(method: str, path: str, timeout: float = 10.0) -> httpx.Response | None:
    """通过 unix socket 调 Docker Engine API；socket 不可用返回 None。

    用 httpx 的自定义 transport（httpcore 支持 unix socket），
    避免引入额外依赖。
    """
    if not docker_socket_available():
        return None
    try:
        # 注意：必须用 AsyncHTTPTransport。HTTPTransport 是同步 transport，
        # 没有 __aenter__，配 AsyncClient 会抛 AttributeError 被上层 except 吞掉，
        # 表现为 docker 接口永远「不可用」、前端开关被隐藏。
        transport = httpx.AsyncHTTPTransport(uds=str(DOCKER_SOCK))
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            return await client.request(method, f"http://localhost{path}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Docker API %s %s failed: %s", method, path, exc)
        return None


async def _container_state(name: str) -> dict[str, Any]:
    """查询单个容器状态：running / exists。socket 不可用返回 available=False。"""
    if not docker_socket_available():
        return {"available": False}
    # 不带 /v1.xx 版本前缀：Docker 会按服务端支持的最高版本协商，
    # 避免写死低版本（如 1.41）在新版 Engine 上被拒（最低要求 1.44）。
    resp = await _docker_request("GET", f"/containers/{name}/json")
    if resp is None:
        return {"available": False}
    if resp.status_code == 404:
        return {"available": True, "exists": False, "running": False}
    if resp.status_code != 200:
        return {"available": True, "exists": True, "running": False, "error": f"HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {"available": True, "exists": True, "running": False, "error": "bad response"}
    return {
        "available": True,
        "exists": True,
        "running": bool(data.get("State", {}).get("Running", False)),
    }


async def _container_action(name: str, action: str) -> dict[str, Any]:
    """对容器执行 start / stop，返回结果摘要。"""
    if not docker_socket_available():
        return {"available": False, "ok": False}
    resp = await _docker_request("POST", f"/containers/{name}/{action}", timeout=30.0)
    if resp is None:
        return {"available": True, "ok": False, "error": "docker api 不可用"}
    if resp.status_code == 404:
        return {"available": True, "ok": False, "error": "容器不存在（compose 未启动？）"}
    if resp.status_code in (204, 304):
        return {"available": True, "ok": True}
    return {"available": True, "ok": False, "error": f"HTTP {resp.status_code}"}


async def _refresh_device_views(container: AppContainer) -> None:
    """停/启模拟器后，立即失效 HAService 状态缓存并重建 AI 设备目录，
    让「离线即消失」过滤尽快反映。

    仅消除 Aether 自身的 5s/60s 缓存延迟；HA 把 mqtt 实体标 unavailable
    仍需数秒（broker 断连检测），那部分无法加速。失败不阻塞响应。
    用 getattr 守卫，便于路由被直接调用（测试）时优雅降级。
    """
    try:
        ha_service = getattr(container, "ha_service", None)
        if ha_service is not None and hasattr(ha_service, "invalidate_states_cache"):
            ha_service.invalidate_states_cache()
        refresh_fn = getattr(container, "catalog_refresh_fn", None)
        if refresh_fn is not None:
            await refresh_fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("刷新设备视图失败: %s", exc)


@router.get("/simulator/status")
async def simulator_status() -> ApiResponse[dict]:
    """查询模拟器与 mosquitto 容器状态。"""
    sim = await _container_state(SIMULATOR_CONTAINER)
    mqtt = await _container_state(MQTT_CONTAINER)
    all_available = bool(sim.get("available")) and bool(mqtt.get("available"))
    running = bool(sim.get("running")) and bool(mqtt.get("running"))
    return ApiResponse(data={
        "available": all_available,
        "running": running,
        "simulator": sim,
        "mqtt": mqtt,
    })


@router.post("/simulator/stop")
async def simulator_stop(container: AppContainer = Depends(get_container), admin: dict = Depends(get_current_admin)) -> ApiResponse[dict]:
    """停止虚拟设备模拟器和 mosquitto（设备全部下线）。"""
    if not docker_socket_available():
        return ApiResponse(code="unavailable", message="Docker socket 不可用", data={"ok": False})
    sim = await _container_action(SIMULATOR_CONTAINER, "stop")
    mqtt = await _container_action(MQTT_CONTAINER, "stop")
    ok = bool(sim.get("ok")) and bool(mqtt.get("ok"))
    if ok:
        await _refresh_device_views(container)
    return ApiResponse(
        code="ok" if ok else "partial",
        message="已停止" if ok else "部分失败",
        data={"ok": ok, "simulator": sim, "mqtt": mqtt},
    )


@router.post("/simulator/start")
async def simulator_start(container: AppContainer = Depends(get_container), admin: dict = Depends(get_current_admin)) -> ApiResponse[dict]:
    """启动 mosquitto 和虚拟设备模拟器（先 broker 后模拟器）。"""
    if not docker_socket_available():
        return ApiResponse(code="unavailable", message="Docker socket 不可用", data={"ok": False})
    mqtt = await _container_action(MQTT_CONTAINER, "start")
    sim = await _container_action(SIMULATOR_CONTAINER, "start")
    ok = bool(mqtt.get("ok")) and bool(sim.get("ok"))
    if ok:
        await _refresh_device_views(container)
    return ApiResponse(
        code="ok" if ok else "partial",
        message="已启动" if ok else "部分失败",
        data={"ok": ok, "simulator": sim, "mqtt": mqtt},
    )
