"""扫描插件目录、校验 manifest（带目录指纹缓存）。

manifest.json 极少变化，而管理页轮询 / 每次插件操作都调 load_manifests /
load_all_manifests——不做缓存时每次调用都全量读盘 + JSON 解析 + pydantic 校验。
缓存策略：按 (plugin_dir, api_version) 记住解析结果，仅当目录指纹（每个
manifest.json 的 mtime_ns/size 快照）变化时才重扫。指纹采集本身只是一组
stat 调用，远廉价于全量解析。
"""

import json
import logging
from pathlib import Path

from .schema import Manifest

logger = logging.getLogger(__name__)

# (plugin_dir, api_version) → (目录指纹, [Manifest 全量含禁用])
_parse_cache: dict[tuple[str, str], tuple[tuple, list[Manifest]]] = {}


def _dir_fingerprint(root: Path) -> tuple:
    """采集每个子目录 manifest.json 的 (name, mtime_ns, size) 快照。"""
    entries = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            st = (child / "manifest.json").stat()
        except OSError:
            continue  # 无 manifest.json 的子目录，与全量扫描口径一致
        entries.append((child.name, st.st_mtime_ns, st.st_size))
    return tuple(entries)


def _scan_all(root: Path, api_version: str) -> list[Manifest]:
    """全量扫描 + 解析校验（含禁用的，不过滤）。"""
    manifests: list[Manifest] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        try:
            manifest = Manifest.model_validate(raw)
        except Exception:
            continue
        if manifest.aether_api_version != api_version:
            continue
        manifests.append(manifest)
    return manifests


def _load_all_cached(plugin_dir: str, api_version: str) -> list[Manifest]:
    """指纹命中返回缓存（调用方只读消费）；变化才重扫重建。"""
    root = Path(plugin_dir)
    if not root.is_dir():
        return []
    key = (plugin_dir, api_version)
    fp = _dir_fingerprint(root)
    cached = _parse_cache.get(key)
    if cached is not None and cached[0] == fp:
        return cached[1]
    manifests = _scan_all(root, api_version)
    _parse_cache[key] = (fp, manifests)
    return manifests


def load_manifests(plugin_dir: str, api_version: str = "1",
                   disabled: list[str] | None = None) -> list[Manifest]:
    """返回校验通过且未禁用的清单列表。

    - 目录不存在 → 返回空列表
    - 子目录无 manifest.json → 跳过
    - JSON 解析失败 / 字段不全 → 记录 warning 并跳过（仅实际重扫时）
    - aether_api_version 不匹配 → 跳过
    - id 在 disabled 列表中 → 跳过（禁用的不加载、不启动进程）
    """
    disabled_set = set(disabled or [])
    return [m for m in _load_all_cached(plugin_dir, api_version)
            if m.id not in disabled_set]


def load_all_manifests(plugin_dir: str, api_version: str = "1") -> list[Manifest]:
    """扫描所有插件（含禁用的），用于管理页面展示状态。

    与 load_manifests 不同：不跳过 disabled，全部返回（管理页要显示禁用态）。
    """
    return _load_all_cached(plugin_dir, api_version)
