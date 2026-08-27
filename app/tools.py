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
from .services.entity_controls import resolve_controls, controls_to_text
from .services.control_probe import call_with_probe
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
    vision_client: Any
    ha_service: Any
    # 可变引用：ha_client 可能被热替换
    ha_client_ref: list  # [HomeAssistantClient]
    # 多路 CameraManager(唯一摄像头来源)。lifespan 后段注入,handler 调用时读。
    camera_manager: Any = None
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
    # 3b. 设备说明书（按需拉单台详情+备注）
    _register_ha_get_device_manual(deps)
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
        camera_id = str(parameters.get("camera_id", "") or "").strip()
        # 多路取帧三级:用户指定 → 当前预览路(_active_display_id)→ 第一个 enabled。
        used_camera_id = camera_id
        if deps.camera_manager is None:
            return {"answer": "摄像头未配置,无法分析。", "question": question, "has_frame": False}
        if not used_camera_id:
            used_camera_id = getattr(deps.camera_manager, "_active_display_id", "") or ""
        if not used_camera_id:
            cams = deps.camera_manager.list_cameras()
            if cams:
                used_camera_id = cams[0]["id"]
        if not used_camera_id:
            return {"answer": "摄像头当前没有画面,无法分析。", "question": question, "has_frame": False}
        # 多帧:取规则引擎环形缓冲的最近几帧(frame_interval_ms 采样、时间有序),
        # 模型能结合帧间变化回答"正在做什么";缓冲为空(刚启动还没攒够采样)回退最新单帧。
        frames = deps.camera_manager.get_recent_frames(used_camera_id, 3)
        if not frames:
            latest = deps.camera_manager.get_frame(used_camera_id)
            frames = [latest] if latest is not None else []
        if not frames:
            return {"answer": "摄像头当前没有画面,无法分析。", "question": question, "has_frame": False}
        answer = await deps.vision_client.ask_about_frames(frames, question)
        return {"answer": answer, "question": question, "has_frame": True,
                "camera_id": used_camera_id, "model": deps.vision_client.model,
                "frames_used": len(frames)}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="vision_chat",
        description="拍摄指定摄像头的最近连续画面，根据画面内容和变化回答用户问题（可用于判断动作/状态）。可用摄像头列表请调用 get_entities 查看。",
        parameters={"type": "object", "properties": {
            "question": {"type": "string"},
            "camera_id": {"type": "string", "description": "可选,指定摄像头ID;不传取当前查看路"},
        }},
        handler=handler,
    ))


def _register_ha_get_entities(deps: ToolDeps) -> None:
    async def handler(_: dict, session) -> dict:
        # 数据源收敛：与 system prompt 的 catalog/controls 共用 device_registry
        # 快照（此处每次现拉保持实时）。禁止设备在快照层已排除，模型不可见。
        try:
            from .services.device_registry import (
                build_device_snapshot, render_devices_brief, render_entities_flat,
            )
            snapshot = await build_device_snapshot(deps.ha_service, deps.ha_client_ref[0])
            services_info = {
                domain: {svc_name: svc_def["fields"] for svc_name, svc_def in svcs.items()}
                for domain, svcs in snapshot["service_defs"].items()
            }
            devices_brief = render_devices_brief(snapshot)
            return {
                "devices": devices_brief,     # 精简物理设备列表（供回答「有哪些设备」）
                "entities": render_entities_flat(snapshot),  # 扁平实体列表（含 _controls，供 call_service）
                "count": len(devices_brief),
                "services": services_info,
            }
        except Exception as e:
            logger.exception("HA get_entities failed")
            return {"entities": [], "devices": [], "count": 0, "error": str(e)}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="ha_devices",
        tool_name="get_entities",
        description=(
            "获取家中所有智能设备。返回 devices 和 entities 两个字段：\n"
            "- devices：物理设备列表，每项一个物理设备（如「小爱音箱Pro」「大门通断器」），"
            "含 name/area/summary/entity_ids/entity_labels（entity_id → 含子功能短名的显示名，"
            "如「A灯 会客厅灯 左键」）。向用户介绍「有哪些设备」时，直接用 devices 的 name 逐个列出，"
            "不要把同一物理设备下的传感器/开关/子功能当成多个设备分别念出。\n"
            "- entities：扁平实体列表（含 entity_id/domain/_controls），控制设备时从这里取 "
            "domain/service/entity_id/data 调用 call_service。\n"
            "一个物理设备可能对应多个 entity_id（不同功能点），用户说一个设备名时可能命中其中任意一个——"
            "用户用子功能名指称（如「打开会客厅的灯」命中某设备的「会客厅灯 左键」子功能）时，"
            "按 entity_labels 匹配最合适的。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=handler,
    ))


