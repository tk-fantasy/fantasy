"""控件范围报错探测保底机制。

当 AI 或前端用错误范围（如把 0-1 的 volume_level 当 0-100）调用 HA 服务收到 400 时，
自动探测正确范围并缓存，然后用用户原本要的值按新范围归一化后重发。

设计：
- 规范表（entity_controls._DOMAIN_SPEC_RANGES）覆盖 HA 规范固定范围的属性（已命中 99%）。
- 本模块是保底：规范表未覆盖的未知属性首次 400 时，按候选刻度试探，命中后缓存。
- 候选刻度：[0-1, 0-100, 0-255]，按传中点值能否成功判断。
- 副作用：探测会短暂改变设备状态（如音箱试探音量），最多 2 次额外调用，命中后缓存。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 候选刻度：(min, max, 探测中点值)。
# 用户传值 50 失败 → 试中点 0.5（0-1 刻度）→ 成功则范围是 0-1。
# 按范围从小到大试探（0-1 最常见于归一化属性）。
_PROBE_SCALES: tuple[tuple[float, float, float], ...] = (
    (0.0, 1.0, 0.5),       # 归一化浮点（media_player.volume_level 等）
    (0.0, 100.0, 50.0),    # 百分比（多数 position/brightness_pct）
    (0.0, 255.0, 128.0),   # 8-bit 原始值（light brightness 原始）
)


class ControlProbeCache:
    """进程内缓存：记忆 (entity_id, param) 探测出的真实范围 (min, max)。

    重启失效（可接受 —— 规范表已覆盖已知属性，这里只兜底未知设备，重启后重新探测一次即可）。
    """

    def __init__(self) -> None:
        # {(entity_id, param): (min, max)}
        self._cache: dict[tuple[str, str], tuple[float, float]] = {}

    def get(self, entity_id: str, param: str) -> tuple[float, float] | None:
        return self._cache.get((entity_id, param))

    def set(self, entity_id: str, param: str, rng: tuple[float, float]) -> None:
        self._cache[(entity_id, param)] = rng

    def clear(self) -> None:
        self._cache.clear()


# 全局单例（进程级缓存，所有请求共享）
_probe_cache = ControlProbeCache()


def _is_out_of_range_error(exc: Exception) -> bool:
    """判断异常是否为「值越界/参数错误」类（值得探测范围）。

    HA 对 volume_level=50 这类越界值返回 400 Bad Request。
    500 通常是设备不支持该服务（非范围问题），不探测。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 400
    # httpx.HTTPStatusError 是 Exception 子类，但 call_service 可能被上层包装
    return False


async def call_with_probe(
    ha_client: Any,
    domain: str,
    service: str,
    entity_id: str | None,
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    """调用 HA 服务，失败时自动探测范围并重试。

    流程：
    1. 缓存命中 → 直接按缓存范围归一化用户值后调用
    2. 首次调用失败（400）→ 按候选刻度探测 → 缓存 → 用用户原值按新范围归一化重发
    3. 探测全失败 → 抛出原始异常

    仅处理「带单个数值参数」的调用（slider 类）。无参 action 或多参调用原样透传。
    """
    # 无 data 或非单数值参数 → 不适合探测，直接调用
    numeric_param = _single_numeric_param(data)
    if numeric_param is None:
        return await ha_client.call_service(domain, service, entity_id, data)

    param, user_value = numeric_param

    # 1. 缓存命中：按缓存范围归一化用户值
    cached = _probe_cache.get(entity_id or "", param)
    if cached:
        normalized = _normalize_to_range(user_value, cached)
        new_data = {**data, param: normalized}
        return await ha_client.call_service(domain, service, entity_id, new_data)

    # 2. 首次调用（用用户原始值）
    try:
        return await ha_client.call_service(domain, service, entity_id, data)
    except Exception as exc:
        if not _is_out_of_range_error(exc):
            raise  # 非 400，原样抛出
        # 3. 400 → 探测范围
        logger.info(
            "call_service 400，开始探测范围: domain=%s service=%s entity=%s param=%s value=%s",
            domain, service, entity_id, param, user_value,
        )
        probed = await _probe_range(ha_client, domain, service, entity_id, param)
        if probed is None:
            raise  # 探测全失败，抛原始 400
        _probe_cache.set(entity_id or "", param, probed)
        logger.info("探测成功: %s.%s 范围=%s，重发用户原值", entity_id, param, probed)
        # 用用户原值按新范围归一化后重发
        normalized = _normalize_to_range(user_value, probed)
        new_data = {**data, param: normalized}
        return await ha_client.call_service(domain, service, entity_id, new_data)


def _single_numeric_param(data: dict[str, Any] | None) -> tuple[str, float] | None:
    """从 data 中提取「单个数值参数」（slider 类调用的特征）。

    返回 (param_name, value)，或 None（无参/多参/非数值）。
    entity_id 由 call_service 单独传，不在 data 里。
    """
    if not data or len(data) != 1:
        return None
    for k, v in data.items():
        if isinstance(v, bool):
            return None  # bool 是 int 子类，但不是滑块值
        if isinstance(v, (int, float)):
            return k, float(v)
    return None


def _normalize_to_range(value: float, target_range: tuple[float, float]) -> float:
    """把用户值按 target_range 归一化。

    启发式：用户值若超出 target_range，按比例缩放（假设用户用的是 0-100 刻度）。
    例：用户传 50（以为是 0-100），实际范围 0-1 → 缩放为 0.5。
    """
    tmin, tmax = target_range
    # 值已在目标范围内 → 原样返回
    if tmin <= value <= tmax:
        return value
    # 值超出范围：假设用户用的是 0-100 百分比刻度，换算到目标范围
    if value > tmax:
        # 50 (0-100 刻度) → 0.5 (0-1 刻度): value/100 * (tmax-tmin) + tmin
        ratio = value / 100.0 if value > 1.0 else value
        return tmin + ratio * (tmax - tmin)
    return value


async def _probe_range(
    ha_client: Any,
    domain: str,
    service: str,
    entity_id: str | None,
    param: str,
) -> tuple[float, float] | None:
    """按候选刻度依次试探，返回首个成功的范围 (min, max)，全失败返回 None。

    探测用各刻度的中点值调用，成功（不抛 400）即判定该刻度为真实范围。
    """
    for tmin, tmax, midpoint in _PROBE_SCALES:
        try:
            await ha_client.call_service(
                domain, service, entity_id, {param: midpoint}
            )
            return (tmin, tmax)
        except httpx.HTTPStatusError as exc:
            # 400 继续试下一个刻度；500 等其他错误放弃探测
            if exc.response.status_code == 400:
                continue
            return None
        except Exception:
            return None
    return None
