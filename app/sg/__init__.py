"""语义图（Semantic Graph）子包 — KG 构建与消费。

将外部 kg-pipeline 的算法集成进 Aether，复用用户在前端配置的 embed/LLM key。
构建产物落在 app/sg/output/<task>/，供 doc_routes / rag_service 消费。
"""
