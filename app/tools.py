"""MCP 工具注册中心 — 所有内置工具的注册入口。

加工具只需在此文件添加，不需要改 main.py。
handler 通过参数接收依赖，不闭包全局变量（支持 HA 热替换等场景）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .mcp.local_mcp_servers import (
    register_local_tools,
)
from .mcp.mcp_client_manager import MCPClientManager, MCPTool
from .services.entity_controls import resolve_controls, controls_to_text
from .services.control_probe import call_with_probe
from .services.pending_rules import (
    KIND_AUTOMATION_RULE,
    PENDING_TTL_SECONDS,
    confirm_pending,
    locate_pending,
    needs_camera,
    pending_store,
    wants_rule_creation,
)
from .services.pending_selections import create_selection_draft, drop_selection_drafts
from .utils.text_match import (
    TIER_ALL_MARKER,
    TIER_AMBIGUOUS,
    TIER_CATEGORY_MISS,
    TIER_EXACT,
    TIER_NONE,
    classify_target,
    match_devices,
)

logger = logging.getLogger(__name__)


def tool_error(reason: str, *, hint: str | None = None,
               candidates: list[str] | None = None, **extra: Any) -> dict:
    """构造结构化工具错误返回：{"error": reason, "hint": ..., "candidates": [...]}。

    「错误信息即提示词」：小模型报错后最大的失败源是不知道怎么改，所以错误
    返回必须携带下一步指引——hint 说明该怎么修正，candidates 给出可选实体/
    场景等候选。langchain_tools 检测到 "error" 键会包成 Error: 前缀字符串
    （触发 is_error 检测与失败重试回路），并把 hint/candidates 以「修正提示/
    候选」行渲染给模型。extra 中的附加字段（如 blocked_entities）原样出现在
    原始返回 JSON 里。
    """
    err: dict = {"error": reason}
    if hint:
        err["hint"] = hint
    if candidates:
        err["candidates"] = candidates
    if extra:
        err.update(extra)
    return err


def _need_selection(session, query: str, res: Any, domain: str, service: str,
                    data: dict) -> dict:
    """构造「转用户选择」的工具返回（消歧闸门用）。

    **不得含 "error" 键** —— 这是硬约束，不是风格偏好：
    langchain_tools 见到 "error" 就加 `Error:` 前缀 → langgraph_agent 据此判
    is_error → dispatcher 把它塞进 failed_tools 触发失败重试轮，模型会被逼着
    「修正」自己再猜一个实体，正好是本闸门要拦住的行为。

    所以走 success 形状（与 automation_rule_create 的 pending_confirm 同构）：
    前端在 Template.CallToolResult 里按 status 识别，并在 Dialog.Finish 后弹框。
    草稿挂不上时也照样返回 need_selection（退化为纯口头确认），绝不放行执行 ——
    歧义指令宁可不执行，也不能默默挑一个。
    """
    candidates = [
        {
            "entity_id": c.get("entity_id"),
            "label": str(c.get("label") or c.get("name") or c.get("entity_id") or ""),
            "domain": c.get("domain"),
            "area_name": c.get("area_name"),
            "state": c.get("state", ""),
        }
        for c in res.candidates
    ]
    labels = [c["label"] for c in candidates]
    if res.tier == TIER_CATEGORY_MISS:
        # 必须如实说设备不存在：用户说的是「月球的灯」，不能假装找到了它
        notice = f"没有找到名为「{query}」的设备。"
    else:
        notice = f"用户说的是「{query}」，匹配到 {len(labels)} 个设备，无法确定是哪一个。"
    try:
        pending_id = create_selection_draft(
            session, query=query, domain=domain, service=service, data=data,
            candidates=candidates, reason=res.tier,
        )
    except Exception:
        logger.warning("call_service: 待选草稿创建失败，退化为口头确认", exc_info=True)
        pending_id = ""
    logger.info("call_service 转用户选择(%s): query=%r candidates=%s",
                res.tier, query, labels)
    return {
        "success": False,
        "status": "need_selection",
        "pending_id": pending_id,
        "reason": res.tier,
        "query": query,
        "notice": notice,
        "candidates": candidates,
        "action": {"domain": domain, "service": service, "data": data},
        "hint": (
            f"{notice}候选是：{'、'.join(labels)}。已请用户选择（网页端会弹框勾选，"
            "语音端请你口头列举让用户挑）。本轮到此为止：不要再调用任何设备工具，"
            "不要自己挑一个，不要重试，也不要声称已执行，只需等用户选定；"
            "向用户说明需要确认是哪个设备即可。之后的轮次不受此限制："
            "再遇到控制指令仍正常调用 call_service，模糊目标交给系统处理。"
        ),
    }


# ---------------------------------------------------------------------------
# call_service 回读校验（「每控必核」）
# ---------------------------------------------------------------------------

# 回读前等待：HA 状态经状态机异步传播，紧贴调用读会拿到旧值。
_CALL_SERVICE_READBACK_DELAY = 0.4

_ON_STATES = {"on", "open"}
_OFF_STATES = {"off", "closed"}
# 不可信状态：回读落到这些值上视为"未生效"而非"符合预期"
_UNRELIABLE_STATES = {"unavailable", "unknown", "none"}


async def _states_for_existence_check(ha_service: Any, ha_client: Any) -> list:
    """entity_id 存在性校验取全量 states：优先 ha_service 的 5s TTL 缓存，
    没有缓存访问器（旧测试桩/异构装配）时退回直拉——实体不会在 5 秒内
    出现/消失，校验不值得绕过缓存多付一次全量拉取。"""
    try:
        states = await ha_service.get_states_snapshot()
        if isinstance(states, list):
            return states
    except (AttributeError, TypeError):
        pass
    return await ha_client.get_states()


def _enabled_cameras(deps: "ToolDeps") -> list[dict]:
    """启用的摄像头 [{"id","name"}]，供拒绝落库时给模型候选名字。

    取不到（camera_manager 未装配 / 抛错）返回空表——校验本身不依赖这份列表，
    它只影响提示语里能不能列出可选项。
    """
    manager = getattr(deps, "camera_manager", None)
    if manager is None:
        return []
    try:
        cameras = manager.list_cameras()
    except Exception:  # noqa: BLE001
        return []
    return [{"id": str(c.get("id", "")), "name": str(c.get("name") or c.get("id") or "")}
            for c in (cameras or [])
            if isinstance(c, dict) and c.get("enabled") is not False and c.get("id")]


async def _readback_entity_state(ha_client: Any, eid_list: list[str]) -> tuple[dict | None, str | None]:
    """「每控必核」回读：逐实体 GET /api/states/{id}，只取第一个命中实体；
    客户端无单实体能力（旧测试桩）时退化全量拉取。返回 (state|None, entity_id|None)。"""
    try:
        for e in eid_list:
            s = await ha_client.get_state(e)
            if s is not None:
                return s, e
        return None, None
    except (AttributeError, TypeError):
        states = await ha_client.get_states()
        by_id = {s.get("entity_id"): s for s in states}
        for e in eid_list:
            if e in by_id:
                return by_id[e], e
        return None, None


def _verify_readback(service: str, data: dict, new_state: dict | None) -> tuple[bool | None, str]:
    """把回读状态与本次指令的预期直接比对（代码级，不依赖模型记得调 verify_action）。

    返回 (verified, detail)：
    - True：所有可比对项均符合预期；
    - False：存在不符项，detail 说明差异；
    - None：无可断言项（toggle、无 data 且非开关类等），不下结论。

    过渡态说明：cover 类下发后可能短暂处于 opening/closing（未到位但已生效），
    既不在开态也不在关态，按通过处理，不产生误报。
    """
    if not new_state:
        return None, ""
    state = str(new_state.get("state", "")).lower()
    attrs = new_state.get("attributes") or {}
    failures: list[str] = []
    asserted = False

    # 开/关类服务：比对 state 本体
    if service in ("turn_on", "open_cover", "open_valve"):
        asserted = True
        if state in _OFF_STATES | _UNRELIABLE_STATES:
            failures.append(f"期望开启，实际 state={state}")
    elif service in ("turn_off", "close_cover", "close_valve"):
        asserted = True
        if state in _ON_STATES | _UNRELIABLE_STATES:
            failures.append(f"期望关闭，实际 state={state}")

    # 带参数的服务（set_temperature/set_percentage 等）：把 data 的每个键在
    # 回读 attributes 中找对应值比对；键与 current_ 前缀变体都不存在时跳过
    # （如 brightness_pct → brightness 的映射差异），避免误报。
    for key, expected in (data or {}).items():
        actual = attrs.get(key)
        if actual is None:
            actual = attrs.get(f"current_{key}")
        if actual is None:
            continue
        asserted = True
        try:
            matched = float(expected) == float(actual)
        except (ValueError, TypeError):
            matched = str(expected) == str(actual)
        if not matched:
            failures.append(f"{key} 期望 {expected}，实际 {actual}")

    if not asserted:
        return None, ""
    return (not failures), "；".join(failures)


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
    # 自动化规则三件套：lifespan 前已建好，直接引用（chat 建规则 / 落库 / 手动触发）
    rule_service: Any = None
    rule_registry_service: Any = None
    automation_service: Any = None


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
    # 动作正确性由 call_service 的「每控必核」回读在代码层保证，不再暴露 verify 工具
    # 7. 定时任务管理（让 agent 能对话建/查/删定时任务）
    _register_scheduled_task_tools(deps)
    # 8. 场景模式（一键切换一组设备到预设状态）
    _register_scene_tools(deps)
    # 9. 自动化规则管理（对话建规则=两段式确认，查询/删除/手动触发直达）
    _register_automation_rule_tools(deps)


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
        # 离线如实告知：缓冲里留着的是断连前的旧帧，拿去问模型等于让 AI 编造
        # 实时场景（拔了摄像头还一本正经描述"画面里有人"）。明确返回离线状态。
        cam_state = deps.camera_manager.get_state(used_camera_id) or {}
        if not cam_state.get("camera_opened"):
            return {"answer": "摄像头当前离线，无法查看实时画面。（最后画面停留在断连之前，不代表现在的场景）",
                    "question": question, "has_frame": False,
                    "camera_id": used_camera_id, "camera_offline": True}
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
        description="拍摄指定摄像头的最近连续画面，根据画面内容和变化回答用户问题（可用于判断动作/状态）。可用摄像头列表请调用 get_entities 查看。返回 has_frame=false 且带 camera_offline=true 表示摄像头已离线——此时必须如实告知用户摄像头离线，禁止描述画面内容。",
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
            err = tool_error(
                str(e),
                hint="Home Assistant 可能离线或不可用。请如实告知用户设备服务暂不可用、稍后重试，禁止编造设备列表。",
            )
            err.update({"entities": [], "devices": [], "count": 0})
            return err

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
                return {"manuals": "", "found": [], "missing": [],
                        **tool_error("entity_ids 不能为空",
                                     hint="传入一个或多个 entity_id（逗号分隔）；不确定 ID 时先调 get_entities。")}
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
            except Exception:
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
                except Exception:
                    logger.warning("get_device_manual: state 翻转失败", exc_info=True)
                controls = resolve_controls(dev, raw_svc_defs)
                blocks.append(
                    controls_to_text(dev, controls, note=notes_map.get(eid))
                )
            ret = {
                "manuals": "\n\n".join(blocks) if blocks else "(无匹配设备)",
                "found": found,
                "missing": missing,
            }
            if missing:
                # 缺失实体也带修正提示：模型常拿着编造/过期的 ID 反复重试
                ret["hint"] = (
                    f"missing 中的 entity_id 不存在（{', '.join(missing)}），"
                    "请用 get_entities 核对真实 ID，不要用相同 ID 重试。"
                )
            return ret
        except Exception as e:
            logger.exception("get_device_manual failed")
            err = tool_error(str(e), hint="Home Assistant 可能不可用，请稍后重试并如实告知用户。")
            err.update({"manuals": "", "found": [], "missing": []})
            return err

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
            # 支持逗号分隔的批量 entity_id，逐个校验（优先走 5s TTL 缓存）。
            if entity_id:
                try:
                    states = await _states_for_existence_check(deps.ha_service, ha_client)
                    real_ids = {s.get("entity_id") for s in states}
                    eid_list = [e.strip() for e in str(entity_id).split(",") if e.strip()]
                    missing = [e for e in eid_list if e not in real_ids]
                    if missing:
                        logger.info("call_service 拒绝编造 entity_id: %s", missing)
                        # 自愈回路：用注册表快照反查用户指令命中的真实实体，作为
                        # candidates 附进报错让 LLM 用候选重试一次（entries 已排除
                        # 禁止项，与模型视野同源——视图里看得见的才可能成为候选）。
                        err = tool_error(
                            f"entity_id '{', '.join(missing)}' 不存在于 Home Assistant，无法控制。",
                            hint="请用 get_entities 查看真实设备列表，不要编造 entity_id。",
                        )
                        query = getattr(session, "current_query", "") or ""
                        if query:
                            try:
                                from .services.device_registry import (
                                    build_device_snapshot, entry_label,
                                )
                                snapshot = await build_device_snapshot(deps.ha_service, ha_client)
                                matched = match_devices(query, snapshot["entries"])[:5]
                                if matched:
                                    err["candidates"] = [
                                        f"{d['entity_id']}（{entry_label(d)}）" for d in matched
                                    ]
                                    err["hint"] = (
                                        f"用户说的是「{query}」，请从候选中选最合适的一个重试一次；"
                                        "都不合适则如实告知设备不存在。"
                                    )
                                else:
                                    err["hint"] = "用户指令没有匹配到任何真实设备，请如实告知设备不存在，不要编造。"
                            except Exception:
                                logger.warning("call_service: 候选反查失败", exc_info=True)
                        return {"success": False, **err}
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
                            **tool_error(
                                f"设备「{names}」被用户设为禁止 AI 操作，调用被拒绝。",
                                hint="请勿再次尝试调用该设备；如实告知用户需手动操作，或在设备页解除限制。",
                                blocked_entities=blocked,
                            ),
                        }
                except Exception:
                    logger.warning("call_service: 授权校验失败，放行", exc_info=True)
            # query→entity 消歧闸门：按 classify_target 的分层决定「直接执行 /
            # 整组执行 / 转用户选择 / 放行」。
            # - exact / all_marker：用户说得够明确（精确同名，或明说「所有/全部/都」），
            #   把模型只挑了一个的 entity_id 补全成整组，避免「开B灯」只开了一半。
            # - ambiguous / category_miss：不猜、不执行，挂草稿转用户选择（网页弹框 /
            #   语音口头列举）。「月球的灯」把全屋灯开掉的根因就在这里——旧逻辑
            #   matched 为空即放行，模型只能从全量目录里随便抓一个。
            # - unique：目标不在候选内才拒（防语义近邻顶替，如「打开加湿器」却操作
            #   带除湿模式的空调）。
            # - none：放行（无法区分"设备不在列表"与"泛指无设备名"，避免误伤
            #   "太热了→开空调"这类合理推断；该场景靠 system prompt 注入兜底软约束）。
            query = getattr(session, "current_query", "") or ""
            executed_labels: dict = {}
            if query and entity_id:
                try:
                    # 延迟导入：device_registry 依赖 ha_service，模块级导入会成环
                    from .services.device_registry import build_match_index
                    index = await build_match_index(deps.ha_service)
                    # 禁止项对 AI 不可见：候选与扩展集都必须滤掉黑名单实体，
                    # 否则被禁设备名会出现在回给模型的 candidates / 弹框候选里。
                    try:
                        from .core.database import Database as _Database
                        _disabled = await _Database.get().prefs_get_by_scope("entity_operable")
                        index = [e for e in index if e.get("entity_id") not in _disabled]
                    except Exception:
                        logger.warning("call_service: 禁控过滤失败，放行", exc_info=True)
                    # 记录 entity_id → 人读名，供结果摘要展示全部已执行设备
                    # （设备级 exact/all_marker 扩展后，真实下发集可能大于模型给的
                    # 单个实体，如双键墙壁开关「开B灯」实际开两个键）。
                    executed_labels = {
                        str(c.get("entity_id")): str(c.get("label") or c.get("name") or "")
                        for c in index
                    }
                    res = classify_target(query, index)
                    gate_eids = [e.strip() for e in str(entity_id).split(",") if e.strip()]
                    cand_ids = {str(c.get("entity_id", "")) for c in res.candidates}
                    if res.tier in (TIER_AMBIGUOUS, TIER_CATEGORY_MISS):
                        return _need_selection(session, query, res, domain, service, data)
                    # effective = 本轮真正要下发的目标集。默认按模型给的；exact/all_marker
                    # 扩展成功后以扩展集为准（它是候选的子集，天然不算错配）。
                    effective = gate_eids
                    if res.tier in (TIER_EXACT, TIER_ALL_MARKER):
                        # 只在模型所选 domain 内扩展：设备「大门」下同时有 switch 和
                        # lock 时，「开大门」不得顺手把门锁一起开了。
                        target_domain = gate_eids[0].split(".", 1)[0] if gate_eids else domain
                        expanded = [str(c["entity_id"]) for c in res.candidates
                                    if str(c.get("domain") or "") == target_domain]
                        if expanded:
                            effective = expanded
                            if set(expanded) != set(gate_eids):
                                logger.info("call_service 消歧扩展(%s): query=%r %s → %s",
                                            res.tier, query, gate_eids, expanded)
                                entity_id = ",".join(expanded)
                        # expanded 为空 = 模型选的 domain 与用户点名的设备完全不同类
                        # （「打开加湿器」却去开 switch），落到下面的错配拒绝
                    if res.tier != TIER_NONE and not any(e in cand_ids for e in effective):
                        labels = [str(c.get("label") or c.get("name") or c.get("entity_id", ""))
                                  for c in res.candidates]
                        logger.info(
                            "call_service 拒绝语义错配: query=%r tier=%s matched=%s target=%s",
                            query, res.tier, cand_ids, gate_eids,
                        )
                        return {
                            "success": False,
                            **tool_error(
                                f"用户说的是「{query}」，匹配到的设备是「{'、'.join(labels)}」，"
                                f"与目标 {entity_id} 不符。",
                                hint="不要用语义相近的实体顶替；若用户提到的设备确实不存在，请如实告知。",
                                candidates=labels,
                            ),
                        }
                    if res.tier != TIER_NONE:
                        # 干净解决一轮指令 → 清掉遗留草稿（语音用户被问「要开哪个」后
                        # 直接说设备名，走的就是这条路）。本轮 query 的草稿要留着：
                        # 同轮第二次工具调用不能把弹框刚拿到的 pending_id 抹掉。
                        dropped = drop_selection_drafts(session, except_query=query)
                        if dropped:
                            logger.info("call_service 清理遗留待选草稿 %d 份", dropped)
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
                except Exception:
                    logger.warning("call_service: 语义映射查询失败，放行原 service", exc_info=True)
            result = await call_with_probe(ha_client, domain, service, entity_id, data)
            new_state = None
            new_state_eid = None
            state_check_failed = False
            if eid_list:
                # 等状态传播后再回读，紧贴调用读会拿到旧值。
                # 逐实体 GET /api/states/{id}：回读只为取第一个可控实体的状态，
                # 不值得为它全量拉 /api/states（实体多时一次数百 KB）。
                await asyncio.sleep(_CALL_SERVICE_READBACK_DELAY)
                try:
                    new_state, new_state_eid = await _readback_entity_state(ha_client, eid_list)
                    if new_state is not None:
                        new_state = {"state": new_state.get("state"),
                                     "attributes": new_state.get("attributes", {})}
                except Exception:
                    # 指令已执行但状态未经核实：必须显式告知 AI，不能静默当作
                    # 有状态反馈（此前 except-pass → AI 在状态未知时照常确认成功）
                    logger.warning("call_service: 状态回查失败，标记状态未知", exc_info=True)
                    state_check_failed = True
            ret: dict = {"success": True, "result": result, "new_state": new_state}
            if executed_labels:
                names = [executed_labels.get(e, e) for e in eid_list]
                if any(names):
                    ret["names"] = names
            # 「每控必核」：代码级回读校验，不依赖模型记得提示词里的三步走。
            # 比对不符不标 error（不触发失败重试回路对设备重复下发指令），
            # 只附 verified=False + note，让模型如实汇报当前实际状态。
            if new_state and not state_check_failed:
                verified, detail = _verify_readback(service, data, new_state)
                if verified is True:
                    ret["verified"] = True
                elif verified is False:
                    ret["verified"] = False
                    ret["note"] = (
                        f"指令已发送，但回读状态与预期不符（{detail}）。"
                        "设备可能未生效或仍在响应中：不要谎报成功，如实告知用户当前实际状态。"
                    )
                    logger.info("call_service 回读校验不符: %s.%s → %s（%s）",
                                domain, service, new_state.get("state"), detail)
            # 主控操作落 device_op 事件（周报「AI 操作设备」统计的数据源）
            try:
                from .services.device_event_service import record_device_op
                name_of = {}
                if new_state_eid and new_state:
                    friendly = (new_state.get("attributes") or {}).get("friendly_name")
                    if friendly:
                        name_of[new_state_eid] = str(friendly)
                await record_device_op(eid_list, service, "AI", name_of)
            except Exception:
                logger.debug("record device_op failed", exc_info=True)
            if state_check_failed:
                ret["state_check"] = "failed"
                ret["note"] = (
                    "设备指令已发送，但回读最新状态失败，当前状态未经核实。"
                    "请如实告知用户指令已下发但未能确认执行结果。"
                )
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
                except Exception:
                    logger.warning("call_service: state 翻转失败，放行原 state", exc_info=True)
            return ret
        except Exception as e:
            logger.exception("HA call_service failed")
            return {
                "success": False,
                **tool_error(
                    str(e),
                    hint="Home Assistant 可能不可用或参数不合法；核对参数后可重试一次，仍失败则如实告知用户。",
                ),
            }

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="ha_devices",
        tool_name="call_service",
        description=(
            "调用 Home Assistant 服务来控制设备。设备与可控项以系统提示词中的清单为准；"
            "目标模糊（如用户只说「开灯」）也照常调用——把原话里的设备词或最接近的 "
            "entity_id 传入即可，系统会让用户挑选，不会误执行。"
        ),
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
            return tool_error("调度器未就绪", hint="调度服务尚未初始化完成，请如实告知用户稍后再试。")
        name = str(parameters.get("name", "")).strip()
        if not name:
            return tool_error("name 不能为空",
                              hint="用一句简短的话概括这个任务，如「起床开灯」「下班提醒」。")
        schedule = parameters.get("schedule") or {}
        payload = parameters.get("payload") or {}
        if not schedule or not payload:
            return tool_error(
                "schedule 和 payload 都是必填",
                hint="schedule 指定触发方式（kind=at/every/cron），payload 指定到点执行内容"
                     "（kind=tool/message/reminder），字段结构见工具描述。",
            )
        # 创建者 user_id：与 REST 路由（scheduler_routes）一致。缺失时 message 类
        # 任务到点执行会被拒（无归属会话）、reminder 会回退全局 agent 投递到
        # 全系统最近活跃会话（多用户下投错人）。session 由 tool_executor 传入，
        # 含登录用户 user_id。
        user_id = getattr(session, "user_id", "") or ""
        task = await svc.add_task({
            "name": name,
            "schedule": schedule,
            "payload": payload,
            "enabled": True,
            "user_id": user_id,
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
            '{"kind":"tool","tool_name":"ha_devices___call_service","tool_input":{"domain":"light","service":"turn_off","entity_id":"<从设备清单取 entity_id>"}}（调工具，如控制设备；entity_id 是占位符，必须取设备清单里的真实值，照抄示例会被拒绝）'
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
            return tool_error("调度器未就绪", hint="调度服务尚未初始化完成，请如实告知用户稍后再试。")
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
            return tool_error("调度器未就绪", hint="调度服务尚未初始化完成，请如实告知用户稍后再试。")
        task_id = str(parameters.get("task_id", "")).strip()
        if not task_id:
            return tool_error("task_id 不能为空", hint="先调 scheduled_task_list 获取任务 ID。")
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
        except Exception:  # noqa: BLE001
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

# ---------------------------------------------------------------------------
# 场景工具 — 一句话应用/创建/查询场景（SceneService，纯核心功能）
# ---------------------------------------------------------------------------

def _register_scene_tools(deps: ToolDeps) -> None:
    """注册场景模式聊天工具。

    scene_service 在 lifespan 装配进 container，handler 调用时动态读取。
    """

    def _svc():
        from .container import get_container
        c = get_container()
        return getattr(c, "scene_service", None)

    async def list_handler(parameters: dict, session) -> dict:
        svc = _svc()
        if svc is None:
            return tool_error("场景服务未就绪", hint="场景服务尚未初始化，请如实告知用户稍后再试。")
        scenes = await svc.list_scenes()
        return {"scenes": [
            {"id": s["id"], "name": s["name"],
             "actions_count": len(s.get("actions", []))}
            for s in scenes
        ]}

    async def apply_handler(parameters: dict, session) -> dict:
        svc = _svc()
        if svc is None:
            return tool_error("场景服务未就绪", hint="场景服务尚未初始化，请如实告知用户稍后再试。")
        name = str(parameters.get("name", "")).strip()
        scene_id = str(parameters.get("scene_id", "")).strip()
        if not scene_id and name:
            scenes = await svc.list_scenes()
            match = next((s for s in scenes if s["name"] == name), None)
            if match is None:
                return tool_error(
                    f"没有叫「{name}」的场景",
                    hint="从候选里选一个最接近的场景应用，或如实告知用户该场景不存在。",
                    candidates=[s["name"] for s in scenes],
                )
            scene_id = match["id"]
        if not scene_id:
            return tool_error("name 或 scene_id 必填一个", hint="不确定有哪些场景时先调 scene_list。")
        try:
            result = await svc.apply_scene(scene_id)
        except ValueError as e:
            return tool_error(str(e), hint="先调 scene_list 确认场景是否存在。")
        ok, total = result.get("ok", 0), result.get("total", 0)
        if ok == total:
            return {"success": True, "summary": f"场景「{result.get('scene')}」已应用（{ok}/{total} 个设备成功）"}
        failed = [r.get("entity_id", "") for r in result.get("results", []) if not r.get("ok")]
        return {"success": ok > 0,
                "summary": f"场景「{result.get('scene')}」部分应用（{ok}/{total} 成功），失败: {', '.join(failed)}"}

    async def create_handler(parameters: dict, session) -> dict:
        svc = _svc()
        if svc is None:
            return tool_error("场景服务未就绪", hint="场景服务尚未初始化，请如实告知用户稍后再试。")
        name = str(parameters.get("name", "")).strip()
        if not name:
            return tool_error("name 不能为空",
                              hint="给场景起个名字，如「观影模式」「睡眠模式」。")
        user_id = getattr(session, "user_id", "") or ""
        try:
            if parameters.get("capture"):
                scene = await svc.capture_scene(name, user_id=user_id)
            else:
                actions = parameters.get("actions") or []
                scene = await svc.create_scene(name, actions, user_id=user_id)
        except (ValueError, RuntimeError) as e:
            return tool_error(str(e), hint="capture 与 actions 二选一：capture=true 拍当前状态，"
                                           "或传 [{domain,service,entity_id,data}] 动作列表。")
        return {"success": True, "scene_id": scene["id"], "name": name,
                "actions_count": len(scene.get("actions", []))}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="scene_list",
        description="【场景列表】列出所有已保存的场景（如'回家模式''观影模式'）。",
        parameters={"type": "object", "properties": {}},
        handler=list_handler,
    ))
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="scene_apply",
        description=(
            "【应用场景】把一组设备切换到预设状态。用户说'回家模式''看电影模式''该睡觉了，切睡眠模式'"
            "等场景名时用本工具。不确定有哪些场景先调 scene_list。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "场景名（如'观影模式'）"},
                "scene_id": {"type": "string", "description": "场景 ID（与 name 二选一）"},
            },
        },
        handler=apply_handler,
    ))
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="scene_create",
        description=(
            "【创建场景】保存一组设备状态为场景。两种方式："
            "① capture=true：把设备当前状态拍下来存成场景（用户说'把现在的灯光存成观影模式'时用）；"
            "② 传 actions 列表（[{domain,service,entity_id,data}]，格式同 call_service 参数）。"
            "用户只是要控制设备时不要用本工具，直接调 call_service。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "场景名（如'观影模式'）"},
                "capture": {"type": "boolean", "description": "true=捕获当前所有设备状态"},
                "actions": {
                    "type": "array",
                    "description": "动作列表（capture=false 时必填）",
                    "items": {"type": "object"},
                },
            },
            "required": ["name"],
        },
        handler=create_handler,
    ))


# ---------------------------------------------------------------------------
# 自动化规则工具 — 对话建规则（两段式确认）/ 查 / 删 / 手动触发
#
# 草稿的存取/TTL/落库口径都在 services.pending_rules（网页确认弹窗的 REST 端点
# 与这里的工具 handler 共用），本文件只负责工具层的入参校验与 hint 措辞。
# ---------------------------------------------------------------------------


def _register_automation_rule_tools(deps: ToolDeps) -> None:
    """注册自动化规则聊天工具。

    建规则走两段式：automation_rule_create 只解析不落库（返回 pending_confirm
    JSON 给用户评估），用户确认后才写 rule_registry——网页端由确认弹窗直接调
    REST 落库，语音/飞书等渠道由用户口头确认后调 automation_rule_confirm。
    规则落库后会被周期评估并真实执行设备动作，误建是持续性风险，与定时任务
    （一次性、payload 明确、直接创建）误建成本不对称，故多一道确认。
    """

    async def create_handler(parameters: dict, session) -> dict:
        svc = deps.rule_service
        if svc is None:
            return tool_error("规则服务未就绪", hint="规则服务尚未初始化，请如实告知用户稍后再试。")
        text = str(parameters.get("text", "")).strip()
        if not text:
            return tool_error("text 不能为空",
                              hint="传用户描述规则的原话，如「温度高于30度就开空调」。")
        # 硬门控：只有用户本轮原话明确出现「创建规则」类字样才允许创建。
        # glm-4-flash 对「如果…就…」条件式话术经常误触本工具（甚至零工具调用
        # 幻觉"已创建"），提示词引导不住，所以按 session.current_query 判定；
        # 命中时的"强制考虑"软推在 prompt_service（两层共用 wants_rule_creation）。
        current_query = str(getattr(session, "current_query", "") or "")
        if not wants_rule_creation(current_query):
            return tool_error(
                "这条消息没有明确要求创建规则，已拒绝创建",
                hint="只有用户消息里出现『创建规则』『新建一条规则』这类字样才创建。"
                     "普通条件式描述（如『如果有人就开灯』）请按普通指令执行设备动作即可，"
                     "不要重试本工具；用户确实想建规则时，请其用『创建规则：…』的说法。",
            )
        camera_id = str(parameters.get("camera_id", "") or "").strip()
        user_id = getattr(session, "user_id", "") or ""
        try:
            rule = await svc.build_rule(text, user_id=user_id, camera_id=camera_id)
        except Exception as exc:
            logger.exception("automation_rule_create 解析失败")
            return tool_error(str(exc), hint="规则解析失败，请如实告知用户，或请其换种说法重试。")
        if not rule.get("actions"):
            return tool_error(
                "解析出的规则没有可执行动作",
                hint="不要凭空创建；请用户说清要对哪台设备做什么，再重新创建。",
            )
        # 零匹配拦截：自动修复也救不回来的动作（用户说的设备家里根本没有，
        # 如没有门类设备时说"开大门"）不出草稿——硬塞不相干设备比确认不了更危险。
        # 附主控设备候选让模型如实告知用户重新选择。
        if rule.pop("validation_errors", None):
            candidates: list[str] = []
            try:
                devices = await deps.ha_service.get_all_devices()
                candidates = [str(d.get("name") or d.get("entity_id", "")) for d in devices or []
                              if str(d.get("entity_id", "")).split(".")[0]
                              not in ("sensor", "binary_sensor")][:8]
            except Exception:  # noqa: BLE001 — 候选拉不到就只报错，不阻塞拒绝路径
                candidates = []
            return tool_error(
                "规则动作引用的设备不存在，且找不到可自动替换的近似设备",
                hint="不要凭空创建，也不要强行替换不相干的设备；把候选设备念给用户，"
                     "请其明确要对哪台设备做什么后重建。",
                candidates=candidates,
            )
        pending_id = uuid4().hex[:12]
        pending_store(session)[pending_id] = {
            "kind": KIND_AUTOMATION_RULE, "rule": rule, "created_at": time.time(),
        }
        missing_camera = needs_camera(rule)
        note = ("规则尚未创建。简要复述条件/动作要点，并明确告知用户「确认后才生效」。"
                "网页端会自动弹出确认框，无需用户再打字；语音等渠道请引导用户口头确认。"
                "用户说「确认」调 automation_rule_confirm；要改调 automation_rule_revise。"
                "不要说成已经创建好了。")
        if rule.get("auto_corrections"):
            # 透明化：用户说的设备不存在但找到了近似真实设备，已强制替换——
            # 复述必须点明替换，否则用户核对的只是系统的猜测，二次核对就失效了
            fixes = "、".join(
                f"「{c.get('from')}」→「{c.get('to_name')}」" for c in rule["auto_corrections"])
            note += (f"注意：用户说的设备不存在，系统已自动替换为最接近的真实设备：{fixes}。"
                     "复述时必须明确告知这一替换，提醒用户在弹窗中核对，不对可改。")
        if missing_camera:
            # 刻意不在这里给摄像头清单：网页端弹窗自带选择器，模型再念一遍名单
            # 只会和界面重复；语音端的问答由渠道插件自己管（见 integrations/）。
            note += ("这条规则还需要绑定一路摄像头才能生效（needs_camera=true）："
                     "有图形界面时用户会在选择器里选，你不必追问、也不要逐个念摄像头名字，"
                     "更不要替用户猜一路。未绑定前不要说规则已创建。")
        return {
            "status": "pending_confirm",
            "pending_id": pending_id,
            "rule": rule,
            "summary": str(rule.get("summary") or text),
            "expire_minutes": PENDING_TTL_SECONDS // 60,
            "needs_camera": missing_camera,
            "note": note,
        }

    async def revise_handler(parameters: dict, session) -> dict:
        svc = deps.rule_service
        if svc is None:
            return tool_error("规则服务未就绪", hint="规则服务尚未初始化，请如实告知用户稍后再试。")
        instruction = str(parameters.get("instruction", "")).strip()
        if not instruction:
            return tool_error("instruction 不能为空",
                              hint="传用户的修改要求原话，如「改成35度」。")
        pending_id, entry, err = locate_pending(
            session, str(parameters.get("pending_id", "")).strip(), KIND_AUTOMATION_RULE)
        if entry is None or pending_id is None:
            return tool_error(err,
                              hint="待确认规则 10 分钟有效；请用户重新描述需求，调 automation_rule_create 重建。")
        user_id = getattr(session, "user_id", "") or ""
        try:
            result = await svc.revise_rule(entry["rule"], instruction, user_id=user_id)
        except Exception as exc:
            logger.exception("automation_rule_revise 失败")
            return tool_error(str(exc), hint="修改失败，请如实告知用户。")
        new_rule = result.get("rule") or entry["rule"]
        entry["rule"] = new_rule
        entry["created_at"] = time.time()  # 改完重新计时，给用户完整评估窗口
        # camera_id 的清空/校验在 rule_service._resolve_revised_camera 里做（三个
        # 调用方共用一处），这里只把结果如实报给模型
        still_needs_camera = needs_camera(new_rule)
        note = "仍是待确认状态（未落库）；把改动复述给用户并继续等待确认。"
        if still_needs_camera:
            note += ("改完仍缺摄像头绑定：有图形界面时用户会在选择器里选，你不必追问、"
                     "也不要念摄像头名单或替用户猜一路。")
        return {"status": "pending_confirm", "pending_id": pending_id,
                "rule": new_rule, "change_summary": str(result.get("summary", "")),
                "expire_minutes": PENDING_TTL_SECONDS // 60,
                "needs_camera": still_needs_camera,
                "note": note}

    async def confirm_handler(parameters: dict, session) -> dict:
        registry = deps.rule_registry_service
        if registry is None:
            return tool_error("规则注册表未就绪", hint="请如实告知用户稍后再试。")
        user_id = getattr(session, "user_id", "") or ""
        result = await confirm_pending(
            session,
            str(parameters.get("pending_id", "")).strip(),
            registry,
            deps.ha_service,
            deps.ha_client_ref,
            user_id=user_id,
            known_cameras=_enabled_cameras(deps),
        )
        if not result.get("ok"):
            hints = {
                "camera_required": "不要替用户挑一路，也不要说规则已创建。把 error 里列出的"
                                   "可选摄像头报给用户，请其指定看哪一路；用户在网页端则由"
                                   "选择器完成。指定后由渠道侧写入绑定，再重新确认。",
                "missing_entities": "不要强行创建；调 automation_rule_revise 换设备，或如实告知用户。",
                "save_failed": "草稿仍在，不要重复创建；如实告知用户保存失败，可稍后再确认一次。",
            }
            return tool_error(str(result.get("error", "确认失败")),
                              hint=hints.get(str(result.get("reason")),
                                             "待确认规则 10 分钟有效；请用户重新描述需求，"
                                             "调 automation_rule_create 重建。"))
        return {"success": True, "rule_id": result.get("rule_id"),
                "name": result.get("name", ""), "summary": str(result.get("summary", "")),
                "note": "规则已创建并启用，自动化评估会周期执行（受冷却约束）。"}

    async def trigger_handler(parameters: dict, session) -> dict:
        automation = deps.automation_service
        registry = deps.rule_registry_service
        if automation is None or registry is None:
            return tool_error("自动化服务未就绪", hint="请如实告知用户稍后再试。")
        rule_id = str(parameters.get("rule_id", "")).strip()
        name = str(parameters.get("name", "")).strip()
        if not rule_id and not name:
            return tool_error("rule_id 或 name 必填一个",
                              hint="不确定有哪些规则时先调 automation_rule_list。")
        if not rule_id:
            rules = registry.list_rules()
            match = next((r for r in rules if r.get("name") == name), None)
            if match is None:
                return tool_error(f"没有叫「{name}」的规则",
                                  hint="从候选里选最接近的一条，或如实告知规则不存在。",
                                  candidates=[r.get("name", "") for r in rules][:8])
            rule_id = match["id"]
        try:
            result = await automation.trigger_rule(rule_id)
        except ValueError as exc:
            return tool_error(str(exc), hint="先调 automation_rule_list 确认规则存在。")
        executed = result.get("results", []) or []
        ret = {"success": True, "rule": result.get("rule"), "executed": len(executed)}
        if not executed:
            ret["note"] = "没有任何动作执行成功（可能全部失败或规则无可执行动作），请如实告知用户。"
        return ret

    async def list_handler(parameters: dict, session) -> dict:
        registry = deps.rule_registry_service
        if registry is None:
            return tool_error("规则注册表未就绪", hint="请如实告知用户稍后再试。")
        rules = registry.list_rules()
        # 设备友好名映射：此前列表只有 actions_count，用户问"这条规则控制的
        # 设备/id 是哪个"时模型没有任何依据可答。附上 entity_id + 友好名 +
        # 动作描述，模型才能如实回答（映射拉不到时降级为只有 entity_id）。
        name_map: dict = {}
        try:
            if deps.ha_service is not None:
                name_map = await deps.ha_service.get_entity_name_map()
        except Exception:  # noqa: BLE001
            name_map = {}

        def _brief_actions(rule: dict) -> list[dict]:
            actions = rule.get("actions") or []
            descriptions = rule.get("action_descriptions") or []
            briefs = []
            for i, action in enumerate(actions):
                tool_input = action.get("mcp_tool_input") or {}
                entity_id = str(tool_input.get("entity_id", "") or "")
                briefs.append({
                    "entity_id": entity_id,
                    "device_name": name_map.get(entity_id, ""),
                    "description": str(descriptions[i]) if i < len(descriptions) else "",
                })
            return briefs

        return {"rules": [
            {"id": r.get("id"), "name": r.get("name", ""), "type": r.get("type", ""),
             "condition": r.get("condition", ""), "enabled": bool(r.get("enabled", True)),
             "actions_count": len(r.get("actions") or []),
             "actions": _brief_actions(r)}
            for r in rules
        ], "count": len(rules)}

    async def delete_handler(parameters: dict, session) -> dict:
        registry = deps.rule_registry_service
        if registry is None:
            return tool_error("规则注册表未就绪", hint="请如实告知用户稍后再试。")
        rule_id = str(parameters.get("rule_id", "")).strip()
        if not rule_id:
            return tool_error("rule_id 不能为空", hint="先调 automation_rule_list 获取规则 ID。")
        try:
            registry.delete_rule(rule_id)
        except Exception as exc:  # noqa: BLE001 — AppException（404）统一转工具错误
            return tool_error(str(exc), hint="先调 automation_rule_list 确认规则存在。")
        return {"success": True, "rule_id": rule_id}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="automation_rule_create",
        description=(
            "【创建自动化规则-第一步】只在用户消息明确出现「创建规则」「新建一条规则」"
            "等字样时调用（如'创建一条规则：温度高于30度就开空调'）。普通条件式描述"
            "（如'温度高于30度就开空调''有人经过就开灯'，没有创建字样）不要调本工具，"
            "按普通指令执行即可——未命中关键词时本工具会直接拒绝。"
            "本工具只解析不创建：返回 pending_confirm JSON（含条件/动作/设备），"
            "必须先向用户复述要点并说清「确认后才生效」——此时规则还没建好，"
            "不要说成已经创建。确认由用户完成（网页端自动弹确认框，语音渠道口头说"
            "\"确认\"），确认落到系统后规则才生效；用户要改则调 automation_rule_revise。"
            "与定时任务的区别：定时用 scheduled_task_create，条件式自动化才用本工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户描述规则的原话"},
                "camera_id": {"type": "string", "description": "可选，绑定摄像头的规则传摄像头 ID"},
            },
            "required": ["text"],
        },
        handler=create_handler,
    ))
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="automation_rule_revise",
        description=(
            "【修改待确认规则】用户对刚解析出的规则说\"不对，改成…\"时调用，"
            "返回修改后的待确认 JSON，仍需用户确认后才落库。"
        ),
        parameters={
            "type": "object",
            "properties": {
                # 非必填：会话历史不保留工具返回值，跨轮后模型可能已丢失该 ID；
                # 缺了就由 handler 回退到会话内唯一的待确认草稿。
                "pending_id": {"type": "string", "description": "create 返回的待确认 ID（记得就传）"},
                "instruction": {"type": "string", "description": "用户的修改要求"},
            },
            "required": ["instruction"],
        },
        handler=revise_handler,
    ))
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="automation_rule_confirm",
        description=(
            "【确认创建自动化规则-第二步】用户明确确认（说\"确认/可以/就这么建\"）后调用，"
            "把待确认规则写入系统开始生效。只报结果要点，不重复规则全文。"
            "若返回\"不存在或已过期\"，可能是用户已在网页确认框里点过了，"
            "此时如实说明即可，不要重复创建。"
        ),
        parameters={
            "type": "object",
            "properties": {
                # 非必填：同 revise——跨轮丢失 ID 时回退到会话内唯一草稿。
                "pending_id": {"type": "string", "description": "create 返回的待确认 ID（记得就传）"},
            },
            "required": [],
        },
        handler=confirm_handler,
    ))
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="automation_rule_trigger",
        description=(
            "【立即触发自动化规则】用户说\"执行一下xx规则/现在就跑xx自动化\"时调用。"
            "跳过条件判断直接执行规则动作（用户说触发就是要执行）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "规则 ID（与 name 二选一）"},
                "name": {"type": "string", "description": "规则名（与 rule_id 二选一）"},
            },
        },
        handler=trigger_handler,
    ))
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="automation_rule_list",
        description=(
            "【自动化规则列表】用户查询「自动化规则/建过的规则/规则控制什么」时必须调用本工具。"
            "每条规则的 actions 里有控制的设备 entity_id、设备名和动作描述——"
            "用户问「这条规则控制哪个设备/id 是什么」时从这里如实回答。"
            "注意：不要用 http_request 直连 HA 的 /api/automations（会被内网防护拦截，"
            "且 HA 自动化与本工具的规则是两套系统）；定时任务也不是自动化规则，"
            "别用定时任务工具代替本工具回答自动化规则问题。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=list_handler,
    ))
    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="local",
        tool_name="automation_rule_delete",
        description="【删除自动化规则】用户要求删除某条自动化规则时调用。",
        parameters={
            "type": "object",
            "properties": {"rule_id": {"type": "string", "description": "规则 ID"}},
            "required": ["rule_id"],
        },
        handler=delete_handler,
    ))
    
