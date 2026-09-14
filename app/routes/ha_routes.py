"""Home Assistant 路由 — 设备控制、配置、测试。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query

from ..container import AppContainer, get_container
from ..clients.ha_client import HomeAssistantClient
from ..core.api_models import ApiResponse
from ..core.auth import get_current_admin, get_current_user, require_owned_session
from ..core.config import get_config, update_config_section
from ..core.exceptions import AppException
from ..schema.api_schemas import HAConfigRequest, HAServiceCallRequest, ModelTestRequest, UniqueSettingsRequest, EntityAliasRequest, EntityNoteRequest, EntityOperableRequest, ActionMapRequest, PendingConfirmRequest, PendingSelectRequest
from ..services.ha_service import HAService
from ..services.control_probe import call_with_probe
from ..services.pending_selections import cancel_selection, confirm_selection

logger = logging.getLogger(__name__)

router = APIRouter()

# 后台目录刷新任务引用（防 GC 中途回收，官方文档明确的弱引用坑）
_bg_refresh_tasks: set[asyncio.Task] = set()


def _spawn_catalog_refresh(refresh_fn) -> None:
    """后台刷新 HA 目录缓存（持强引用；异常只记日志）。"""
    try:
        t = asyncio.create_task(refresh_fn())
        _bg_refresh_tasks.add(t)
        t.add_done_callback(_bg_refresh_tasks.discard)
    except Exception:  # noqa: BLE001
        logger.warning("catalog refresh spawn failed", exc_info=True)


@router.get("/ha/entities")
async def ha_entities(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    try:
        from ..services.entity_controls import resolve_controls as _rc
        entities = await container.ha_service.get_all_devices()
        grouped = await container.ha_service.get_all_devices_grouped()
        # 用扁平实体的 domain 集合算 service 定义（覆盖所有实体）
        all_domains = {d.get("domain", "") for d in entities}
        for dev in grouped.get("devices", []):
            for ent in dev.get("entities", []):
                all_domains.add(ent.get("domain", ""))
        raw_svc_defs = await container.ha_service.get_service_defs(
            container.ha_client, domains=all_domains
        )
        # 扁平实体：保留原有 _controls（下游 tools/text_match/automation 消费）
        controls_by_eid: dict[str, dict] = {}
        for d in entities:
            ctrl = _rc(d, raw_svc_defs)
            d["_controls"] = ctrl
            controls_by_eid[d["entity_id"]] = ctrl
        # 设备分组里的子实体：复用同一份 controls（按 entity_id 查）
        for dev in grouped.get("devices", []):
            for ent in dev.get("entities", []):
                ent["_controls"] = controls_by_eid.get(ent["entity_id"], _rc(ent, raw_svc_defs))
        return ApiResponse(data={
            "entities": entities,
            "devices": grouped.get("devices", []),
            "count": len(entities),
        })
    except Exception as e:
        logger.exception("HA entities failed")
        raise AppException(f"Home Assistant 连接失败: {e}", code="ha_error", http_status=502)


@router.get("/ha/entity-aliases")
async def get_entity_aliases() -> ApiResponse[dict]:
    """获取全部实体别名映射 {entity_id: alias}。"""
    from ..core.database import Database
    db = Database.get()
    aliases = await db.prefs_get_by_scope("entity_alias")
    return ApiResponse(data={"aliases": aliases})


@router.put("/ha/entity-aliases")
async def set_entity_alias(
    payload: EntityAliasRequest, container: AppContainer = Depends(get_container)
) -> ApiResponse[dict]:
    """设置/更新一个实体别名。空串 alias 表示删除别名恢复默认名。

    别名同时写两处：
    - Aether DB（entity_alias scope）：本系统展示用
    - HA entity_registry.name：同步到 HA 原生（HA 网页/自动化/其他集成都能看到）
    HA 写入失败时回滚 Aether 侧，保证两边一致。
    """
    from ..core.database import Database
    entity_id = payload.entity_id
    alias = payload.alias
    if not entity_id:
        raise AppException("缺少 entity_id", code="missing_params", http_status=400)

    db = Database.get()
    # 先写 HA（失败则不写 Aether，保持一致）
    ha_name = alias or None  # 空串 → 清除 HA 自定义名，恢复默认
    try:
        await container.ha_client.update_entity_name(entity_id, ha_name)
    except Exception as e:
        logger.warning("同步别名到 HA 失败: %s", e)
        raise AppException(
            f"同步到 Home Assistant 失败: {e}", code="ha_sync_failed", http_status=502
        )
    # HA 成功后再写 Aether DB
    if alias:
        await db.emoji_pref_upsert("entity_alias", entity_id, alias)
    else:
        await db.emoji_pref_delete("entity_alias", entity_id)
    # 清缓存让前端重拉时应用新名
    container.ha_service.invalidate_states_cache()
    return ApiResponse(data={"entity_id": entity_id, "alias": alias})


@router.get("/ha/entity-notes")
async def get_entity_notes() -> ApiResponse[dict]:
    """获取全部实体备注映射 {entity_id: note}（用户自定义，注入 LLM 认知）。"""
    from ..core.database import Database
    db = Database.get()
    notes = await db.prefs_get_by_scope("entity_note")
    return ApiResponse(data={"notes": notes})


@router.put("/ha/entity-notes")
async def set_entity_note(
    payload: EntityNoteRequest, container: AppContainer = Depends(get_container)
) -> ApiResponse[dict]:
    """设置/更新一个实体备注。空串 note 表示删除备注。

    备注只写 Aether DB（不同步 HA——HA 无此概念），用于注入 LLM 认知：
    让 AI 看到设备怪癖（如继电器 ON=关门），据此正确调用 service。
    写入后清 HA 状态缓存，让后台 _refresh_ha_catalog 下个周期重读备注。
    """
    from ..core.database import Database
    entity_id = payload.entity_id
    note = payload.note
    if not entity_id:
        raise AppException("缺少 entity_id", code="missing_params", http_status=400)

    db = Database.get()
    if note:
        await db.emoji_pref_upsert("entity_note", entity_id, note)
    else:
        await db.emoji_pref_delete("entity_note", entity_id)
    # 立即重建 catalog 缓存（含备注），不必等后台 60 秒循环。
    # 否则用户写完备注立刻聊天，LLM 用的还是旧缓存（不含备注）→ 调错 service。
    refresh_fn = getattr(container, "catalog_refresh_fn", None)
    if refresh_fn is not None:
        _spawn_catalog_refresh(refresh_fn)
    return ApiResponse(data={"entity_id": entity_id, "note": note})


@router.get("/ha/entity-operable")
async def get_entity_operable() -> ApiResponse[dict]:
    """获取被用户禁止 AI 操作的实体集合（黑名单，{entity_id: "0"}）。"""
    from ..core.database import Database
    db = Database.get()
    disabled = await db.prefs_get_by_scope("entity_operable")
    return ApiResponse(data={"disabled": disabled})


@router.put("/ha/entity-operable")
async def set_entity_operable(
    payload: EntityOperableRequest, container: AppContainer = Depends(get_container)
) -> ApiResponse[dict]:
    """设置/取消实体的「AI 可操作」权限。完全可逆。

    operable=False 写入黑名单（禁止），True 删除记录（恢复可操作）。
    写入后立即刷新 catalog，让 system prompt 不等 60 秒就反映新权限。
    """
    from ..core.database import Database
    entity_id = payload.entity_id
    if not entity_id:
        raise AppException("缺少 entity_id", code="missing_params", http_status=400)

    db = Database.get()
    if payload.operable:
        # 恢复可操作：删除黑名单记录
        await db.emoji_pref_delete("entity_operable", entity_id)
    else:
        # 禁止 AI 操作：写入黑名单
        await db.emoji_pref_upsert("entity_operable", entity_id, "0")
    # 立即刷新 catalog（与 set_entity_note 同做法）
    refresh_fn = getattr(container, "catalog_refresh_fn", None)
    if refresh_fn is not None:
        _spawn_catalog_refresh(refresh_fn)
    return ApiResponse(data={"entity_id": entity_id, "operable": payload.operable})


@router.get("/ha/action-maps")
async def get_action_maps() -> ApiResponse[dict]:
    """获取全部已配置的动作语义映射 {entity_id: {mappings: {...}}}。"""
    import json
    from ..core.database import Database
    db = Database.get()
    raw = await db.prefs_get_by_scope("entity_action_map")
    maps: dict[str, dict] = {}
    for eid, val in raw.items():
        try:
            obj = json.loads(val) if isinstance(val, str) else val
            if isinstance(obj, dict) and obj.get("mappings"):
                maps[eid] = obj
        except (ValueError, TypeError):
            logger.warning("action-maps 解析失败 entity=%s", eid, exc_info=True)
    return ApiResponse(data={"maps": maps})


@router.put("/ha/action-maps")
async def set_action_map(
    payload: ActionMapRequest, container: AppContainer = Depends(get_container)
) -> ApiResponse[dict]:
    """设置/更新一个实体的动作映射。空 mappings = 删除。

    校验：每个 target 必须属于该域 services 且 ≠ 源 service。
    写入后清缓存并触发 catalog 刷新。
    """
    import json
    from ..core.database import Database
    from ..services.semantic_map import invalidate_cache

    entity_id = payload.entity_id
    if not entity_id:
        raise AppException("缺少 entity_id", code="missing_params", http_status=400)

    db = Database.get()
    if not payload.mappings:
        await db.emoji_pref_delete("entity_action_map", entity_id)
    else:
        # 校验 target 合法性
        domain = entity_id.split(".")[0]
        svc_defs = await container.ha_service.get_service_defs(container.ha_client, domains={domain})
        domain_svcs = svc_defs.get(domain) or {}
        valid_svcs = set(domain_svcs.keys()) if isinstance(domain_svcs, dict) else set(domain_svcs)
        if not valid_svcs:
            raise AppException(
                f"域 {domain} 的服务列表获取失败，无法校验映射",
                code="ha_error", http_status=502,
            )
        cleaned: dict[str, dict] = {}
        for svc, entry in payload.mappings.items():
            if not isinstance(entry, dict):
                continue
            target = entry.get("target", "")
            if not target or target == svc:
                continue
            if target not in valid_svcs:
                raise AppException(
                    f"service '{target}' 不属于域 {domain}（可用: {sorted(valid_svcs)}）",
                    code="invalid_target", http_status=400,
                )
            cleaned[svc] = {"target": target, "description": entry.get("description", "")}
        if not cleaned:
            await db.emoji_pref_delete("entity_action_map", entity_id)
        else:
            await db.emoji_pref_upsert(
                "entity_action_map", entity_id, json.dumps({"mappings": cleaned})
            )
    invalidate_cache()
    refresh_fn = getattr(container, "catalog_refresh_fn", None)
    if refresh_fn is not None:
        _spawn_catalog_refresh(refresh_fn)
    return ApiResponse(data={"entity_id": entity_id, "mappings": payload.mappings})


@router.get("/ha/entity-services")
async def get_entity_services(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """返回按域分组的可用服务列表（供前端拉取可配置的 action）。

    复用 ha_service.get_service_defs，剥离成 {domain: [svc_name, ...]}。
    """
    try:
        svc_defs = await container.ha_service.get_service_defs(container.ha_client)
        services = {domain: list(svcs.keys()) for domain, svcs in svc_defs.items()}
        return ApiResponse(data={"services": services})
    except Exception as e:
        logger.exception("entity-services failed")
        raise AppException(f"服务列表获取失败: {e}", code="ha_error", http_status=502)


@router.get("/ha/services")
async def ha_services(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """返回 HA 服务定义，格式: {domain: {service_name: {fields: [...], required: [...]}}}"""
    try:
        services_info = await container.ha_service.get_service_defs(
            container.ha_client, include_required=True
        )
        return ApiResponse(data=services_info)
    except Exception as e:
        logger.exception("HA services failed")
        raise AppException(f"Home Assistant 服务列表获取失败: {e}", code="ha_error", http_status=502)


@router.get("/ha/history")
async def ha_history(
    filter_entity_id: str = Query(..., description="实体 ID（逗号分隔多个）"),
    hours: float = Query(default=24, ge=0.1, le=24 * 30, description="查询近 N 小时历史"),
    minimal: bool = Query(default=True, description="仅返回最少字段以加速传输"),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """查询实体历史状态记录（用于传感器趋势图）。

    后端按 hours 计算起止时间（ISO8601），直接透传 HA /api/history/period。
    """
    from datetime import datetime, timedelta, timezone

    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        timestamp = start.isoformat()
        end_time = now.isoformat()
        history = await container.ha_client.get_history(
            filter_entity_id=filter_entity_id,
            timestamp=timestamp,
            end_time=end_time,
            minimal=minimal,
        )
        return ApiResponse(data={"history": history, "count": len(history)})
    except Exception as e:
        logger.exception("HA history failed")
        raise AppException(f"Home Assistant 历史查询失败: {e}", code="ha_error", http_status=502)


@router.post("/ha/call_service")
async def ha_call_service(payload: HAServiceCallRequest, container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    domain = payload.domain
    service = payload.service
    entity_id = payload.entity_id
    data = payload.data
    if entity_id and "." not in str(entity_id):
        entity_id = f"{domain}.{entity_id}"
    try:
        result = await call_with_probe(container.ha_client, domain, service, entity_id, data)
        # 调用服务后立即清掉 HAService 的状态缓存，确保前端重拉拿到最新状态
        container.ha_service.invalidate_states_cache()
        # 主控操作落 device_op 事件（手动/设备页发起，与 AI 操作区分统计）
        try:
            from ..services.device_event_service import record_device_op
            name_of = {}
            try:
                states = await container.ha_service.get_states_snapshot()
                name_of = {
                    s.get("entity_id"): str((s.get("attributes") or {}).get("friendly_name") or "")
                    for s in states if s.get("entity_id")
                }
            except Exception:  # noqa: BLE001
                pass
            eids = [e.strip() for e in str(entity_id).split(",") if e.strip()] if entity_id else []
            await record_device_op(eids, service, "手动", name_of)
        except Exception:  # noqa: BLE001
            logger.debug("record device_op failed", exc_info=True)
        return ApiResponse(data={"success": True, "result": result})
    except Exception as e:
        logger.exception("HA call_service failed")
        raise AppException(str(e), code="ha_error", http_status=502)


# ---------------------------------------------------------------------------
# 设备消歧待选确认 —— 网页弹框勾选后直接执行，不再回模型
#
# 与聊天工具 call_service 的消歧闸门共用 services.pending_selections：草稿挂在
# SessionState.pending_confirmations（内存、10 分钟 TTL、不持久化）。语音/飞书等
# 无界面渠道不走这里 —— 模型口头列举候选，用户下一轮直接说设备名即可。
#
# 顺序约束（同 /rules/pending/*）：前端必须等本轮 Dialog.Finish 到达后再弹框。
# dispatcher 在轮末才把 user/assistant 消息 append 进 model_messages
# （agents/dispatcher.py），确认若在轮中落进来，下面那条合成消息会排在原始请求
# 之前，下一轮模型读到的是乱序历史。
# ---------------------------------------------------------------------------

_SELECTION_GONE = "待选设备不存在或已过期，请重新说一遍指令"


async def _execute_selection(container: AppContainer, draft: dict, entity_id: str) -> Any:
    """执行用户在消歧弹框里勾选的指令。

    与 call_service 工具同源：entity_operable 黑名单校验 + call_with_probe +
    device_op 审计（actor="AI"）。

    **不复用 POST /api/ha/call_service**，但理由不是它没鉴权 —— 它由全局
    `api_token_guard` 中间件守着（app/main.py），和所有 /api/* 一样要 JWT/APP_TOKEN。
    真正的区别是语义：那个端点是**人在设备页手动操作**的入口，所以它刻意不查
    entity_operable（该黑名单的含义是「禁止 **AI** 操作」，人点按钮本来就该能操作），
    审计也记 actor="手动"。而这里执行的是 **AI 会话里发起的指令**，只是最后一步
    由用户点了勾选，必须继续受 AI 侧约束；复用手动路径就等于给「禁止 AI 操作」
    开了一条绕过弹框的旁路。
    """
    service = str(draft.get("service") or "")
    data = draft.get("data") or {}
    eids = [e.strip() for e in entity_id.split(",") if e.strip()]
    # 会话中途被禁的设备，不能因为弹框里还留着就执行
    from ..core.database import Database
    disabled = await Database.get().prefs_get_by_scope("entity_operable")
    blocked = [e for e in eids if e in disabled]
    if blocked:
        raise AppException(
            f"设备「{'、'.join(blocked)}」被用户设为禁止 AI 操作，调用被拒绝。",
            code="entity_not_operable", http_status=403)
    # 按实体各自的域分组下发，不能信草稿的 domain：那是模型对**歧义原话**猜的域
    # （「打开灯」→ light），而候选集里可能混着别的域的实体（墙壁开关键是 switch.*）。
    # 按草稿域调跨域实体会被 HA 静默忽略——HTTP 200 + HA 内部 warning，设备毫无动作
    # （2026-09-13「会客厅灯左键」事故）。entity_id 前缀永远是它真实的域。
    groups: dict[str, list[str]] = {}
    for eid in eids:
        groups.setdefault(eid.split(".", 1)[0], []).append(eid)
    results = [await call_with_probe(container.ha_client, dom, service, ",".join(ids), data)
               for dom, ids in groups.items()]
    result = results[0] if len(results) == 1 else results
    # 调用后立即清状态缓存，确保前端重拉拿到最新状态
    container.ha_service.invalidate_states_cache()
    try:
        from ..services.device_event_service import record_device_op
        name_of = {str(c.get("entity_id", "")): str(c.get("label", ""))
                   for c in (draft.get("candidates") or [])}
        await record_device_op(eids, service, "AI", name_of)
    except Exception:  # noqa: BLE001 — 审计失败不影响执行结果
        logger.debug("record device_op failed", exc_info=True)
    return result


@router.post("/ha/pending/{pending_id}/select")
async def select_pending_devices(
    pending_id: str,
    payload: PendingSelectRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """提交弹框勾选 → 校验在候选内 → 执行 → 摘草稿 → 补一条合成消息进会话历史。"""
    session = await require_owned_session(container, payload.session_id, current_user)
    result = await confirm_selection(
        session, pending_id, payload.entity_ids,
        lambda draft, eid: _execute_selection(container, draft, eid),
    )
    if not result.get("ok"):
        # 选择越界是用户可修正的输入问题（400），执行失败是下游问题（502），
        # 草稿没了才是 404
        status = {"invalid_selection": 400, "exec_failed": 502}.get(
            str(result.get("reason")), 404)
        raise AppException(str(result.get("error") or _SELECTION_GONE),
                           code="pending_select_failed", http_status=status)
    names = [str(n) for n in (result.get("names") or [])]
    # 让下一轮 LLM 知道设备是用户在界面上挑的（会话历史不存 tool 消息，
    # 不补这一条模型会以为指令还没执行）
    session.model_messages.append(
        {"role": "user", "content": f"（我已通过界面选择：{'、'.join(names)}，指令已执行）"})
    await container.session_store.store_session(session)
    return ApiResponse(data={"entity_ids": result.get("entity_ids"), "names": names})


@router.post("/ha/pending/{pending_id}/cancel")
async def cancel_pending_selection(
    pending_id: str,
    payload: PendingConfirmRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """放弃待选草稿（不执行任何设备操作）。camera_id 字段忽略。"""
    session = await require_owned_session(container, payload.session_id, current_user)
    if not cancel_selection(session, pending_id):
        raise AppException(_SELECTION_GONE,
                           code="pending_selection_not_found", http_status=404)
    return ApiResponse(data={"cancelled": True})


@router.get("/ha/config")
async def get_ha_config() -> ApiResponse[dict]:
    ha_cfg = get_config("ha", {})
    token = ha_cfg.get("token", "")
    return ApiResponse(
        data={
            "url": ha_cfg.get("url", "http://localhost:8123"),
            "token_set": bool(token),
            "token_preview": (token[:4] + "****" + token[-4:]) if len(token) >= 8 else ("****" if token else ""),
        }
    )


def _classify_ha_error(e: Exception) -> dict:
    """把 HA 调用异常分类成前端可读的 reason。

    返回 {"reason": "unauthorized"|"unreachable"|"error", "detail": str}，
    供 /ha/test 和 /ha/config 保存前预校验共用。
    """
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code in (401, 403):
            return {"reason": "unauthorized", "detail": "Token 无效或已过期（URL 可达，请检查 Token）"}
        return {"reason": "error", "detail": f"HA 返回 HTTP {e.response.status_code}"}
    if isinstance(e, (httpx.ConnectError, httpx.TimeoutException, httpx.UnsupportedProtocol)):
        return {"reason": "unreachable", "detail": f"HA 地址不可达：{e}"}
    return {"reason": "error", "detail": str(e)}


@router.post("/ha/config")
async def set_ha_config(
    payload: HAConfigRequest,
    current_user: dict = Depends(get_current_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """保存 HA 配置（改写全屋设备连接指向，仅管理员）。

    安全策略：用户传了新 token 时，先用 (url, token) 建临时 client 连一次 HA
    /api/，验证通过才写入 config.json。这样从根上杜绝「存进去的 token 连不上」
    ——用户在页面上误填旧 token / 错 token / 脏文本时，保存会被拒绝而不是把
    好配置覆盖成坏的。
    """
    url = payload.url.strip().rstrip("/")
    from ..core.net_guard import url_scheme_error, HTTP_SCHEMES
    scheme_err = url_scheme_error(url, HTTP_SCHEMES)
    if scheme_err:
        # 刻意返回 200 + data.saved=false（不抛异常）：probe 类失败前端要就地显示
        # 在对应输入框旁，而不是弹全局错误。AdvancedView 依赖 data.saved === false，
        # 别改成 raise —— 信号走 data.saved，不走 HTTP 状态码。
        return ApiResponse(message=scheme_err, data={"saved": False})
    new_token = str(payload.token).strip() if payload.token is not None else None

    # 只传了 url（没传 token）：用现有 token 验证 url 是否可达
    # 传了 token：必须验证新 token 真的能连上 HA 才允许保存
    verify_token = new_token if new_token else get_config("ha.token", "")
    if verify_token:
        probe = HomeAssistantClient(base_url=url, token=verify_token)
        try:
            await probe.get_states()
        except Exception as e:
            await probe.close()
            info = _classify_ha_error(e)
            logger.warning("HA config save rejected: %s (%s)", info["reason"], info["detail"])
            return ApiResponse(
                code="ha_error",
                message=info["detail"],
                data={"saved": False, **info},
            )
        finally:
            await probe.close()

    # 验证通过，写盘
    updates: dict = {"url": url}
    if new_token:
        updates["token"] = new_token
    new_cfg = update_config_section("ha", updates)
    old_client = container.ha_client_ref[0]
    new_client = HomeAssistantClient()
    container.ha_client_ref[0] = new_client
    container.ha_service = HAService(client=new_client)
    await old_client.close()
    # 同步 main/dispatcher/集成 host_deps 等处持有的旧引用（旧 client 已 close，
    # 不同步则目录刷新、设备名映射、插件反向调用持续失败直到重启）
    from ..main import sync_ha_runtime_refs
    sync_ha_runtime_refs(new_client, container.ha_service)
    token_val = new_cfg.get("token", "")
    return ApiResponse(
        data={
            "saved": True,
            "url": new_cfg.get("url", "http://localhost:8123"),
            "token_set": bool(token_val),
            "token_preview": (token_val[:4] + "****" + token_val[-4:]) if len(token_val) >= 8 else ("****" if token_val else ""),
        }
    )


@router.post("/ha/test")
async def test_ha_connection(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """测试 HA 连接。区分 Token 无效 (401) 和地址不可达 (连接/DNS/超时)，
    前端据此给出针对性提示，避免用户误判是 URL 问题还是 Token 问题。
    """
    try:
        states = await container.ha_client.get_states()
        count = len(states)
        return ApiResponse(data={"connected": True, "entity_count": count})
    except Exception as e:
        info = _classify_ha_error(e)
        if info["reason"] != "error":
            logger.warning("HA test failed: %s", info["detail"])
        else:
            logger.exception("HA test connection failed")
        return ApiResponse(
            code="ha_error",
            message=info["detail"],
            data={"connected": False, **info},
        )


@router.get("/unique")
async def get_unique_settings() -> ApiResponse[dict]:
    """获取聊天助手的个性化设置（角色设定、行为原则）。"""
    from ..services.prompt_service import DEFAULT_PERSONA, GUIDELINES
    persona = str(get_config("chat_assistant.persona", "") or "").strip()
    guidelines = str(get_config("chat_assistant.guidelines", "") or "").strip()
    return ApiResponse(
        data={
            "persona": persona or DEFAULT_PERSONA,
            "guidelines": guidelines or GUIDELINES,
            "persona_custom": bool(persona),
            "guidelines_custom": bool(guidelines),
        }
    )


@router.post("/unique")
async def set_unique_settings(payload: UniqueSettingsRequest) -> ApiResponse[dict]:
    """更新聊天助手的个性化设置。仅允许 persona，guidelines 由系统管理。"""
    from ..services.prompt_service import DEFAULT_PERSONA, GUIDELINES
    updates: dict = {}
    if payload.persona:
        updates["persona"] = payload.persona.strip()
    new_cfg = update_config_section("chat_assistant", updates)
    # guidelines 返回 config 实际生效值（与 GET /unique、prompt 同口径），
    # 而非硬编码默认值——此前保存 persona 后前端拿到的
    # guidelines/guidelines_custom 是假的。
    guidelines_cfg = str(new_cfg.get("guidelines", "") or "").strip()
    return ApiResponse(
        data={
            "persona": new_cfg.get("persona", "") or DEFAULT_PERSONA,
            "guidelines": guidelines_cfg or GUIDELINES,
            "persona_custom": bool(new_cfg.get("persona", "")),
            "guidelines_custom": bool(guidelines_cfg),
        }
    )
