"""MCP 工具注册中心 — 所有内置工具的注册入口。

加工具只需在此文件添加，不需要改 main.py。
handler 通过参数接收依赖，不闭包全局变量（支持 HA 热替换等场景）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .mcp.local_mcp_servers import (
    create_verify_action_handler,
    create_verify_condition_handler,
    register_local_tools,
)
from .mcp.mcp_client_manager import MCPClientManager, MCPTool
from .services.entity_controls import resolve_controls
from .utils.text_match import match_devices

logger = logging.getLogger(__name__)


@dataclass
class ToolDeps:
    """工具注册所需的服务依赖。

    使用 ref 模式的属性（ha_client_ref / scheduler_service_ref）支持运行时热替换：
    scheduler_service 在 lifespan 后段才创建，注册工具时还不存在，
    handler 被调用时才读取 ref[0]。
    """
    mcp_client_manager: MCPClientManager
    camera_stream: Any
    vision_client: Any
    ha_service: Any
    # 可变引用：ha_client 可能被热替换
    ha_client_ref: list  # [HomeAssistantClient]
    # 可变引用：scheduler_service 在 lifespan 后段才创建
    scheduler_service_ref: list = field(default_factory=lambda: [None])


def register_all_tools(deps: ToolDeps) -> None:
    """注册所有内置 MCP 工具。加工具只改这个文件。"""
    # 1. 基础工具（无外部依赖）
    register_local_tools(deps.mcp_client_manager)
    # 2. 视觉聊天
    _register_vision_chat(deps)
    # 3. HA 设备查询
    _register_ha_get_entities(deps)
    # 4. HA 服务调用
    _register_ha_call_service(deps)
    # 5. 条件验证
    _register_verify_condition(deps)
    # 6. 动作验证
    _register_verify_action(deps)
    # 7. 定时任务管理（让 agent 能对话建/查/删定时任务）
    _register_scheduled_task_tools(deps)


# ---------------------------------------------------------------------------
# 各工具注册函数
# ---------------------------------------------------------------------------

def _register_vision_chat(deps: ToolDeps) -> None:
    async def handler(parameters: dict, session) -> dict:
        question = str(parameters.get("question", "") or "请描述画面内容。")
        frame = deps.camera_stream.get_latest_frame()
        if frame is None:
            return {"answer": "摄像头当前没有画面,无法分析。", "question": question, "has_frame": False}
        answer = await deps.vision_client.ask_about_frame(frame, question)
        return {"answer": answer, "question": question, "has_frame": True, "model": deps.vision_client.model}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="vision_chat",
        description="拍摄当前摄像头画面，根据画面内容回答用户问题",
        parameters={"type": "object", "properties": {"question": {"type": "string"}}},
        handler=handler,
    ))


def _register_ha_get_entities(deps: ToolDeps) -> None:
    async def handler(_: dict, session) -> dict:
        try:
            devices = await deps.ha_service.get_all_devices()
            raw_svc_defs = await deps.ha_service.get_service_defs(
                deps.ha_client_ref[0], domains=set(d.get("domain", "") for d in devices)
            )
            services_info = {
                domain: {svc_name: svc_def["fields"] for svc_name, svc_def in svcs.items()}
                for domain, svcs in raw_svc_defs.items()
            }
            for device in devices:
                device["_controls"] = resolve_controls(device, raw_svc_defs)
            return {"entities": devices, "count": len(devices), "services": services_info}
        except Exception as e:
            logger.exception("HA get_entities failed")
            return {"entities": [], "count": 0, "error": str(e)}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="ha_devices",
        tool_name="get_entities",
        description="获取所有 Home Assistant 设备列表及其状态和可控项",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    ))


def _register_ha_call_service(deps: ToolDeps) -> None:
    async def handler(parameters: dict, session) -> dict:
        domain = str(parameters.get("domain", ""))
        service = str(parameters.get("service", ""))
        entity_id = parameters.get("entity_id")
        data = parameters.get("data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                data = {}
        if not domain and entity_id and "." in str(entity_id):
            domain = str(entity_id).split(".")[0]
        if entity_id and "." not in str(entity_id):
            entity_id = f"{domain}.{entity_id}"
        try:
            ha_client = deps.ha_client_ref[0]  # 动态读取当前实例
            # entity_id 真实性校验：HA 对不存在的 entity_id 静默返回 200（不报错），
            # 不校验的话 LLM 编造的 entity_id 会被当成"成功"，谎报已执行。
            # 支持逗号分隔的批量 entity_id，逐个校验。
            if entity_id:
                try:
                    states = await ha_client.get_states()
                    real_ids = {s.get("entity_id") for s in states}
                    eid_list = [e.strip() for e in str(entity_id).split(",") if e.strip()]
                    missing = [e for e in eid_list if e not in real_ids]
                    if missing:
                        logger.info("call_service 拒绝编造 entity_id: %s", missing)
                        return {
                            "success": False,
                            "error": (
                                f"entity_id '{', '.join(missing)}' 不存在于 Home Assistant，"
                                "无法控制。请用 get_entities 查看真实设备列表，不要编造 entity_id。"
                            ),
                        }
                except Exception:
                    logger.warning("call_service: entity_id 校验失败，放行", exc_info=True)
            # query→entity 语义校验：复用 match_devices 判断用户指令命中的设备，
            # 若命中设备但目标 entity_id 不在命中范围内 → 拒绝（防止语义近邻顶替，
            # 如「打开加湿器」却操作带除湿模式的空调）。
            # matched 为空时放行（无法区分"设备不在列表"与"泛指无设备名"，
            # 避免误伤"太热了→开空调"这类合理推断；该场景靠 system prompt 注入兜底软约束）。
            query = getattr(session, "current_query", "") or ""
            if query and entity_id:
                try:
                    devices = await deps.ha_service.get_all_devices()
                    matched = match_devices(query, devices)
                    if matched:
                        matched_ids = {d.get("entity_id") for d in matched}
                        eid_list = [e.strip() for e in str(entity_id).split(",") if e.strip()]
                        if not any(e in matched_ids for e in eid_list):
                            names = "、".join(d.get("name", d.get("entity_id", "")) for d in matched)
                            logger.info(
                                "call_service 拒绝语义错配: query=%r matched=%s target=%s",
                                query, matched_ids, eid_list,
                            )
                            return {
                                "success": False,
                                "error": (
                                    f"用户说的是「{query}」，匹配到的设备是「{names}」，"
                                    f"与目标 {entity_id} 不符。不要用语义相近的实体顶替，"
                                    "若用户提到的设备不存在请如实告知。"
                                ),
                            }
                except Exception:
                    logger.warning("call_service: 语义校验失败，放行", exc_info=True)
            result = await ha_client.call_service(domain, service, entity_id, data)
            new_state = None
            if entity_id:
                try:
                    states = await ha_client.get_states()
                    for s in states:
                        if s.get("entity_id") == entity_id:
                            new_state = {"state": s.get("state"), "attributes": s.get("attributes", {})}
                            break
                except Exception:
                    pass
            return {"success": True, "result": result, "new_state": new_state}
        except Exception as e:
            logger.exception("HA call_service failed")
            return {"success": False, "error": str(e)}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="ha_devices",
        tool_name="call_service",
        description="调用 Home Assistant 服务来控制设备",
        parameters={
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "entity_id": {"type": "string"},
                "data": {"type": "object"},
            },
            "required": ["domain", "service", "entity_id"],
        },
        handler=handler,
    ))


def _register_verify_condition(deps: ToolDeps) -> None:
    handler = create_verify_condition_handler(
        deps.camera_stream, deps.vision_client, deps.ha_client_ref[0]
    )
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="verify_condition",
        description=(
            "验证某个条件当前是否成立。在执行任何条件性操作（'如果...就...'）之前必须先调用此工具。"
            "根据 condition_type 自动路由到正确的验证源，返回实时状态数据。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "condition": {
                    "type": "string",
                    "description": "要验证的条件，用自然语言描述",
                },
                "condition_type": {
                    "type": "string",
                    "enum": ["auto", "time", "weather", "vision", "device"],
                    "description": "条件类型：auto=自动识别, time=时间, weather=天气, vision=视觉, device=设备状态",
                },
            },
            "required": ["condition"],
        },
        handler=handler,
    ))


def _register_verify_action(deps: ToolDeps) -> None:
    handler = create_verify_action_handler(deps.ha_client_ref[0])
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="verify_action",
        description=(
            "只读校验工具：查询 Home Assistant 当前实时状态，对比某次 call_service 之后设备是否真的变了。"
            "本工具只读，绝不执行任何控制操作，不能用来开/关/调节设备——执行控制必须用 call_service。"
            "典型用法：先 call_service 设温度，再用本工具查证温度是否已变。"
            "禁止用本工具去'设置'任何值：想改设备状态只能 call_service。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "要验证的设备 ID",
                },
                "service": {
                    "type": "string",
                    "description": "刚才调用的服务名",
                },
                "data": {
                    "type": "object",
                    "description": "传给服务的参数",
                },
                "expected_state": {
                    "type": "string",
                    "description": "期望的状态值（旧版，建议用 service+data 替代）",
                },
                "action_description": {
                    "type": "string",
                    "description": "刚才执行的操作的简要描述",
                },
            },
            "required": ["entity_id"],
        },
        handler=handler,
    ))


# ---------------------------------------------------------------------------
# 定时任务工具 — 让 agent 能通过对话建/查/删定时任务
# ---------------------------------------------------------------------------

def _register_scheduled_task_tools(deps: ToolDeps) -> None:
    """注册定时任务管理工具。

    scheduler_service 在 lifespan 后段才创建，这里用 deps.scheduler_service_ref[0]
    在 handler 被调用时动态读取（与 ha_client_ref 同模式）。
    """

    def _svc():
        return deps.scheduler_service_ref[0]

    async def create_handler(parameters: dict, session) -> dict:
        svc = _svc()
        if svc is None:
            return {"error": "调度器未就绪"}
        name = str(parameters.get("name", "")).strip()
        if not name:
            return {"error": "name 不能为空"}
        schedule = parameters.get("schedule") or {}
        payload = parameters.get("payload") or {}
        if not schedule or not payload:
            return {"error": "schedule 和 payload 都是必填"}
        task = await svc.add_task({
            "name": name,
            "schedule": schedule,
            "payload": payload,
            "enabled": True,
        })
        from .services.scheduler_service import summarize_schedule
        # 只回精简摘要，不回完整 task（含 payload 文本），避免模型复述导致确认语冗长重复
        return {"success": True, "task_id": task.get("id"), "name": name,
                "summary": summarize_schedule(task.get("schedule", {}))}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="scheduled_task_create",
        description=(
            "【定时任务】当用户指定一个未来时间点或周期要做某事时（如'11点20分开灯''每天8点提醒''每小时刷新'），"
            "必须用本工具创建定时任务，让系统到点自动执行——禁止立即执行动作。"
            "判断标准：用户的话里带未来时刻（X点X分/明天/后天/每天/每小时/X分钟后），就该用本工具，而非现在就做。"
            "\n\nschedule 指定触发方式："
            '{"kind":"at","at":"2026-07-07T11:20:00"}（一次性时刻，跑完自动停）、'
            '{"kind":"every","every_seconds":3600}（固定间隔）、'
            '{"kind":"cron","expr":"0 8 * * *"}（cron 表达式，5 字段：分 时 日 月 周）。'
            "\n\npayload 指定到点执行的内容："
            '{"kind":"tool","tool_name":"ha_devices___call_service","tool_input":{"domain":"light","service":"turn_off","entity_id":"light.bedroom"}}（调工具，如控制设备）'
            ' 或 {"kind":"reminder","intent":"下班提醒","original":"在18点27分提醒我下班"}（提醒场景：存用户原始意图，到点由 AI 主动组织语言提醒，不要预设固定话术）'
            ' 或 {"kind":"message","message":"该起床了"}（发固定文本，仅当内容完全确定时用）。'
            "\n\n例1：'11点20分开厨房灯' -> schedule={kind:at, at:'2026-07-07T11:20:00'}, "
            "payload={kind:tool, tool_name:'ha_devices___call_service', tool_input:{domain:light, service:turn_on, entity_id:light.chu_fang_deng}}"
            "\n例2：'每天8点提醒起床' -> schedule={kind:cron, expr:'0 8 * * *'}, payload={kind:reminder, intent:'提醒起床', original:'每天8点提醒起床'}"
            "\n例3：'在18点27分提醒我下班' -> schedule={kind:at, at:'2026-07-08T18:27:00'}, payload={kind:reminder, intent:'下班提醒', original:'在18点27分提醒我下班'}"
            "\n\n提醒类任务一律用 kind=reminder（带 intent + original），不要用 kind=message。创建成功后只需简短确认一句，不要重复说明。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "任务名称"},
                "schedule": {
                    "type": "object",
                    "description": "触发配置，见工具描述",
                    "properties": {
                        "kind": {"type": "string", "enum": ["at", "every", "cron"]},
                        "at": {"type": "string", "description": "ISO 时刻，kind=at 时必填"},
                        "every_seconds": {"type": "number", "description": "间隔秒数，kind=every 时必填"},
                        "expr": {"type": "string", "description": "cron 表达式，kind=cron 时必填"},
                    },
                    "required": ["kind"],
                },
                "payload": {
                    "type": "object",
                    "description": "执行内容，见工具描述",
                    "properties": {
                        "kind": {"type": "string", "enum": ["tool", "message", "reminder"]},
                        "tool_name": {"type": "string", "description": "kind=tool 时必填，要调用的 MCP 工具全名"},
                        "tool_input": {"type": "object", "description": "kind=tool 时，传给工具的参数"},
                        "message": {"type": "string", "description": "kind=message 时必填，往主会话发的固定文本"},
                        "intent": {"type": "string", "description": "kind=reminder 时必填，提醒意图简述（如'下班提醒'）"},
                        "original": {"type": "string", "description": "kind=reminder 时建议填，用户创建时的原话（如'在18点27分提醒我下班'）"},
                    },
                    "required": ["kind"],
                },
            },
            "required": ["name", "schedule", "payload"],
        },
        handler=create_handler,
    ))

    async def list_handler(_: dict, session) -> dict:
        svc = _svc()
        if svc is None:
            return {"error": "调度器未就绪"}
        tasks = await svc.list_tasks()
        return {"tasks": tasks, "count": len(tasks)}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="scheduled_task_list",
        description="列出所有定时任务",
        parameters={"type": "object", "properties": {}},
        handler=list_handler,
    ))

    async def delete_handler(parameters: dict, session) -> dict:
        svc = _svc()
        if svc is None:
            return {"error": "调度器未就绪"}
        task_id = str(parameters.get("task_id", "")).strip()
        if not task_id:
            return {"error": "task_id 不能为空"}
        await svc.delete_task(task_id)
        return {"success": True, "task_id": task_id}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="scheduled_task_delete",
        description="删除一个定时任务",
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "任务 ID"}},
            "required": ["task_id"],
        },
        handler=delete_handler,
    ))


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

async def connect_external_mcp_servers(mcp_client_manager: MCPClientManager) -> None:
    """后台并行连接外部 MCP server。"""
    from .core.config import get_config

    external_cfg = get_config("external_mcp", [])
    if not external_cfg:
        logger.info("No external MCP servers configured, skipping")
        return

    async def _connect(name: str, cmd: str, args: list[str]) -> None:
        try:
            tools = await asyncio.wait_for(
                mcp_client_manager.connect_external_server(name, cmd, args),
                timeout=60,
            )
            logger.info("External MCP %s connected", name, extra={"tools": len(tools)})
        except Exception:
            logger.info("External MCP %s not available (optional, skipped)", name)

    tasks = []
    for entry in external_cfg:
        name = entry.get("name", "")
        cmd = entry.get("cmd", "")
        args = entry.get("args", [])
        if name and cmd:
            tasks.append(_connect(name, cmd, args))

    if tasks:
        await asyncio.gather(*tasks)
