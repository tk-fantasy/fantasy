from __future__ import annotations

from datetime import datetime, timezone, timedelta

from ..core.config import WEEKDAY_NAMES, get_config
from .mcp_client_manager import MCPClientManager, MCPTool
from .web_tools import (
    fetch_webpage_handler,
    http_request_handler,
)
from .search_tools import web_search_handler
from .weather_tools import get_weather_handler


def _resolve_ha_client(ha_client):
    """兼容两种注入：[client] 可变引用（生产，HA 热替换后在调用时才取新实例）
    或直接传 client（测试便利）。ref 语义见 tools.ToolDeps.ha_client_ref——
    注册时解引用会把已关闭的旧 client 永久焊死在 verify 工具上。
    """
    if isinstance(ha_client, list):
        return ha_client[0]
    return ha_client


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
            tool_name="http_request",
            description="发送 HTTP 请求（GET/POST 等）到外部 API 并返回响应。用于调用公开 Web API",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "headers": {"type": "object"},
                    "params": {"type": "object"},
                    "json_body": {"type": "object"},
                },
                "required": ["url"],
            },
            handler=http_request_handler,
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


# ---------------------------------------------------------------------------
# 验证工具工厂（需要运行时依赖注入）
# ---------------------------------------------------------------------------


def create_verify_action_handler(ha_client):
    """创建动作验证工具处理器。

    查询 HA 中实体的当前状态，与 call_service 传入的 data 直接比对。
    不硬编码 service→attribute 映射，而是从 data 参数出发在 attributes 中查找。
    """
    async def handler(parameters: dict, session) -> dict:
        ha = _resolve_ha_client(ha_client)
        # 本地模型（Ollama）常在工具参数首尾带空格，导致 entity_id 精确匹配失败。
        # 在入口统一 strip，避免下游每个比较点都要单独处理。
        entity_id = str(parameters.get("entity_id", "")).strip()
        expected_state = str(parameters.get("expected_state", "") or "").strip()
        data = parameters.get("data") or {}

        if not entity_id:
            return {"verified": False, "error": "缺少 entity_id 参数"}

        has_domain = "." in entity_id

        try:
            states = await ha.get_states()
            actual = None

            if has_domain:
                for s in states:
                    if s["entity_id"] == entity_id:
                        actual = s
                        break
            else:
                for s in states:
                    eid = s["entity_id"]
                    name_part = eid.split(".", 1)[1] if "." in eid else eid
                    if name_part == entity_id or entity_id in name_part or name_part in entity_id:
                        actual = s
                        break
                if actual is None:
                    for s in states:
                        friendly_name = s.get("attributes", {}).get("friendly_name", "")
                        # 空 friendly_name 会 ""in任何输入 恒真 → 跳过，避免误匹配
                        if friendly_name and (entity_id in friendly_name or friendly_name in entity_id):
                            actual = s
                            break

            if actual is None:
                return {
                    "verified": False,
                    "entity_id": entity_id,
                    "error": f"实体 {entity_id} 不存在",
                }

            current_state = actual["state"]
            attrs = actual.get("attributes", {})
            is_on = current_state not in ("off", "closed", "unavailable", "unknown")

            checks = []

            # 从 data 参数出发，直接在 attributes 中查找对应 key 比对
            for key, expected_val in data.items():
                actual_val = attrs.get(key)
                # 如果直接 key 找不到，尝试 current_{key}（如 position → current_position）
                if actual_val is None:
                    actual_val = attrs.get(f"current_{key}")
                if actual_val is not None:
                    # 数值比较容错
                    try:
                        matched = float(expected_val) == float(actual_val)
                    except (ValueError, TypeError):
                        matched = str(expected_val) == str(actual_val)
                    checks.append({
                        "attribute": key,
                        "expected": expected_val,
                        "actual": actual_val,
                        "passed": matched,
                    })

            # 如果没有 data 或 data 中没有可验证的属性，用 expected_state 检查 state
            if not checks and expected_state:
                exp_lower = expected_state.lower()
                if exp_lower in ("on", "开", "打开", "开启"):
                    checks.append({"attribute": "state", "expected": "on", "actual": current_state, "passed": is_on})
                elif exp_lower in ("off", "关", "关闭", "关掉"):
                    checks.append({"attribute": "state", "expected": "off", "actual": current_state, "passed": not is_on})
                else:
                    checks.append({"attribute": "state", "expected": expected_state, "actual": current_state, "passed": exp_lower in current_state.lower()})

            all_passed = all(c["passed"] for c in checks) if checks else True

            result = {
                "verified": all_passed,
                # 回显实际匹配到的实体（模糊匹配时可能与输入不同，如输入 "be" 匹配 light.bed）
                "entity_id": actual["entity_id"],
                "entity_name": attrs.get("friendly_name") or actual["entity_id"],
                "current_state": current_state,
                "is_on": is_on,
            }

            if checks:
                result["checks"] = checks
                failed = [c for c in checks if not c["passed"]]
                if failed:
                    result["error"] = "验证失败: " + ", ".join(
                        f"{c['attribute']} 期望 {c['expected']} 实际 {c['actual']}" for c in failed
                    )

            return result

        except Exception as e:
            return {"verified": False, "entity_id": entity_id, "error": f"验证失败: {e}"}

    return handler
