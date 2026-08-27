"""MCP 动态管理路由 — MCP server 连接、Agent 状态。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.config import get_config
from ..core.exceptions import AppException
from ..schema.api_schemas import MCPConnectRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/mcp/servers")
async def list_mcp_servers(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """列出已连接的外部 MCP server 和全部注册工具。"""
    mcp_client_manager = container.mcp_client_manager
    servers = mcp_client_manager.list_external_servers()
    all_tools = [
        {"name": k, "description": v.description}
        for k, v in mcp_client_manager._tools.items()
    ]
    return ApiResponse(data={"servers": servers, "tools": all_tools})


@router.post("/mcp/servers")
async def connect_mcp_server(payload: MCPConnectRequest, container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """运行时连接新的外部 MCP server。

    安全限制：只允许连接 config.json 的 external_mcp 中已预声明的 server，
    杜绝通过本接口执行任意命令（RCE）。预声明条目由 name + cmd 共同唯一标识。
    """
    from ..main import _rebuild_lock, _rebuild_agent
    mcp_client_manager = container.mcp_client_manager

    name = payload.name.strip()
    cmd = payload.cmd.strip()
    args = payload.args

    # 白名单校验：必须命中 config.json 的 external_mcp 预声明条目
    if not _is_allowed_external_mcp(name, cmd):
        # 注意：extra 的 key 不能用 "name"——它是 LogRecord 的保留属性，
        # 否则 logger.warning 会抛 KeyError，被上层 api_token_guard 的
        # `except Exception: pass` 吞掉，导致本应返回 403 的请求被误转为 401。
        logger.warning(
            "Rejected MCP connect: not in whitelist",
            extra={"mcp_name": name, "mcp_cmd": cmd},
        )
        raise AppException(
            "拒绝连接：该 MCP server 未在 config.json 的 external_mcp 中预声明，"
            "不允许运行时连接任意命令。请在配置文件中预先声明。",
            code="mcp_not_whitelisted",
            http_status=403,
        )

    async with _rebuild_lock:
        try:
            tools = await asyncio.wait_for(
                mcp_client_manager.connect_external_server(name, cmd, args),
                timeout=60,
            )
        except asyncio.TimeoutError:
            raise AppException(f"连接 {name} 超时（60s）", code="mcp_timeout", http_status=504)
        except Exception as e:
            raise AppException(f"连接 {name} 失败: {e}", code="mcp_error", http_status=502)
        await _rebuild_agent()
    return ApiResponse(data={"connected": True, "name": name, "tools": len(tools)})


def _is_allowed_external_mcp(name: str, cmd: str) -> bool:
    """校验 (name, cmd) 是否在 config.json 的 external_mcp 预声明白名单内。

    白名单由运维在 config.json 静态声明，是启动时 connect_external_mcp_servers
    读取的同一份配置。运行时接口只能连接这些已预声明的 server。
    """
    whitelist = get_config("external_mcp", []) or []
    for entry in whitelist:
        if entry.get("name", "") == name and entry.get("cmd", "") == cmd:
            return True
    return False


@router.delete("/mcp/servers/{name}")
async def disconnect_mcp_server(name: str, container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """断开指定的外部 MCP server。"""
    from ..main import _rebuild_lock, _rebuild_agent
    mcp_client_manager = container.mcp_client_manager
    async with _rebuild_lock:
        ok = await mcp_client_manager.disconnect_server(name)
        if not ok:
            raise AppException(f"server '{name}' 未找到", code="not_found", http_status=404)
        await _rebuild_agent()
    return ApiResponse(data={"disconnected": True, "name": name})
