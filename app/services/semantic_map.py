"""设备动作语义映射 — 代码级无条件翻转 service + state 隐含跟随。

核心思路：AI 凭直觉调用（稳定一致），call_service 执行前无条件替换 service，
结果反馈带描述让 AI 正确汇报。对称翻转对（turn_on↔turn_off）自动翻转 state。
映射规则不进提示词（防双重错误），真实解释放结果反馈里。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# 进程内缓存：{entity_id: {mappings: {...}}}。写入时 invalidate，下次读取重载。
_cache: dict[str, dict] = {}
_cache_loaded = False


async def _reload_cache() -> None:
    global _cache_loaded
    try:
        from ..core.database import Database
        raw = await Database.get().prefs_get_by_scope("entity_action_map")
        parsed: dict[str, dict] = {}
        for eid, val in raw.items():
            try:
                obj = json.loads(val) if isinstance(val, str) else val
                if isinstance(obj, dict) and obj.get("mappings"):
                    parsed[eid] = obj
            except (ValueError, TypeError):
                logger.warning("动作映射解析失败 entity=%s", eid, exc_info=True)
        _cache.clear()
        _cache.update(parsed)
        _cache_loaded = True
    except Exception:  # noqa: BLE001
        logger.warning("动作映射缓存加载失败", exc_info=True)


async def get_action_map(entity_id: str) -> dict | None:
    """读取某实体的动作映射。带进程内缓存。DB 异常返回 None 放行。"""
    global _cache_loaded
    if not _cache_loaded:
        await _reload_cache()
    return _cache.get(entity_id)


def invalidate_cache() -> None:
    """写入后调用，下次 get_action_map 时重新加载。"""
    global _cache_loaded
    _cache_loaded = False


def is_flipped_pair(mappings: dict) -> bool:
    """检测对称翻转对：turn_on→turn_off 且 turn_off→turn_on 同时存在。"""
    def target_of(svc: str) -> str | None:
        e = mappings.get(svc)
        return e.get("target") if isinstance(e, dict) else None
    return (target_of("turn_on") == "turn_off"
            and target_of("turn_off") == "turn_on")


def apply_state_flip(new_state: dict, entity_id: str) -> dict:
    """对称翻转对设备：把 new_state.state on↔off 反转。非翻转设备原样返回。

    同步版，用于 call_service 返回时（缓存已被同次调用的 get_action_map 预热）。
    """
    am = _cache.get(entity_id)
    if not am or not is_flipped_pair(am.get("mappings", {})):
        return new_state
    s = new_state.get("state")
    if s == "on":
        return {**new_state, "state": "off"}
    if s == "off":
        return {**new_state, "state": "on"}
    return new_state


async def flip_state_value(entity_id: str, state: str) -> str:
    """供状态读取点调用（get_entities / catalog）。翻转 on/off。"""
    am = await get_action_map(entity_id)
    if am and is_flipped_pair(am.get("mappings", {})):
        if state == "on":
            return "off"
        if state == "off":
            return "on"
    return state