def _register_ha_get_device_manual(deps: ToolDeps) -> None:
    async def handler(parameters: dict, session) -> dict:
        # 按需拉单台/多台设备的完整可控项明细 + 用户备注。
        # 阶段一：作为补充手段，LLM 控制不熟悉或有怪癖的设备前可主动调用看详情。
        try:
            raw = str(parameters.get("entity_ids", "") or "").strip()
            if not raw:
                return {"manuals": "", "found": [], "missing": [], "error": "entity_ids 不能为空"}
            eid_list = [e.strip() for e in raw.split(",") if e.strip()]
            devices = await deps.ha_service.get_all_devices()
            raw_svc_defs = await deps.ha_service.get_service_defs(
                deps.ha_client_ref[0], domains=set(d.get("domain", "") for d in devices)
            )
            # 备注按 entity_id 查（一次读全部，O(1) 查 dict）
            notes_map: dict[str, str] = {}
            try:
                from .core.database import Database
                notes_map = await Database.get().prefs_get_by_scope("entity_note")
            except Exception:  # noqa: BLE001
                logger.warning("get_device_manual: 备注读取失败", exc_info=True)

            dev_by_eid = {d["entity_id"]: d for d in devices}
            found: list[str] = []
            missing: list[str] = []
            blocks: list[str] = []
            for eid in eid_list:
                dev = dev_by_eid.get(eid)
                if not dev:
                    missing.append(eid)
                    continue
                found.append(eid)
                # 语义映射：对称翻转对设备预翻转 state（controls current 跟着对）
                try:
                    from .services.semantic_map import flip_state_value
                    dev = {**dev, "state": await flip_state_value(eid, str(dev.get("state", "")))}
                except Exception:  # noqa: BLE001
                    logger.warning("get_device_manual: state 翻转失败", exc_info=True)
                controls = resolve_controls(dev, raw_svc_defs)
                blocks.append(
                    controls_to_text(dev, controls, note=notes_map.get(eid))
                )
            return {
                "manuals": "\n\n".join(blocks) if blocks else "(无匹配设备)",
                "found": found,
                "missing": missing,
            }
        except Exception as e:
            logger.exception("get_device_manual failed")
            return {"manuals": "", "found": [], "missing": [], "error": str(e)}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="ha_devices",
        tool_name="get_device_manual",
        description=(
            "查询单台或多台设备的详细操作手册（含 domain/service/param 明细和用户自定义备注）。"
            "控制不熟悉的设备、或设备有特殊语义（如继电器 ON=关门、需调 turn_off）时调用本工具。"
            "支持传一个或多个 entity_id（逗号分隔）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "entity_ids": {
                    "type": "string",
                    "description": "一个或多个 entity_id，逗号分隔",
                },
            },
            "required": ["entity_ids"],
        },
        handler=handler,
    ))


