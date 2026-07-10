"""SG pipeline 算法模块 — 从 kg-pipeline 移植，去掉对 Config 的依赖。

各模块通过参数注入而非全局配置，embed/LLM 调用通过回调注入。
"""
