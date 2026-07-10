"""MCP 工具 → LangChain StructuredTool 转换层。"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableConfig

from .mcp_client_manager import MCPClientManager, MCPTool

logger = logging.getLogger(__name__)


def _json_schema_to_args_schema(json_schema: dict[str, Any]) -> type:
    """将 JSON Schema 转为 Pydantic model（LangChain 的 args_schema）。

    简单实现：处理 string/integer/number/boolean/object/array 类型。
    如果 schema 为空（无 properties），返回空的 Pydantic model。
    """
    from pydantic import create_model, Field

    properties = json_schema.get("properties", {})
    required = set(json_schema.get("required", []))
    field_definitions: dict[str, Any] = {}

    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "object": dict,
        "array": list,
    }

    for name, prop in properties.items():
        prop_type_str = prop.get("type", "string")
        python_type = type_map.get(prop_type_str, str)
        description = prop.get("description", "")

        if name in required:
            field_definitions[name] = (python_type, Field(description=description))
        else:
            # 可选字段：默认值 None，类型 Union[T, None]
            from typing import Optional
            field_definitions[name] = (Optional[python_type], Field(default=None, description=description))

    model_name = f"ArgsSchema_{abs(hash(str(json_schema)))}"
    return create_model(model_name, **field_definitions)


def mcp_to_langchain_tool(mcp_tool: MCPTool, full_name: bool = False) -> StructuredTool:
    """将 MCPTool 转换为 LangChain StructuredTool。

    Args:
        mcp_tool: 原始 MCP 工具
        full_name: 是否使用完整名（client_id___tool_name），默认用短名
    """
    tool_name = f"{mcp_tool.client_id}___{mcp_tool.tool_name}" if full_name else mcp_tool.tool_name
    original_handler = mcp_tool.handler

    async def _coroutine(config: RunnableConfig, **kwargs):
        # 从 RunnableConfig 提取 session
        session = config["configurable"].get("session")
        # 调用原始 handler（签名：handler(parameters, session)）
        result = await original_handler(kwargs, session)
        # handler 返回 {"error": ...} 视为失败（与 ToolExecutor 约定一致）：
        # 加 "Error:" 前缀，让 LLM 与 on_tool_end 的 is_error 检测都能识别
        if isinstance(result, dict) and "error" in result:
            import json
            body = json.dumps(result, ensure_ascii=False, default=str)
            return f"Error: {result['error']}\n原始返回：{body}"
        # 将结果转为字符串（LangChain ToolMessage 需要字符串内容）
        if isinstance(result, dict):
            import json
            return json.dumps(result, ensure_ascii=False, default=str)
        return str(result)

    args_schema = _json_schema_to_args_schema(mcp_tool.parameters)

    return StructuredTool(
        name=tool_name,
        description=mcp_tool.description,
        coroutine=_coroutine,
        args_schema=args_schema,
    )


def convert_all_tools(manager: MCPClientManager, full_name: bool = False) -> list[StructuredTool]:
    """批量转换所有已注册的 MCP 工具为 LangChain 工具。

    Args:
        manager: MCP 工具管理器
        full_name: 是否使用完整名（client_id___tool_name）
    """
    return [mcp_to_langchain_tool(t, full_name=full_name) for t in manager.list_tools()]
