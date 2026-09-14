"""LLM 角色策略常量。

定义哪些 LLM 角色 per-user 隔离、哪些全局共享。集中定义避免多处重复，
且供 settings/user 路由与 wizard 相关逻辑统一引用。
"""
from __future__ import annotations

# per-user 隔离的角色：chat/stt 的 provider 绑定按用户存 DB。
# 注意 llm_keys 本体不分角色全量同步进 per-user DB（"将错就错"容错：vision/embed
# 的 key 也写 per-user DB 作为全局备份，供启动自愈 heal_global_keys_from_user_db
# 在全局 .env 丢失时恢复，见 llm_key_service.sync_llm_keys_to_current_user）。
# 本集合只决定 provider 绑定的归属，不再作为 llm_keys 同步的过滤依据——
# 若按旧注释改回"同步时按本集合过滤"，自愈链会断（全局 key 丢失后无从恢复）。
# summary 角色已删除：会话摘要复用对话（chat）模型，无需单独配置。
PER_USER_ROLES: set[str] = {"chat", "stt"}
