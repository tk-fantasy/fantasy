"""语义图构建参数 — 从 Aether config.json 读取，零硬编码。

参数分两类：
1. embed/chat 后端：从 llm_keys 找 type=="embed" / type=="chat" 的条目（用户在前端 /keys 配置）。
2. 算法超参：从 config.json 的 "sg" 节点读取，缺失则用默认值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.config import get_config


@dataclass
class SgConfig:
    """语义图构建参数。"""

    # ── 后端 key 条目（来自 llm_keys）──
    embed_key: dict[str, Any] = field(default_factory=dict)
    chat_key: dict[str, Any] = field(default_factory=dict)

    # ── 算法超参（config.json "sg" 节点可覆盖）──
    threshold: float = 0.7              # 向量相似度阈值，决定 LLM 分析的文档对数量
    pca_dim: int = 32                   # PCA 降维目标维度
    umap_n_components: int = 3          # UMAP 输出维度（3D 球用 3）
    umap_n_neighbors: int = 15
    umap_min_dist: float = 0.1
    umap_n_epochs: int = 200
    max_paragraph_chars: int = 500      # 单段最大字符数（截断长文档）
    max_workers: int = 8                # LLM 并发线程数

    @classmethod
    def from_config(cls) -> "SgConfig":
        """从当前 CONFIG 读取参数。每次构建前调用以拿到最新配置。"""
        keys = get_config("llm_keys", []) or []
        embed_key = next((k for k in keys if k.get("type") == "embed"), {})
        chat_key = next((k for k in keys if k.get("type") == "chat"), {})

        sg = get_config("sg", {}) or {}
        return cls(
            embed_key=embed_key,
            chat_key=chat_key,
            threshold=float(sg.get("threshold", 0.7)),
            pca_dim=int(sg.get("pca_dim", 32)),
            umap_n_components=int(sg.get("umap_n_components", 3)),
            umap_n_neighbors=int(sg.get("umap_n_neighbors", 15)),
            umap_min_dist=float(sg.get("umap_min_dist", 0.1)),
            umap_n_epochs=int(sg.get("umap_n_epochs", 200)),
            max_paragraph_chars=int(sg.get("max_paragraph_chars", 500)),
            max_workers=int(sg.get("max_workers", 8)),
        )

    @property
    def ready(self) -> bool:
        """是否具备构建前提（embed + chat key 均已配置）。"""
        return bool(self.embed_key) and bool(self.chat_key)
