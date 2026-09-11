"""模糊文本匹配工具 — 双向子串 + 2-gram 匹配 + 指令目标分层判定。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


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


# ---------------------------------------------------------------------------
# query 归一化 — 剥掉动词/前缀/语气词/属性词/数值，留下纯设备词
# ---------------------------------------------------------------------------

# 长词在前，避免「开」抢「开下」「打开」。
_ACTION_WORDS = ("打开", "开启", "开下", "开一下", "关闭", "关掉", "关下", "关一下",
                 "调整", "调节", "设置", "开", "关", "打", "调", "设")
_PARTICLES = ("一下", "吧", "了", "呢", "啊", "嘛", "的")
# 请求前缀:用户常在指令前加的客套/请求词,与设备无关,会干扰子串匹配。
# 「帮我开下灯」→ 去前缀「帮我」→「开下灯」→ 去动词「开下」→「灯」
_REQUEST_PREFIXES = ("帮我", "麻烦你", "麻烦", "给我", "你来", "能不能", "可以", "请", "帮", "给")
# 属性/参数词:用户调节类指令带的属性名+数值,与设备名无关,不剥离会让子串匹配失败。
# 「调亮度到70 灯」→ 去属性词「亮度」+ 数值「到70」→「灯」
# 「设温度到26 空调」→ 去属性词「温度」+ 数值「到26」→「空调」
# 「把色温调到300 床头灯」→ 去属性词「色温」+ 数值「到300」→「床头灯」
# 数值带可选单位(度/%/百分号),数值本身可能是小数。
# 「模式」是通用后缀(制热模式/制冷模式/风速模式),剥掉后剩具体模式值交 _ENUM_WORDS 处理。
_PARAM_WORDS = ("亮度", "色温", "温度", "风速", "风量", "位置", "音量", "湿度", "模式")
# 风速/风量等枚举档位词 + 空调 hvac 模式值(非数值)。
# 「风速调到低 空调」中的「低」、「空调调到制热模式」中的「制热」。
# 这些词几乎不会出现在设备 friendly_name 里,全局替换安全。
_ENUM_WORDS = ("低", "中", "高", "自动", "强", "弱", "最大", "最小",
               "制热", "制冷", "除湿", "送风")
# 连接词「到/至」:连接属性词与数值/枚举值(「调到70」「调到低」)。属性词和值
# 都被剥后,孤立的「到」会残留干扰匹配,故单独剥。设备名几乎不含「到/至」。
_LINK_WORDS = ("到", "至")
# 「到70」「到70度」「到70%」「至26」「26度」这类数值短语。单独的纯数字也剥。
_NUM_VALUE_RE = re.compile(r"(到|至)?\s*\d+(?:\.\d+)?\s*(度|%|百分号)?")


def _normalize_query(query: str) -> str:
    """剥离操作动词/请求前缀/语气词/属性词/数值，返回纯设备词。

    用户说法多样：「开灯」「关空调」「把灯关了」「打开客厅窗帘」「关闭一下空调」
    「帮我开下灯」「给我关下空调」「麻烦你把灯打开」——不剥离的话这些与设备名
    无子串关系，匹配失败。

    循环剥离首尾的请求前缀/动词/语气词/「把」字 + 任意位置的属性词/数值，直到不再变化：
      「把灯关了」→ 去首「把」→「灯关了」→ 去尾「了」→「灯关」→ 去尾「关」→「灯」
      「帮我开下灯」→ 去首「帮我」→「开下灯」→ 去首「开下」→「灯」
      「调亮度到70 灯」→ 去属性「亮度」→「调到70 灯」→ 去首动词「调」→「到70 灯」
                     → 去数值「到70」→「 灯」→ strip →「灯」

    `len(q) > 1` 的循环条件是刻意的：单字 query 再剥就成空串，而空串与任何
    target 都构成子串关系（`"" in x` 恒真），会把全部设备拉进候选。
    """
    q = query.strip()
    # 每轮末尾 strip：剥离后可能残留前后空格，不 strip 会令子串匹配失败。
    prev = None
    while q != prev and len(q) > 1:
        prev = q
        if q.startswith("把"):
            q = q[1:]
        for w in _REQUEST_PREFIXES:
            if q.startswith(w) and len(q) > len(w):
                q = q[len(w):]
                break
        for w in _ACTION_WORDS:
            if q.startswith(w) and len(q) > len(w):
                q = q[len(w):]
                break
        for w in _ACTION_WORDS:
            if q.endswith(w) and len(q) > len(w):
                q = q[:-len(w)]
                break
        for w in _PARTICLES:
            if q.endswith(w) and len(q) > len(w):
                q = q[:-len(w)]
                break
        # 属性词和数值可在任意位置出现(常夹在动词和设备名之间),全局替换。
        for w in _PARAM_WORDS:
            q = q.replace(w, "")
        for w in _ENUM_WORDS:
            q = q.replace(w, "")
        q = _NUM_VALUE_RE.sub("", q)
        # 数值/枚举值被剥后,孤立的「到/至」连接词最后清掉(放 _NUM_VALUE_RE 之后,
        # 避免先把「到」删了导致正则匹配不到「到70」)。
        for w in _LINK_WORDS:
            q = q.replace(w, "")
        # 结构助词「的」全局剥：用户口语「会客厅的灯」与设备名「会客厅灯 左键」
        # 只差一个「的」，不剥则子串匹配失败——候选反查/语义校验都命中不了。
        q = q.replace("的", "")
        q = q.strip()
    return q


def _name_fields(dev: dict[str, Any]) -> list[str]:
    return [str(dev.get("name", "") or "")]


def _name_area_fields(dev: dict[str, Any]) -> list[str]:
    return [str(dev.get("name", "") or ""), str(dev.get("area_name", "") or "")]


def _match_by(
    candidates_q: Iterable[str],
    devices: list[dict[str, Any]],
    fields_fn: Callable[[dict[str, Any]], list[str]],
) -> list[dict[str, Any]]:
    """双向子串匹配：按 query 变体逐个尝试，按 entity_id 去重并保留原顺序。"""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dev in devices:
        eid = str(dev.get("entity_id", ""))
        if eid in seen:
            continue
        for cq in candidates_q:
            # 空变体必须跳过：`"" in target` 恒真，会把全部设备拉进候选
            if not cq:
                continue
            for target in fields_fn(dev):
                if target and (cq in target or target in cq):
                    result.append(dev)
                    seen.add(eid)
                    break
            else:
                continue
            break
    return result


def match_devices(query: str, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 query 匹配设备列表，返回命中项（保留原 dict，不拷贝）。

    匹配范围：设备 friendly_name 和 area_name（区域名），任一命中即入选。
    用严格的双向子串包含（不用 fuzzy_match 的 2-gram，避免「客厅灯」误命中
    「客厅风扇」这种仅区域词重叠的情况）。

    query 会先经 `_normalize_query` 剥离常见操作动词（开/关/打/调/设/开启/关闭/
    打开/调节/设置等），因为用户常说「开灯」「关空调」「打开窗帘」，动词会干扰
    子串匹配——「开灯」与「床头灯」无子串关系，但去掉「开」后「灯」能命中所有灯。

    匹配示例：
      query="开灯"      → 剥离为"灯" → 命中所有 name 含「灯」的设备（多匹配）
      query="客厅吊灯"   → 命中 name 含「客厅吊灯」的（唯一）
      query="客厅"       → 命中 area_name 含「客厅」的所有客厅设备（多匹配）

    保证「灯亮度80」这类带数值/属性/动词的 query 能正确匹配设备名，
    避免 LLM 因拿不到正确 service/param 而编造 HA 不存在的服务（如 light.set_level）。

    只回答「哪些设备被命中」，不回答「该不该直接执行」——后者是
    `classify_target` 的职责（分层判定 + 候选收窄）。

    Args:
        query: 用户指令中的设备描述，如 "开灯" / "空调" / "客厅吊灯"
        devices: ha_service.get_all_devices() 返回的设备列表

    Returns:
        命中的设备子集（按相关性排序）
    """
    if not query or not devices:
        return []
    q = _normalize_query(query)
    # 原始 query 和剥离后的都尝试：原始用于「客厅吊灯」这种完整名，剥离后用于「开灯」「把灯关了」
    candidates_q = [q, query] if q != query else [query]
    # 分两轮匹配：先只匹 name，name 命中为空时再匹 area_name。
    # 否则「客厅吊灯」会因 area「客厅」是 query 子串，把所有客厅设备拉进来，唯一变多匹配。
    name_matched = _match_by(candidates_q, devices, _name_fields)
    if name_matched:
        return _rank_by_relevance(name_matched, query)
    return _rank_by_relevance(
        _match_by(candidates_q, devices, _name_area_fields),
        query,
    )


