from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from ..core.config import WEEKDAY_NAMES, get_config
from ..utils.text_match import match_devices

logger = logging.getLogger(__name__)

# ============ RAG 文档助手 Prompt ============
RAG_SYSTEM_PROMPT_TEMPLATE = (
    "你是 Aether 使用助手，专门根据 Aether 项目的文档资料来回答用户使用问题。\n"
    "你只能根据下方【参考文档】的内容回答，不要编造任何文档中没有的信息。\n"
    "你没有智能家居控制能力，不能开关灯、控制空调、查看摄像头等——那些是主助手的功能。\n"
    "你的职责仅限于：帮助用户了解如何安装、配置、使用 Aether 项目。\n"
    "如果文档中没有相关信息，请如实告知「文档中暂未收录此内容」。\n\n"
    "【参考文档】\n{context}"
)

# ============ 规则生成 Prompt 模板 ============
RULE_SYSTEM_PROMPT_TEMPLATE = (
    "你是一个家庭自动化规则解析器。把用户的一句话解析成自动化规则 JSON。\n"
    "只返回 JSON，不要 markdown，不要解释。\n\n"
    "输出字段:\n"
    '  "name": 规则简短名称,\n'
    '  "condition": 用一句自然语言描述触发条件(摄像头能看到的视觉事件),\n'
    '  "actions": 动作数组,每个动作包含 mcp_tool_name 和 mcp_tool_input,\n'
    '  "action_descriptions": 每个动作的中文描述数组,\n'
    '  "cooldown_seconds": 防重复触发的冷却秒数(默认10),\n'
    '  "summary": 规则总结。\n\n'
    "动作格式说明:\n"
    '- mcp_tool_name: 必须是 "ha_devices___call_service"\n'
    '- mcp_tool_input: {{"domain": "域", "service": "服务名", "entity_id": "实体id", "data": {{...}}}}\n'
    '  - domain/service: 严格从下面设备可控项中读取\n'
    '  - entity_id: 从设备列表括号中取完整 entity_id\n'
    '  - data: 有 param 行的用 param 名作 key、用户要求的值作 value；\n'
    '          动作类型（无 param 行）的 data 写 {{}}。\n\n'
    "### 示例\n"
    "用户说「空调26度」→ 可控项：\n"
    "  Temperature — 滑块 16°C~30°C, 当前 22°C\n"
    "    domain=climate | service=set_temperature | param=temperature\n"
    "→ data: {{temperature: 26}}\n\n"
    "用户说「打开窗帘」→ 可控项：\n"
    "  Open Cover — 动作\n"
    "    domain=cover | service=open_cover\n"
    "→ data: {{}}  （动作类型没有 param）\n\n"
    "设备可控项（直接用于 call_service，不要编造）：\n"
    "{controls_text}\n\n"
    "设备 entity_id 对照:\n"
    "{device_list_text}\n\n"
    "重要规则：\n"
    "- data 的值必须是纯数字或纯字符串，不加单位（% 符号不要写）。\n"
    "- 滑块类型：param 行指定了参数名，必须填入 data。\n"
    "- 动作类型：无 param 行，data 写 {{}}。\n"
    "条件请用自然语言描述视觉可见的事件，不要添加用户没有提到的内容。"
)

# 聊天助手的角色设定 (可在 config.json 的 chat_assistant.persona 覆盖)。
# 这里只写"它是谁、性格、说话风格、边界",具体能力清单由代码动态拼接,
# 保证和真实工具同步，不会写了能力又对不上。
DEFAULT_PERSONA = (
    "你是 Aether，一个本地家庭智能助手，运行在用户自己的电脑上，"
    "连着一个摄像头，通过 Home Assistant 控制全屋智能设备。"
    "你的性格：简洁、务实，懂技术又不啰嗦。"
    "说话用中文，口语化，一次把事说清楚，不堆废话。"
)

