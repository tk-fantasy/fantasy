"""模糊文本匹配工具 — 双向子串 + 2-gram 匹配。"""
from __future__ import annotations

import re
from typing import Any


def fuzzy_match(query: str, target: str) -> bool:
    """判断 query 和 target 是否模糊匹配。

    匹配规则（满足任一即返回 True）：
    1. 直接子串包含（双向）
    2. query 的 2-gram（双字片段）在 target 中出现
    """
    if not query or not target:
        return False
    if target in query or query in target:
        return True
    for i in range(len(query) - 1):
        if query[i:i + 2] in target:
            return True
    return False


def match_devices(query: str, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 query 匹配设备列表，返回命中项（保留原 dict，不拷贝）。

    匹配范围：设备 friendly_name 和 area_name（区域名），任一命中即入选。
    用严格的双向子串包含（不用 fuzzy_match 的 2-gram，避免「客厅灯」误命中
    「客厅风扇」这种仅区域词重叠的情况）。

    query 会先剥离常见操作动词（开/关/打/调/设/开启/关闭/打开/调节/设置等），
    因为用户常说「开灯」「关空调」「打开窗帘」，动词会干扰子串匹配——
    「开灯」与「床头灯」无子串关系，但去掉「开」后「灯」能命中所有灯。

    匹配示例：
      query="开灯"      → 剥离为"灯" → 命中所有 name 含「灯」的设备（多匹配）
      query="客厅吊灯"   → 命中 name 含「客厅吊灯」的（唯一）
      query="客厅"       → 命中 area_name 含「客厅」的所有客厅设备（多匹配）

    供 _query_matches_controls（系统提示词注入判定）使用，保证「灯亮度80」
    这类带数值/属性/动词的 query 能正确匹配设备名、可控项得以注入 system prompt，
    避免 LLM 因拿不到正确 service/param 而编造 HA 不存在的服务（如 light.set_level）。

    Args:
        query: 用户指令中的设备描述，如 "开灯" / "空调" / "客厅吊灯"
        devices: ha_service.get_all_devices() 返回的设备列表

    Returns:
        命中的设备子集（按原顺序）
    """
    if not query or not devices:
        return []
    # 剥离操作动词和语气词，提取纯设备词。
    # 用户说法多样：「开灯」「关空调」「把灯关了」「打开客厅窗帘」「关闭一下空调」
    # 「帮我开下灯」「给我关下空调」「麻烦你把灯打开」——不剥离的话这些与设备名
    # 无子串关系，匹配失败。
    # 长词在前，避免「开」抢「开下」「打开」。
    action_words = ("打开", "开启", "开下", "开一下", "关闭", "关掉", "关下", "关一下",
                    "调整", "调节", "设置", "开", "关", "打", "调", "设")
    particles = ("一下", "吧", "了", "呢", "啊", "嘛", "的")
    # 请求前缀:用户常在指令前加的客套/请求词,与设备无关,会干扰子串匹配。
    # 「帮我开下灯」→ 去前缀「帮我」→「开下灯」→ 去动词「开下」→「灯」
    request_prefixes = ("帮我", "麻烦你", "麻烦", "给我", "你来", "能不能", "可以", "请", "帮", "给")
    # 属性/参数词:用户调节类指令带的属性名+数值,与设备名无关,不剥离会让子串匹配失败。
    # 「调亮度到70 灯」→ 去属性词「亮度」+ 数值「到70」→「灯」
    # 「设温度到26 空调」→ 去属性词「温度」+ 数值「到26」→「空调」
    # 「把色温调到300 床头灯」→ 去属性词「色温」+ 数值「到300」→「床头灯」
    # 数值带可选单位(度/%/百分号),数值本身可能是小数。
    # 「模式」是通用后缀(制热模式/制冷模式/风速模式),剥掉后剩具体模式值交 enum_words 处理。
    param_words = ("亮度", "色温", "温度", "风速", "风量", "位置", "音量", "湿度", "模式")
    # 风速/风量等枚举档位词 + 空调 hvac 模式值(非数值)。
    # 「风速调到低 空调」中的「低」、「空调调到制热模式」中的「制热」。
    # 这些词几乎不会出现在设备 friendly_name 里,全局替换安全。
    enum_words = ("低", "中", "高", "自动", "强", "弱", "最大", "最小",
                  "制热", "制冷", "除湿", "送风")
    # 连接词「到/至」:连接属性词与数值/枚举值(「调到70」「调到低」)。属性词和值
    # 都被剥后,孤立的「到」会残留干扰匹配,故单独剥。设备名几乎不含「到/至」。
    link_words = ("到", "至")
    # 「到70」「到70度」「到70%」「至26」「26度」这类数值短语。单独的纯数字也剥。
    num_value_re = re.compile(r"(到|至)?\s*\d+(?:\.\d+)?\s*(度|%|百分号)?")
    q = query.strip()
    # 循环剥离首尾的请求前缀/动词/语气词/「把」字 + 任意位置的属性词/数值，直到不再变化。
    # 「把灯关了」→ 去首「把」→「灯关了」→ 去尾「了」→「灯关」→ 去尾「关」→「灯」
    # 「帮我开下灯」→ 去首「帮我」→「开下灯」→ 去首「开下」→「灯」
    # 「调亮度到70 灯」→ 去属性「亮度」→「调到70 灯」→ 去首动词「调」→「到70 灯」
    #                → 去数值「到70」→「 灯」→ strip →「灯」
    # 每轮末尾 strip：剥离后可能残留前后空格，不 strip 会令子串匹配失败。
    prev = None
    while q != prev and len(q) > 1:
        prev = q
        if q.startswith("把"):
            q = q[1:]
        for w in request_prefixes:
            if q.startswith(w) and len(q) > len(w):
                q = q[len(w):]
                break
        for w in action_words:
            if q.startswith(w) and len(q) > len(w):
                q = q[len(w):]
                break
        for w in action_words:
            if q.endswith(w) and len(q) > len(w):
                q = q[:-len(w)]
                break
        for w in particles:
            if q.endswith(w) and len(q) > len(w):
                q = q[:-len(w)]
                break
        # 属性词和数值可在任意位置出现(常夹在动词和设备名之间),全局替换。
        for w in param_words:
            q = q.replace(w, "")
        for w in enum_words:
            q = q.replace(w, "")
        q = num_value_re.sub("", q)
        # 数值/枚举值被剥后,孤立的「到/至」连接词最后清掉(放 num_value_re 之后,
        # 避免先把「到」删了导致正则匹配不到「到70」)。
        for w in link_words:
            q = q.replace(w, "")
        q = q.strip()
    # 原始 query 和剥离后的都尝试：原始用于「客厅吊灯」这种完整名，剥离后用于「开灯」「把灯关了」
    candidates_q = [q, query] if q != query else [query]
    # 分两轮匹配：先只匹 name，name 命中为空时再匹 area_name。
    # 否则「客厅吊灯」会因 area「客厅」是 query 子串，把所有客厅设备拉进来，唯一变多匹配。
    def _match_by(fields_fn):
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for dev in devices:
            eid = str(dev.get("entity_id", ""))
            if eid in seen:
                continue
            for cq in candidates_q:
                for target in fields_fn(dev):
                    if target and (cq in target or target in cq):
                        result.append(dev)
                        seen.add(eid)
                        break
                else:
                    continue
                break
        return result

    name_matched = _match_by(lambda d: [str(d.get("name", "") or "")])
    if name_matched:
        return name_matched
    return _match_by(lambda d: [str(d.get("name", "") or ""), str(d.get("area_name", "") or "")])