# 主控 domain：用户说「开大门」时，真正想操作的是开关/灯/音箱这类，而不是
# 同名传感器（故障/版本号）或配置项（童锁/灵动开关）。匹配多候选时把这些排前面。
_PRIMARY_DOMAINS = frozenset({
    "light", "switch", "climate", "cover", "fan", "humidifier",
    "lock", "media_player", "vacuum", "valve", "water_heater",
    "siren", "alarm_control_panel",
})
# 纯属性/诊断 domain：匹配时降权，避免「打开大门」命中一堆 sensor
_DIAGNOSTIC_DOMAINS = frozenset({"sensor", "binary_sensor"})


def _rank_by_relevance(matched: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """对命中的设备按相关性排序，主控实体优先、诊断类靠后。

    不删减任何候选（调用方可能依赖完整命中集合做校验），只调整顺序，
    让 LLM 和 match_devices 的消费方在「多匹配」时优先看到最可能的目标。
    """
    def score(dev: dict[str, Any]) -> tuple[int, int]:
        domain = str(dev.get("entity_id", "")).split(".", 1)[0]
        name = str(dev.get("name", "") or "")
        # 主控 domain 得 0 分（最优先），诊断 domain 得 2 分，其他得 1 分
        if domain in _PRIMARY_DOMAINS:
            base = 0
        elif domain in _DIAGNOSTIC_DOMAINS:
            base = 2
        else:
            base = 1
        # 名称与 query 精确相等（剥离后）优先于子串包含：用户说「大门开关」时，
        # name=「大门开关」应排在 name=「大门开关故障」前面。
        q = query.strip()
        exact = 0 if (q and name == q) else 1
        return (base, exact)

    return sorted(matched, key=score)


# ---------------------------------------------------------------------------
# 指令目标分层判定 — 回答「该直接执行，还是让用户挑」
# ---------------------------------------------------------------------------

TIER_EXACT = "exact"                  # 精确同名（含一设备多子实体）→ 全部执行，不问
TIER_ALL_MARKER = "all_marker"        # 带「所有/全部/都」→ 命中集全部执行，不问
TIER_UNIQUE = "unique"                # 唯一候选 → 直接执行
TIER_AMBIGUOUS = "ambiguous"          # 多候选且不同名 → 转用户选择
TIER_CATEGORY_MISS = "category_miss"  # 说了品类词但设备不存在 → 转用户选择（并如实说不存在）
TIER_NONE = "none"                    # 无任何线索 → 放行给 LLM 推断（「太热了→开空调」）

# 全量词：出现即表示用户要「全部」，多候选时不必再问。
_ALL_MARKERS = ("所有", "全部", "整个", "都", "全")

# 设备级精确同名的子功能分隔符：「B灯 左键」算「B灯」的子功能，
# 「客厅灯带」**不**算「客厅灯」的子功能（「带」不是分隔符）——这条边界是
# 防止精确同名把两个不同设备吞成一个的唯一防线。
_SUB_SEPARATORS = (" ", "-", "_", "·", "—", "（", "(")

# 弹框候选上限：再多用户也勾不过来，超出部分由调用方提示「还有 N 个」
_MAX_CANDIDATES = 12

# 复合句连接词：出现即表示一句话里有多条指令
_COMPOUND_CONNECTIVES = ("然后", "接着", "同时", "并且", "顺便", "之后")


def _looks_compound(query: str) -> bool:
    """判断是否为一句话含多条指令的复合句（「开灯关窗帘」「关灯然后拉窗帘」）。

    复合句必须整句放行给 LLM 拆解，闸门不能插手：归一化是按「单个设备词」设计的，
    「开灯关窗帘」会被剥成「灯关窗帘」——两轮子串匹配全空，落到 category_miss
    去弹框，文案还会是"没有找到『灯关窗帘』"这种把归一化中间态念给用户听的话。

    判据用「开/关」出现次数 ≥2 或显式连接词。只数这两个字是权衡过的：把「调」
    也算进去会让「空调开到26度」（"空调"含"调"）被误判成复合句。误判成复合句的
    代价是退回旧的放行行为（不会误弹框、不会误拒），可接受。
    """
    if any(w in query for w in _COMPOUND_CONNECTIVES):
        return True
    return query.count("开") + query.count("关") >= 2


@dataclass
class MatchResult:
    """分层判定结果。

    tier 决定调用方（call_service 闸门）的行为；candidates 是入参 entries 里的
    原 dict（不拷贝），口径为 device_registry.build_match_index 的产出，也兼容
    ha_service.get_all_devices 的裸设备（缺 label/device_name 时退回 name）。
    """

    tier: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    normalized: str = ""

    @property
    def needs_user_selection(self) -> bool:
        """是否必须转用户选择（弹框 / 口头列举），不得由 LLM 自行挑一个。"""
        return self.tier in (TIER_AMBIGUOUS, TIER_CATEGORY_MISS)


def _entry_names(entry: dict[str, Any]) -> list[str]:
    """条目可用于精确比对的名称：label（设备名+子功能短名）、实体名、父设备名。

    父设备名参与比对是「开启B灯 → B灯的两个子实体全开」的实现基础：MIoT 子实体
    的 friendly_name 是「B灯 左键」这种带父名前缀的形态，用户只会说父名。
    """
    out: list[str] = []
    for key in ("label", "name", "device_name"):
        value = str(entry.get(key) or "")
        if value and value not in out:
            out.append(value)
    return out


def _strip_edge_markers(nq: str) -> tuple[str, bool]:
    """剥掉归一化后仍留在**首尾**的全量词，返回 (剥后, 是否剥到过)。

    只剥首尾是刻意的：「成都的灯都关了」归一化后是「成都灯都」，句尾的「都」是
    副词该剥，但「成都」里的「都」是地名用字——全局替换会把设备名一起削掉，
    反而匹配不上。

    「是否剥到过」同时充当「用户说了全量词」的判据，比在原始 query 里搜子串
    精确得多：设备名自带「全」的句子（「打开全屋电源」）不会被误判成「全开」。
    """
    q = nq
    changed = False
    while q:
        before = q
        for w in _ALL_MARKERS:
            if len(q) > len(w) and q.startswith(w):
                q = q[len(w):]
                break
            if len(q) > len(w) and q.endswith(w):
                q = q[:-len(w)]
                break
        if q == before:
            break
        changed = True
    return q, changed


def _common_suffix_len(a: str, b: str) -> int:
    """两个字符串的公共后缀长度（品类尾词长度）。"""
    count = 0
    for ca, cb in zip(reversed(a), reversed(b)):
        if ca != cb:
            break
        count += 1
    return count


def _shares_category_tail(nq: str, entry: dict[str, Any]) -> bool:
    """条目名与 query 是否共享品类尾词（末字相同即算，如「月球灯」与「床头灯」共享「灯」）。"""
    if not nq:
        return False
    return any(_common_suffix_len(nq, name) >= 1 for name in _entry_names(entry))


def _exact_hits(variants: list[str], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """精确同名命中：名称完全相等，或名称 = query + 分隔符 + 子功能名。"""
    hits: list[dict[str, Any]] = []
    for entry in entries:
        names = _entry_names(entry)
        for nq in variants:
            if any(name == nq for name in names):
                hits.append(entry)
                break
            if any(name.startswith(nq) and name[len(nq):len(nq) + 1] in _SUB_SEPARATORS
                   for name in names):
                hits.append(entry)
                break
    return hits


def _finalize(candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """排序（主控 domain 优先）+ 截断到弹框可展示的上限。"""
    return _rank_by_relevance(list(candidates), query)[:_MAX_CANDIDATES]


def classify_target(
    query: str,
    entries: list[dict[str, Any]],
    *,
    domain: str | None = None,
) -> MatchResult:
    """判定用户指令的目标设备，返回分层结果 + 候选集。

    与 `match_devices` 的分工：后者只回答「哪些设备被命中」，本函数回答
    「该直接执行、全部执行，还是必须让用户挑」。判定顺序（**exact 必须先于
    all_marker**，否则「成都的灯都关了」会被句尾的「都」误判成全开）：

    1. `exact`         精确同名（设备名/实体名/别名/label，或父名+分隔符+子功能名）
    2. `all_marker`    带全量词且有候选
    3. `unique`        唯一候选
    4. `ambiguous`     多候选 → 转用户选择
    5. `category_miss` 无候选但 query 带品类尾词 → 转用户选择（设备不存在，不许瞎猜）
    6. `none`          无线索 → 放行

    前置豁免：**复合句**（「开灯关窗帘」「关灯然后拉窗帘」）直接判 `none`，
    整句交给 LLM 拆解——归一化按单设备词设计，硬判会误落 category_miss。

    Args:
        query: 用户原话（未剥离），如 "开灯" / "把所有灯关掉" / "月球的灯"
        entries: device_registry.build_match_index() 的产出
        domain: 可选，调用方（LLM 所选实体）的 domain。仅在 query 推不出品类
            尾词时用作候选收窄的兜底——**不能优先用它**：LLM 选错 domain 时
            （用户说灯、它去开风扇），按它的 domain 过滤反而会把错误洗白成
            「唯一候选」放行。

    Returns:
        MatchResult(tier, candidates, normalized)
    """
    raw = (query or "").strip()
    if not raw or not entries:
        return MatchResult(TIER_NONE, [], _normalize_query(raw) if raw else "")

    # 复合句（一句话多条指令）整句放行：归一化按单设备词设计，拆不了「开灯关窗帘」，
    # 硬判会落进 category_miss 弹框。拆解是 LLM 的活，闸门不插手。
    if _looks_compound(raw):
        return MatchResult(TIER_NONE, [], _normalize_query(raw))

    nq_plain = _normalize_query(raw)
    nq_bare, has_all_marker = _strip_edge_markers(nq_plain)
    # 剥后优先、原样兜底：剥标记才能让「把所有灯关掉」匹配上（原样「所有灯」与
    # 实体名无子串关系），但设备名自带全量词时（「全屋电源」）必须靠原样变体。
    variants = [v for v in dict.fromkeys([nq_bare, nq_plain, raw]) if v]
    normalized = nq_bare or nq_plain

    exact = _exact_hits(variants, entries)
    if exact:
        return MatchResult(TIER_EXACT, _finalize(exact, raw), normalized)

    # 两轮匹配与 match_devices 同口径：先 name，name 空了才退 area
    name_matched = _match_by(variants, entries, _name_fields)
    via_area = not name_matched
    matched = name_matched or _match_by(variants, entries, _name_area_fields)

    if not matched:
        # 说了品类词却一个都没命中：设备大概率不存在。给出同品类的真实设备让用户挑，
        # 而不是放行让 LLM 从全量目录里随便抓一个（「月球的灯」把全屋灯开了的根因）。
        tail_entries = [e for e in entries if _shares_category_tail(normalized, e)]
        if tail_entries:
            return MatchResult(TIER_CATEGORY_MISS, _finalize(tail_entries, raw), normalized)
        return MatchResult(TIER_NONE, [], normalized)

    candidates = matched
    narrowed_by_tail = False
    if via_area:
        # area 兜底轮很松（「客厅」是「客厅灯」的子串 → 整个客厅的设备都进来），
        # 用品类尾词收窄回同类设备。name 轮命中的一律不收窄——「开灯」必须带上
        # 「客厅灯带」，它以「带」结尾不共享尾词「灯」，收窄会把它误删。
        kept = [e for e in matched if _shares_category_tail(normalized, e)]
        if kept:
            candidates = kept
            narrowed_by_tail = True
    if not narrowed_by_tail and domain:
        by_domain = [e for e in candidates if str(e.get("domain") or "") == domain]
        # 过滤后为空则保留原候选：宁可弹框让用户挑，也不能把候选清空变成放行
        if by_domain:
            candidates = by_domain

    if has_all_marker:
        return MatchResult(TIER_ALL_MARKER, _finalize(candidates, raw), normalized)
    if len(candidates) == 1:
        return MatchResult(TIER_UNIQUE, _finalize(candidates, raw), normalized)
    return MatchResult(TIER_AMBIGUOUS, _finalize(candidates, raw), normalized)