def _register_ha_call_service(deps: ToolDeps) -> None:
    async def handler(parameters: dict, session) -> dict:
        # 本地模型（Ollama）常在工具参数首尾带空格，导致 entity_id/domain/service
        # 精确匹配失败（如 " light.chuang_tou_deng " 校验不存在）。入口统一 strip。
        domain = str(parameters.get("domain", "")).strip()
        service = str(parameters.get("service", "")).strip()
        entity_id = parameters.get("entity_id")
        if isinstance(entity_id, str):
            entity_id = entity_id.strip()
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
                        error = (
                            f"entity_id '{', '.join(missing)}' 不存在于 Home Assistant，"
                            "无法控制。"
                        )
                        # 自愈回路：用注册表快照反查用户指令命中的真实实体，附进
                        # 报错让 LLM 用候选重试一次（entries 已排除禁止项，与
                        # 模型视野同源——视图里看得见的才可能成为候选）。
                        fallback_hint = "请用 get_entities 查看真实设备列表，不要编造 entity_id。"
                        query = getattr(session, "current_query", "") or ""
                        if query:
                            try:
                                from .services.device_registry import (
                                    build_device_snapshot, entry_label,
                                )
                                snapshot = await build_device_snapshot(deps.ha_service, ha_client)
                                matched = match_devices(query, snapshot["entries"])[:5]
                                if matched:
                                    cand_text = "、".join(
                                        f"{d['entity_id']}（{entry_label(d)}）" for d in matched
                                    )
                                    error += (
                                        f"用户说的是「{query}」，可能匹配：{cand_text}。"
                                        "请从候选中选最合适的一个重试一次；都不合适则如实告知设备不存在。"
                                    )
                                else:
                                    error += "用户指令没有匹配到任何真实设备，请如实告知设备不存在，不要编造。"
                            except Exception:  # noqa: BLE001
                                logger.warning("call_service: 候选反查失败", exc_info=True)
                                error += fallback_hint
                        else:
                            error += fallback_hint
                        return {"success": False, "error": error}
                except Exception:
                    logger.warning("call_service: entity_id 校验失败，放行", exc_info=True)
            # 授权校验：用户可在设备页把危险设备（童锁/门锁）标为禁止 AI 操作。
            # 读 entity_operable 黑名单，命中则拒绝。DB 异常时放行（避免锁死全屋）。
            if entity_id:
                try:
                    from .core.database import Database
                    disabled = await Database.get().prefs_get_by_scope("entity_operable")
                    eid_list_op = [e.strip() for e in str(entity_id).split(",") if e.strip()]
                    blocked = [e for e in eid_list_op if e in disabled]
                    if blocked:
                        names = "、".join(blocked)
                        logger.info("call_service 拒绝未授权 entity_id: %s", blocked)
                        return {
                            "success": False,
                            "error": (
                                f"设备「{names}」被用户设为禁止 AI 操作。请勿尝试调用，"
                                "如实告知用户需手动操作或在设备页解除限制。"
                            ),
                        }
                except Exception:
                    logger.warning("call_service: 授权校验失败，放行", exc_info=True)
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
            # 语义映射过滤：无条件替换 service（不依赖意图判断，避免双重错误）。
            # AI 凭直觉调用，过滤器无条件纠正，结果反馈事后解释。
            # 批量 entity_id：仅当全部实体对同一 service 映射到相同 target（共识）
            # 才替换 —— 混合设备批量控制下按单实体映射替换会误伤未映射设备。
            original_service = service
            mapped_description = None
            eid_list = [e.strip() for e in str(entity_id).split(",") if e.strip()] if entity_id else []
            if eid_list:
                try:
                    from .services.semantic_map import get_action_map
                    targets = []
                    for e in eid_list:
                        am = await get_action_map(e)
                        entry = (am or {}).get("mappings", {}).get(service)
                        targets.append(entry.get("target") if isinstance(entry, dict) else None)
                    if targets[0] and targets[0] != service and all(t == targets[0] for t in targets):
                        service = targets[0]
                        mapped_description = ""
                        am0 = await get_action_map(eid_list[0])
                        entry0 = am0.get("mappings", {}).get(original_service)
                        if isinstance(entry0, dict):
                            mapped_description = entry0.get("description", "")
                        logger.info("call_service 语义映射: %s.%s → %s",
                                    entity_id, original_service, service)
                except Exception:  # noqa: BLE001
                    logger.warning("call_service: 语义映射查询失败，放行原 service", exc_info=True)
            result = await call_with_probe(ha_client, domain, service, entity_id, data)
            new_state = None
            new_state_eid = None
            if eid_list:
                try:
                    states = await ha_client.get_states()
                    states_by_id = {s.get("entity_id"): s for s in states}
                    # 批量时取第一个有状态的实体作代表
                    for e in eid_list:
                        if e in states_by_id:
                            s = states_by_id[e]
                            new_state = {"state": s.get("state"), "attributes": s.get("attributes", {})}
                            new_state_eid = e
                            break
                except Exception:
                    pass
            ret: dict = {"success": True, "result": result, "new_state": new_state}
            if service != original_service:
                # 动作被映射 → 带描述，让 AI 理解实际发生了什么、如何汇报给用户
                ret["semantic_mapping"] = {
                    "requested": original_service,
                    "executed": service,
                    "description": mapped_description or "该设备配置了语义映射",
                }
            # 对称翻转对 → state 无条件隐含翻转（toggle 等未映射动作同样生效，
            # 避免 AI 看到物理原始值说反话）。非翻转设备 apply_state_flip 原样返回。
            if new_state and new_state.get("state") in ("on", "off") and new_state_eid:
                try:
                    from .services.semantic_map import apply_state_flip
                    ret["new_state"] = apply_state_flip(new_state, new_state_eid)
                except Exception:  # noqa: BLE001
                    logger.warning("call_service: state 翻转失败，放行原 state", exc_info=True)
            return ret
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
        deps.vision_client, deps.ha_client_ref[0],
        camera_manager=deps.camera_manager,
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
