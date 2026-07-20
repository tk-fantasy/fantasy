"""LLM 角色策略常量。

定义哪些 LLM 角色 per-user 隔离、哪些全局共享。集中定义避免多处重复，
且供 settings/user 路由与 wizard 相关逻辑统一引用。
"""
from __future__ import annotations

# per-user 隔离的角色：chat/summary/stt 按用户存 DB。
# vision/embed 历史上就是全局共享（所有用户用同一个 embed 索引/视觉模型），
# 不进 per-user DB —— wizard 同步 llm_keys 时必须按此过滤，否则全局 key
# 丢失后运行时无法从 per-user 回退（embed_client 走全局解析路径）。
PER_USER_ROLES: set[str] = {"chat", "summary", "stt"}