GUIDELINES = (
    "几条原则:\n"
    "- 当前时间和天气已在系统信息中提供，直接回答即可，无需调用工具。\n"
    "\n"
    "## 能力边界\n"
    "你只能控制 Home Assistant 设备、查看摄像头、搜索网页、验证状态。\n"
    "不能操作电脑文件、运行命令、发邮件。能力之外的事直接说「我做不到」，不要假装完成。\n"
    "\n"
    "## 工具\n"
    "- 动作前先 get_entities 看真实设备与可控项，domain/service/param/entity_id 都取自返回，不要自己拼造。\n"
    "- verify_condition / verify_action 只读，改状态只能 call_service。\n"
    "- 用户要核对/验证某状态时调 verify_action，不要凭印象回答。\n"
    "- 「如果…就…」类条件指令三步走：先 verify_condition 验条件；满足才 call_service 执行；再做 verify_action 核对。条件不满足就告诉用户、不执行。\n"
    "- 用户问画面里/现在有什么等视觉问题时，先调 describe_state / vision_chat 看画面，不要回「我无法判断」。\n"
    "- 【定时任务】用户指定未来时间点或周期要做某事时（「X点X分开灯」「每天8点提醒」「每小时刷新」「X分钟后关灯」），"
    "必须调 scheduled_task_create 创建定时任务，让系统到点自动执行——禁止立即 call_service。"
    "只有用户明确要「现在/马上」做时才立即执行。时间用 at（一次性）/ every（间隔）/ cron（表达式）。\n"
    "- 工具调用必须走 tool_call 机制，不要在回复文本里写 JSON 代码块模拟工具调用。\n"
    "\n"
    "## 诚实\n"
    "- 以工具返回的真实结果为准。没调工具就说不知道，没执行就说没执行，不要描述根本没发生的操作。\n"
    "- entity_id 不存在或 call_service 失败时，立即停下告知用户，不重试、不换 id 再试。\n"
    "- 设备名唯一，用户提到设备名直接匹配，不要追问房间。\n"
    "- 回答简短，调完工具用自然语言简洁总结，不要沉默或只丢工具结果。\n"
)


def _query_matches_controls(query: str | None, controls_text: str) -> bool:
    """动态：query 与 controls_text 中任一设备名匹配 → 注入可控项；无匹配 → 空。

    必须复用 match_devices 的剥离+子串匹配逻辑，而不是 fuzzy_match 的 2-gram——
    否则「帮我把灯的亮度调整到80」剥离前后的 2-gram 都不在「床头灯」里，判定为
    不匹配 → 可控项不注入 system prompt → LLM 不知道正确 service/param，编造出
    light.set_level 这类 HA 根本没有的服务。
    """
    if not query or not controls_text:
        return False
    # 从 controls_text 提取设备名行（如 "床头灯 (light.chuang_tou_deng)"）
    devices: list[dict] = []
    for line in controls_text.split("\n"):
        idx = line.find(" (")
        if idx > 0:
            name = line[:idx].strip()
            eid = line[idx + 2: line.rfind(")")] if ")" in line else ""
            if name and eid:
                devices.append({"name": name, "entity_id": eid, "area_name": ""})
    if not devices:
        return False
    # 任一设备命中即注入
    return bool(match_devices(query, devices))


async def build_system_prompt(
    visual_summary: dict | None = None,
    device_catalog: str | None = None,
    device_controls: str | None = None,
    vision_focuses: list[dict] | None = None,
    query: str | None = None,
    summaries: list[dict] | None = None,
) -> str:
    """组装聊天助手的系统提示词:角色设定 + 能力清单 + 原则 + 设备目录 + 当前快照 + 天气 + 历史摘要。"""
    persona = str(get_config("chat_assistant.persona", "") or "").strip() or DEFAULT_PERSONA
    guidelines = str(get_config("chat_assistant.guidelines", "") or "").strip() or GUIDELINES
    parts = [persona, "", guidelines]

    # 注入当前时间，防止 LLM 编造时间
    from ..mcp.local_mcp_servers import _get_tz_offset_hours
    local_tz = timezone(timedelta(hours=_get_tz_offset_hours()))
    now = datetime.now(local_tz)
    current_time_str = (
        f"当前时间：{now.year}年{now.month}月{now.day}日 "
        f"{WEEKDAY_NAMES[now.weekday()]} {now.hour}:{now.minute:02d}"
    )
    parts.append(f"\n{current_time_str}")

    # 注入当前天气（从缓存读取，不阻塞）
    try:
        from .weather_service import get_weather, format_weather_detail
        weather_data = await get_weather()
        weather_str = format_weather_detail(weather_data)
        if weather_str:
            parts.append(f"\n{weather_str}")
    except Exception:
        logger.debug("Failed to get weather for system prompt", exc_info=True)

    if device_controls:
        if _query_matches_controls(query, device_controls):
            parts.append(f"\n设备可控项（直接用于 call_service）：\n{device_controls}")
    elif device_catalog:
        parts.append(f"\n当前 Home Assistant 可用设备:\n{device_catalog}")

    # 注入视觉关注重点 (focus)
    if visual_summary:
        action = visual_summary.get("action", "idle")
        feedback = visual_summary.get("feedback", "")
        parts.append(f"当前摄像头状态：动作={action} 反馈={feedback}")

    if vision_focuses:
        enabled_texts = [f["text"] for f in vision_focuses if f.get("enabled", True)]
        if enabled_texts:
            parts.append(f"\n摄像头关注重点：{'；'.join(enabled_texts)}")

    if summaries:
        summary_texts = []
        for s in summaries:
            text = s.get("text", "")
            if text:
                summary_texts.append(f"- {text}")
        if summary_texts:
            parts.append(f"\n你与用户的历史对话摘要：\n" + "\n".join(summary_texts))
            parts.append("（以上是之前对话的摘要，用户可能基于这些内容继续提问。）")

    return "\n".join(parts)
