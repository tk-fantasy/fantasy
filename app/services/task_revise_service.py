"""定时任务对话式修改与解释服务。

revise_task：基于自然语言指令迭代修改已有定时任务的 schedule / payload（不落库，只返回预览）。
explain_task：plan 模式，回答关于当前定时任务的提问（只读，不修改）。
两者与 schedule_parser_service.parse_schedule 同一调用模式（chat 角色 LLM + 无工具单次调用）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from .schedule_parser_service import _validate_schedule

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是定时任务修改助手。根据用户的修改指令更新任务 JSON，只输出 JSON，不要任何解释或代码块标记。

任务 JSON 的结构：
{
  "name": "任务名称",
  "schedule": {触发配置},
  "payload": {执行内容}
}

schedule 有三种 kind：
1. {"kind": "at", "at": "2026-07-08T10:00:00"}  一次性时刻（本地时间 ISO，不带时区）
2. {"kind": "every", "every_seconds": 3600}      固定间隔（秒）
3. {"kind": "cron", "expr": "0 8 * * *"}          cron 5 字段（分 时 日 月 周）

payload 有三种 kind：
1. {"kind": "tool", "tool_name": "ha_devices___call_service", "tool_input": {"domain":"light","service":"turn_on","entity_id":"light.xxx","data":{}}}
2. {"kind": "message", "message": "该起床了"}      把固定文本投递进会话；改内容时更新 message 字段
3. {"kind": "reminder", "intent": "提醒起床", "original": "每天8点提醒起床"}  绕过 ReAct，直接生成提醒
   - intent 是提醒的核心意图（简短，如"提醒起床""下班提醒"）
   - original 是触发该提醒的用户原话
   - 用户说「改成提醒该喝咖啡了」→ intent 改为"提醒喝咖啡"，original 改为"每天8点提醒喝咖啡"

判断规则：
- 用户只改时间 → 只动 schedule，payload 保持原样。
- 用户只改执行内容 → 只动 payload，schedule 保持原样。改 reminder 内容时 intent 和 original 都要更新。
- 改动 payload.kind 时，补全新 kind 需要的字段。
- 当前时间：{now}。用户说「明天/后天」相对当前时间推算。

输出格式：在任务 JSON 顶层额外加一个 "change_summary" 字段，用一句中文说明你这次改了什么。
例如：{{"name":"...", "schedule":{{...}}, "payload":{{...}}, "change_summary":"触发时间改为每天早上8点"}}
只输出 JSON 对象本身。"""


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象（容忍前后多余文本/代码块标记）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"无法从模型输出解析 JSON: {text[:200]}")


async def revise_task(current_task: dict, instruction: str) -> dict:
    """基于自然语言指令修改定时任务（不落库，返回预览）。

    Args:
        current_task: 当前任务 dict（含 name/schedule/payload 等字段）
        instruction: 用户的修改指令，如「改成每天8点」「把提醒内容改成下班」

    Returns:
        {"task": {...新任务 JSON...}, "summary": "一句中文说明改了什么"}
        {"task": current_task, "summary": "...", "fallback": True} 当 LLM 失败时。

    Raises:
        ValueError: schedule 校验失败（非法 cron / 非法 ISO 等）。
        RuntimeError: LLM 未配置或调用失败。
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("修改指令不能为空")

    from ..agents.langgraph_agent import _load_model_config_from_config
    from ..clients.http_client import new_client, new_sync_client
    from langchain_openai import ChatOpenAI

    model_config = _load_model_config_from_config()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M %A")
    system_prompt = _SYSTEM_PROMPT.replace("{now}", now_str)

    # 只传可改字段，去掉运行时状态噪声
    current_brief = {
        "name": current_task.get("name", ""),
        "schedule": current_task.get("schedule", {}),
        "payload": current_task.get("payload", {}),
    }
    user_prompt = (
        f"当前任务 JSON:\n{json.dumps(current_brief, ensure_ascii=False, indent=2)}\n\n"
        f"修改指令：{instruction}"
    )

    llm = ChatOpenAI(
        model=model_config.get("model", "glm-4-flash"),
        base_url=model_config.get("base_url"),
        api_key=model_config.get("api_key", "not-needed"),
        temperature=0.0,
        http_client=new_sync_client(timeout=30.0),
        http_async_client=new_client(timeout=30.0),
    )
    messages = [("system", system_prompt), ("human", user_prompt)]
    resp = await llm.ainvoke(messages)
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    logger.info("revise_task('%s') -> %s", instruction, raw[:200])

    parsed = _extract_json(raw)
    summary = str(parsed.pop("change_summary", "") or "已更新") or "已更新"

    # 兜底缺失字段（保留原值，避免丢字段）
    parsed.setdefault("name", current_task.get("name", ""))
    parsed.setdefault("schedule", current_task.get("schedule", {}))
    parsed.setdefault("payload", current_task.get("payload", {}))

    # 校验 schedule 合法性（非法则让上层提示用户重试）
    schedule = parsed.get("schedule") or {}
    if schedule:
        _validate_schedule(schedule)

    return {"task": parsed, "summary": summary}


# ============ plan 模式：解释定时任务 ============

_TASK_EXPLAIN_PROMPT = """你是定时任务讲解员。用户把一条已有定时任务的配置 JSON 给你，并问一个关于这个任务的问题。
你的任务是用通俗的中文回答用户的问题，帮他理解这个任务现在是怎么配置的。

