"""MCP 工具执行器 — 统一入口，含参数 schema 校验。"""
from __future__ import annotations

import logging
from typing import Any

from .mcp_client_manager import MCPClientManager

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self, manager: MCPClientManager) -> None:
        self._manager = manager

    async def execute_tool_by_name(self, tool_name: str, parameters: dict[str, Any], session) -> dict[str, Any]:
        tool = self._manager.get_tool(tool_name)
        if tool is None:
            return {"success": False, "error": f"tool not found: {tool_name}"}

        # JSON Schema 轻量校验
        schema = tool.parameters
        if schema and schema.get("type") == "object" and schema.get("properties"):
            errors = _validate_params(parameters or {}, schema)
            if errors:
                logger.warning("Tool %s parameter validation failed: %s", tool_name, errors)
                return {"success": False, "error": f"invalid parameters: {'; '.join(errors)}", "tool_name": tool_name}

        result = await tool.handler(parameters, session)
        # 如果 handler 返回的结果本身包含 error 字段，视为失败
        if isinstance(result, dict) and "error" in result:
            return {"success": False, "tool_name": tool_name, "error": result["error"], "result": result}
        return {"success": True, "tool_name": tool_name, "result": result}

    def resolve_tool_name(self, tool_name: str) -> str:
        """解析工具名,如果不含分隔符则搜索所有已注册工具。

        Args:
            tool_name: 工具名,可能带 client_id___ 前缀或不带

        Returns:
            完整的工具名 (client_id___tool_name)
        """
        # 1. 直接匹配完整名
        if self._manager.get_tool(tool_name) is not None:
            return tool_name
        # 2. 如果不含分隔符，搜索所有已注册工具
        if "___" not in tool_name:
            for tool in self._manager.list_tools():
                if tool.tool_name == tool_name:
                    return f"{tool.client_id}___{tool_name}"
        return tool_name


# ---------------------------------------------------------------------------
# 轻量 JSON Schema 校验
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _type_check(value: Any, expected_type: str) -> bool:
    expected = _TYPE_MAP.get(expected_type)
    if expected is None:
        return True  # 未知类型跳过校验
    return isinstance(value, expected)


def _validate_params(params: dict, schema: dict) -> list[str]:
    """轻量 JSON Schema 校验：required + type + enum。不引入 jsonschema 依赖。"""
    errors: list[str] = []

    # required 字段检查
    for field in schema.get("required", []):
        if field not in params:
            errors.append(f"missing required field: {field}")

    # 逐字段 type + enum 校验
    props = schema.get("properties", {})
    for key, value in params.items():
        prop_def = props.get(key)
        if prop_def is None:
            continue  # 未定义的字段允许透传（宽松策略）

        expected_type = prop_def.get("type")
        if expected_type and not _type_check(value, expected_type):
            errors.append(f"field '{key}': expected {expected_type}, got {type(value).__name__}")
            continue  # 类型不对就不继续校验 enum

        enum_values = prop_def.get("enum")
        if enum_values is not None and value not in enum_values:
            errors.append(f"field '{key}': value {value!r} not in {enum_values}")

    return errors
