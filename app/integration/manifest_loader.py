"""扫描插件目录、校验 manifest。"""

import json
import logging
from pathlib import Path

from .schema import Manifest

logger = logging.getLogger(__name__)


def load_manifests(plugin_dir: str, api_version: str = "1") -> list[Manifest]:
    """扫描 plugin_dir 下每个子目录的 manifest.json，返回校验通过的清单列表。

    - 目录不存在 → 返回空列表
    - 子目录无 manifest.json → 跳过
    - JSON 解析失败 / 字段不全 → 记录 warning 并跳过
    - aether_api_version 不匹配 → 跳过
    """
    root = Path(plugin_dir)
    if not root.is_dir():
        return []

    manifests: list[Manifest] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue

        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("插件 %s manifest 解析失败: %s", child.name, exc)
            continue

        try:
            manifest = Manifest.model_validate(raw)
        except Exception as exc:  # pydantic.ValidationError
            logger.warning("插件 %s manifest 校验失败: %s", child.name, exc)
            continue

        if manifest.aether_api_version != api_version:
            logger.warning(
                "插件 %s API 版本不匹配 (期望 %s, 实际 %s),跳过",
                manifest.id, api_version, manifest.aether_api_version,
            )
            continue

        manifests.append(manifest)

    return manifests
