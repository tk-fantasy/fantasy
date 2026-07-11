"""自然语言 → 触发配置解析服务。

把用户的自然语言时间描述（如「每天早上8点」「明天10点」「每30分钟」）
翻译成调度器认识的 at/every/cron 配置。复用 chat 角色的 LLM 配置，
做一次无工具的单次调用，prompt 对齐 OpenClaw cron-tool 的三类型设计。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from croniter import croniter

from ..agents.langgraph_agent import _load_model_config_from_config
from ..clients.http_client import new_client, new_sync_client
from ..services.scheduler_service import summarize_schedule

logger = logging.getLogger(__name__)

# 强制模型只输出 JSON 对象。system 给规则，human 给待翻译短语。
_SYSTEM_PROMPT = """你是一个时间表达翻译器。把用户的中文时间描述翻译成一个调度配置 JSON 对象，只输出 JSON，不要任何解释或代码块标记。

支持三种触发类型（选最贴近用户意图的一种）：

1. 一次性时刻 —— 用户说「X点X分」「明天上午10点」「3小时后」等明确的单一未来时刻：
   {"kind": "at", "at": "2026-07-08T10:00:00"}
   at 用本地时间的 ISO 格式（YYYY-MM-DDTHH:MM:SS），不要带时区后缀。

2. 固定间隔 —— 用户说「每30分钟」「每小时」「每2小时」「每10秒」：
   {"kind": "every", "every_seconds": 1800}
   every_seconds 是整数秒。换算：10秒=10，1分钟=60，30分钟=1800，1小时=3600，1天=86400。

3. cron 表达式 —— 用户说「每天8点」「工作日下午5点半」「每周一9点」「每月1号」等周期性时刻：
   {"kind": "cron", "expr": "0 8 * * *"}
   expr 是 5 字段标准 cron（分 时 日 月 周），用本地时间，不要转 UTC。
   - 每天8点 → "0 8 * * *"
   - 工作日下午5点半 → "30 17 * * 1-5"
   - 每周一9点 → "0 9 * * 1"
   - 每月1号0点 → "0 0 1 * *"

判断规则：
- 单一未来时刻用 at；纯间隔用 every；带「每天/每周/每月/工作日」等周期时刻用 cron。
- 当前时间：{now}。用户说的「明天/后天」相对当前时间推算具体日期。
- 若用户的时间描述模糊无法确定，返回 {"error": "时间描述不明确"}。

只输出 JSON 对象本身。"""


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象（容忍前后多余文本/代码块标记）。"""
    # 先尝试直接解析整段
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 再尝试抠 ```json ... ``` 或 { ... }
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"无法从模型输出解析 JSON: {text[:200]}")


def _validate_schedule(schedule: dict[str, Any]) -> None:
    """校验翻译结果的合法性，非法则抛 ValueError 让上层提示用户。"""
    kind = schedule.get("kind")
    if kind == "at":
        at = str(schedule.get("at", ""))
        if not at:
            raise ValueError("at 触发缺少 at 字段")
        # 宽松校验 ISO 格式
        try:
            datetime.fromisoformat(at.replace("Z", ""))
        except ValueError as e:
            raise ValueError(f"at 字段不是合法 ISO 时刻: {at}") from e
    elif kind == "every":
        secs = schedule.get("every_seconds")
        if not isinstance(secs, (int, float)) or secs <= 0:
            raise ValueError(f"every 触发的 every_seconds 必须为正数，得到: {secs}")
    elif kind == "cron":
        expr = str(schedule.get("expr", ""))
        if not expr:
            raise ValueError("cron 触发缺少 expr 字段")
        if not croniter.is_valid(expr):
            raise ValueError(f"cron 表达式不合法: {expr}")
    else:
        raise ValueError(f"未知触发类型: {kind}（应为 at/every/cron）")


async def parse_schedule(phrase: str) -> dict[str, Any]:
    """把自然语言时间描述翻译成调度配置。

    Returns:
        {"schedule": {...}, "summary": "人话摘要"}
    Raises:
        ValueError: 翻译失败、输出非法或时间描述不明确。
        RuntimeError: LLM 未配置或调用失败。
    """
    phrase = (phrase or "").strip()
    if not phrase:
        raise ValueError("时间描述不能为空")

    model_config = _load_model_config_from_config()

    # 无工具的纯 ChatOpenAI，复用绕代理 http client
    from langchain_openai import ChatOpenAI

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M %A")
    # 注意：prompt 里有大量字面量 { } JSON 示例，不能用 str.format（会把 {kind} 当字段解析）。
    # 用 replace 注入当前时间即可。
    system_prompt = _SYSTEM_PROMPT.replace("{now}", now_str)
    llm = ChatOpenAI(
        model=model_config.get("model", "glm-4-flash"),
        base_url=model_config.get("base_url"),
        api_key=model_config.get("api_key", "not-needed"),
        temperature=0.0,
        http_client=new_sync_client(timeout=30.0),
        http_async_client=new_client(timeout=30.0),
    )

    messages = [
        ("system", system_prompt),
        ("human", phrase),
    ]
    resp = await llm.ainvoke(messages)
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    logger.info("parse_schedule('%s') -> %s", phrase, raw[:200])

    schedule = _extract_json(raw)
    if "error" in schedule:
        raise ValueError(str(schedule["error"]))

    _validate_schedule(schedule)
    return {"schedule": schedule, "summary": summarize_schedule(schedule)}
