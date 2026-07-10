"""SG LLM 工具 — 同步 LLM 调用辅助，供 entity_extractor/relation_analyzer 使用。

通过注入的 chat_fn 回调调用 LLM（由 sg_service 从 async embed_client/chat_client 桥接为同步），
不直接依赖 Aether 的异步客户端，保持 pipeline 的同步执行模型。
"""
import re
import json


def parse_json_from_llm(content: str) -> dict | None:
    """从 LLM 输出中提取 JSON 对象。"""
    json_match = re.search(r"\{.*\}", content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            return None
    return None


def call_chat(chat_fn, messages: list[dict], max_tokens: int = 1024) -> str:
    """调用 LLM 并返回文本内容。

    Args:
        chat_fn: 同步回调，签名 chat_fn(messages, max_tokens) -> str
        messages: 消息列表
        max_tokens: 最大输出 token 数
    Returns:
        LLM 输出文本
    """
    return chat_fn(messages, max_tokens=max_tokens)
