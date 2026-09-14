from __future__ import annotations

from datetime import datetime, timezone, timedelta

from ..core.config import WEEKDAY_NAMES, get_config
from .mcp_client_manager import MCPClientManager, MCPTool
from .web_tools import fetch_webpage_handler
from .search_tools import web_search_handler
from .weather_tools import get_weather_handler


def _get_tz_offset_hours() -> int:
    return int(get_config("home.timezone_offset", 8))


async def current_time_handler(parameters: dict, session) -> dict:
    """返回当前时间。可选参数 tz_offset_hours(时区偏移)。"""
    tz_offset = parameters.get("tz_offset_hours")
    default_offset = _get_tz_offset_hours()
    if tz_offset is not None:
        tz = timezone(timedelta(hours=int(tz_offset)))
    else:
        tz = timezone(timedelta(hours=default_offset))
    now = datetime.now(tz)
    return {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": WEEKDAY_NAMES[now.weekday()],
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour": now.hour,
        "minute": now.minute,
        "tz_offset_hours": int(tz_offset) if tz_offset is not None else default_offset,
    }


async def describe_state_handler(_: dict, session) -> dict:
    visual_state = (session.latest_visual_state if session else None)
    return {
        "visual_state": visual_state,
    }


def register_local_tools(manager: MCPClientManager) -> None:
    manager.register_tool(
        MCPTool(
            client_id="local",
            tool_name="describe_state",
            description=("查询本轮对话开始时的摄像头状态快照（轮内调用不刷新，非实时）。"
                         "返回里 camera_opened=false 表示摄像头离线——涉及画面的回答必须"
                         "说明摄像头离线、无法看到实时画面，不得描述画面内容"),
            parameters={"type": "object", "properties": {}},
            handler=describe_state_handler,
        )
    )
    manager.register_tool(
        MCPTool(
            client_id="local",
            tool_name="fetch_webpage",
            description="抓取指定网页的内容，返回正文。用户想查看某个网页的具体内容时使用。默认返回 markdown 格式，保留标题/列表/链接结构",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer"},
                    "format": {"type": "string", "description": "返回格式：text 或 markdown，默认 markdown"},
                },
                "required": ["url"],
            },
            handler=fetch_webpage_handler,
        )
    )
    manager.register_tool(
        MCPTool(
            client_id="local",
            tool_name="web_search",
            description="搜索互联网，返回摘要结果。用户想搜索信息、查新闻、找资料时使用",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=web_search_handler,
        )
    )
