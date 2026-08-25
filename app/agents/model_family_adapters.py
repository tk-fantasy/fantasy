"""模型家族适配器 — 按模型家族封装差异行为（思考开关、提示词习惯等）。

dispatcher / validator 等调用方不硬编码任何家族特判，统一经 get_adapter(model)
取适配器；无匹配返回 None，调用方按通用路径处理。

适配器不写在宿主代码里，以 model_adapter 能力插件形式安装
（integrations/<id>/，manifest 声明 model_adapter 能力 + 提供 adapters.py，
模块级导出 ADAPTERS 列表）。本模块负责发现、加载与缓存：
- 首次 get_adapter 懒加载；refresh_plugin_adapters() 供插件启用/禁用/
  上传/删除后热刷新。
- 新增家族支持：写一个插件包（integrations/ 下有示例），
  宿主零改动。
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)

# 每个适配器插件的适配器模块文件名（宿主与插件作者的约定）
ADAPTERS_MODULE = "adapters.py"


class ModelFamilyAdapter:
    """模型家族适配器基类。

    子类通过 _match_re 声明模型名匹配规则，按需覆写各行为方法；
    未覆写的行为保持默认（原样返回 / 不修改）。
    """

    family: ClassVar[str] = ""
    _match_re: ClassVar[re.Pattern[str] | None] = None

    @classmethod
    def matches(cls, model: str) -> bool:
        if cls._match_re is None:
            return False
        return bool(cls._match_re.search(model or ""))

    def no_think(self, system_text: str, user_text: str) -> tuple[str, str]:
        """返回注入"关闭思考"开关后的 (system_text, user_text)。

        针对混合思考模型：闲聊/控制场景关思考可大幅降低首字延迟。
        默认原样返回——只有支持思考软开关的家族覆写。
        """
        return system_text, user_text


# ── 插件发现与加载 ──────────────────────────────────────────────────────

# 已加载的适配器实例（经 refresh_plugin_adapters 填充；None = 未加载过）
_adapters: list[ModelFamilyAdapter] | None = None


def _default_plugin_dir() -> Path:
    """从 config 解析插件目录（与 integration_routes._resolve_plugin_dir 一致）。"""
    from ..core.config import BASE_DIR, get_config
    return Path(BASE_DIR) / str(get_config("integration.plugin_dir", "integrations"))


def _load_adapters_module(plugin_root: Path, plugin_id: str) -> list[ModelFamilyAdapter]:
    """import 某插件的 adapters.py，取模块级 ADAPTERS（类或实例均可）。"""
    module_path = plugin_root / ADAPTERS_MODULE
    if not module_path.is_file():
        logger.warning("model_adapter 插件 %s 缺少 %s，跳过", plugin_id, ADAPTERS_MODULE)
        return []
    mod_name = f"_aether_model_adapter_{plugin_id}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        logger.exception("加载 model_adapter 插件 %s 的 %s 失败", plugin_id, ADAPTERS_MODULE)
        return []
    exported = getattr(module, "ADAPTERS", None)
    if not isinstance(exported, list) or not exported:
        logger.warning("model_adapter 插件 %s 的 ADAPTERS 未导出或为空", plugin_id)
        return []
    result: list[ModelFamilyAdapter] = []
    for item in exported:
        adapter = item() if isinstance(item, type) else item
        if isinstance(adapter, ModelFamilyAdapter):
            result.append(adapter)
        else:
            logger.warning("model_adapter 插件 %s 导出了非 ModelFamilyAdapter 项: %r",
                           plugin_id, item)
    return result


def refresh_plugin_adapters(plugin_dir: str | Path | None = None,
                            disabled: list[str] | None = None) -> int:
    """重扫插件目录，重建适配器注册表。返回注册的适配器数量。

    plugin_dir / disabled 缺省时从 config 读取（生产路径）；
    测试可显式传入临时目录与禁用列表。
    """
    global _adapters
    from ..integration.manifest_loader import load_all_manifests
    from ..integration.schema import CapabilityType

    if plugin_dir is None:
        plugin_dir = _default_plugin_dir()
    if disabled is None:
        try:
            from ..integration.config_helper import get_disabled_plugins
            disabled = get_disabled_plugins()
        except Exception:
            disabled = []

    loaded: list[ModelFamilyAdapter] = []
    for manifest in load_all_manifests(str(plugin_dir)):
        if manifest.id in set(disabled):
            continue
        if not manifest.has_capability(CapabilityType.MODEL_ADAPTER):
            continue
        loaded.extend(_load_adapters_module(Path(plugin_dir) / manifest.id, manifest.id))

    _adapters = loaded
    logger.info("模型家族适配器已加载 %d 个: %s",
                len(loaded), [a.family for a in loaded])
    return len(loaded)


def get_adapter(model: str) -> ModelFamilyAdapter | None:
    """按模型名取家族适配器；无匹配返回 None。首次调用懒加载插件。"""
    global _adapters
    if _adapters is None:
        refresh_plugin_adapters()
        if _adapters is None:  # 加载异常兜底
            _adapters = []
    key = (model or "").strip().lower()
    for adapter in _adapters:
        if type(adapter).matches(key):
            return adapter
    return None


def reset_adapters() -> None:
    """清空注册表（测试用；下一次 get_adapter 重新懒加载）。"""
    global _adapters
    _adapters = None