任务结构：
- name: 任务名
- schedule: 触发配置
- payload: 触发时执行的内容

schedule 三种 kind（关键：用户最常问「这是执行一次还是每天都跑」）：
- {"kind":"at","at":"2026-07-08T10:00:00"} → 一次性，到这个时刻跑一次就结束（at 是本地时间，ISO 格式 YYYY-MM-DDTHH:MM:SS）。明确告诉用户「只执行一次」。
- {"kind":"every","every_seconds":3600} → 周期重复，每隔这个秒数跑一次（一直循环）。告诉用户「每隔多久重复一次」。
- {"kind":"cron","expr":"0 8 * * *"} → cron 周期，按表达式周期触发。expr 是「分 时 日 月 周」5 字段。
  常见：0 8 * * * = 每天 8 点；30 17 * * 1-5 = 工作日 17:30；0 9 * * 1 = 每周一 9 点；0 0 1 * * = 每月 1 号 0 点。
  解析 cron 时用人话说清楚频率。

payload 三种 kind：
- tool → 控制设备（tool_input 里 domain/service/entity_id）
- message → 把固定文本当用户发言投递进会话
- reminder → 绕过 ReAct，AI 直接生成一句提醒写进会话

回答要求：
- 直接回答用户的问题，不要复述整个 JSON。
- 如果用户问「这个任务是执行一次还是每天」，明确说清楚 schedule.kind 的含义。
- cron 要解析成人话（「每天 8 点」「工作日下午 5 点半」）。
- payload 里如果是 tool，把 entity_id/device 翻译成中文设备名（如 light.chuang_tou_deng → 床头灯），service 翻译成动作（turn_on → 打开）。
- 不确定就如实说，不要编造。
- 简洁，一两句话或几条短列表即可。

当前时间：{now}"""


async def explain_task(current_task: dict, question: str) -> str:
    """plan 模式：用自然语言回答关于当前定时任务的提问（只读，不修改）。

    Returns:
        LLM 生成的解释文本。失败时返回兜底错误信息。
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    from ..agents.langgraph_agent import _load_model_config_from_config
    from ..clients.http_client import new_client, new_sync_client
    from langchain_openai import ChatOpenAI

    model_config = _load_model_config_from_config()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M %A")
    system_prompt = _TASK_EXPLAIN_PROMPT.replace("{now}", now_str)

    brief = {
        "name": current_task.get("name", ""),
        "schedule": current_task.get("schedule", {}),
        "payload": current_task.get("payload", {}),
    }
    user_prompt = (
        f"任务 JSON:\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
        f"用户的问题：{question}"
    )

    llm = ChatOpenAI(
        model=model_config.get("model", "glm-4-flash"),
        base_url=model_config.get("base_url"),
        api_key=model_config.get("api_key", "not-needed"),
        temperature=0.0,
        http_client=new_sync_client(timeout=30.0),
        http_async_client=new_client(timeout=30.0),
    )
    messages = [("system", system_prompt), ("human", user_prompt)]
    resp = await llm.ainvoke(messages)
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    logger.info("explain_task('%s') -> %s", question, raw[:200])
    return str(raw or "").strip() or "无法生成解释。"

