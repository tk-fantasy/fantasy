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
    latest_tool_result = (session.latest_tool_result if session else None)
    return {
        "visual_state": visual_state,
        "latest_tool_result": latest_tool_result,
    }


def register_local_tools(manager: MCPClientManager) -> None:
    manager.register_tool(
        MCPTool(
            client_id="local",
            tool_name="describe_state",
            description="查询当前摄像头画面状态和最近一次工具调用结果",
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

def create_verify_condition_handler(camera_stream, vision_client, ha_client):
    """创建条件验证工具处理器。

    根据 condition_type 路由到合适的验证源，返回当前状态数据和条件判断结果。
    返回值中 condition_met 字段明确表示条件是否成立（true/false/null）。
    """
    async def handler(parameters: dict, session) -> dict:
        condition = str(parameters.get("condition", ""))
        cond_type = str(parameters.get("condition_type", "auto")).lower()

        if cond_type == "auto":
            cond_lower = condition.lower()
            if any(kw in cond_lower for kw in ["时间", "几点", "白天", "晚上", "早上", "下午", "hour", "time", "钟"]):
                cond_type = "time"
            elif any(kw in cond_lower for kw in ["天气", "下雨", "温度", "晴", "weather"]):
                cond_type = "weather"
            elif any(kw in cond_lower for kw in ["画面", "看到", "摄像头", "有人", "检测", "camera", "接上", "没接"]):
                cond_type = "vision"
            elif any(kw in cond_lower for kw in ["设备", "实体", "entity", "device", "状态"]):
                cond_type = "device"
            else:
                cond_type = "time"

        if cond_type == "time":
            time_data = await current_time_handler({"tz_offset_hours": _get_tz_offset_hours()}, session)
            return {
                "condition_met": None,
                "type": "time",
                "current_time": time_data,
                "instruction": "请根据以上当前时间数据判断条件是否成立",
            }

        if cond_type == "weather":
            weather_data = await get_weather_handler(parameters, session)
            return {
                "condition_met": None,
                "type": "weather",
                "current_weather": weather_data,
                "instruction": "请根据以上当前天气数据判断条件是否成立",
            }

        if cond_type == "vision":
            frame = camera_stream.get_latest_frame()
            if frame is None:
                return {
                    "condition_met": None,
                    "type": "vision",
                    "camera_connected": False,
                    "data": "摄像头当前没有画面（未连接或无法打开）",
                    "instruction": "摄像头未连接，请根据条件内容判断是否满足",
                }
            import asyncio
            answer = await vision_client.ask_about_frame(
                frame,
                f"请判断以下条件是否在画面中成立，回答是或否：{condition}"
            )
            return {
                "condition_met": None,
                "type": "vision",
                "camera_connected": True,
                "vision_judgment": answer,
                "instruction": "请根据视觉分析结果判断条件是否成立",
            }

        if cond_type == "device":
            try:
                states = await ha_client.get_states()
                devices = []
                for s in states[:50]:
                    domain = s["entity_id"].split(".")[0]
                    if domain in ("group", "automation", "script", "scene", "person", "zone", "sun", "calendar", "todo", "weather", "binary_sensor", "sensor"):
                        continue
                    devices.append({
                        "entity_id": s["entity_id"],
                        "name": s["attributes"].get("friendly_name", s["entity_id"]),
                        "state": s["state"],
                    })
                return {
                    "condition_met": None,
                    "type": "device",
                    "devices": devices,
                    "instruction": "请根据以上设备当前状态判断条件是否成立",
                }
            except Exception as e:
                return {"condition_met": None, "type": "device", "error": f"查询设备状态失败: {e}"}

        time_data = await current_time_handler({"tz_offset_hours": _get_tz_offset_hours()}, session)
        return {
            "condition_met": None,
            "type": "time",
            "current_time": time_data,
            "instruction": "请根据以上当前时间数据判断条件是否成立",
        }

    return handler


def create_verify_action_handler(ha_client):
    """创建动作验证工具处理器。

    查询 HA 中实体的当前状态，与 call_service 传入的 data 直接比对。
    不硬编码 service→attribute 映射，而是从 data 参数出发在 attributes 中查找。
    """
    async def handler(parameters: dict, session) -> dict:
        entity_id = str(parameters.get("entity_id", ""))
        expected_state = str(parameters.get("expected_state", "") or "")
        action_desc = str(parameters.get("action_description", "") or "")
        data = parameters.get("data") or {}

        if not entity_id:
            return {"verified": False, "error": "缺少 entity_id 参数"}

        has_domain = "." in entity_id

        try:
            states = await ha_client.get_states()
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
                        if entity_id in friendly_name or friendly_name in entity_id:
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
                "entity_id": entity_id,
                "entity_name": attrs.get("friendly_name", entity_id),
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
