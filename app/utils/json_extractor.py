from __future__ import annotations

import json
import re

_JSON_MARKDOWN_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _find_balanced_json(text: str) -> str | None:
    """查找文本中第一个平衡的 JSON 对象。

    使用括号计数法正确处理嵌套 JSON。
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        char = text[i]

        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None


def extract_json_from_content(content: str) -> str:
    """从 LLM 返回的杂文本中提取 JSON 字符串。

    两层策略:
    1. 先尝试非贪婪匹配 markdown 代码块内的 {...}（精确）
    2. 兜底使用括号计数法查找平衡的 JSON 对象（正确处理嵌套）
    都找不到则原样返回，由调用方 json.loads 决定成败。
    """
    content = content.strip()

    json_match = _JSON_MARKDOWN_PATTERN.search(content)
    if json_match:
        return json_match.group(1).strip()

    # 使用括号计数法查找平衡的 JSON
    balanced = _find_balanced_json(content)
    if balanced:
        # 验证是否是有效的 JSON
        try:
            json.loads(balanced)
            return balanced.strip()
        except json.JSONDecodeError:
            pass

    return content
