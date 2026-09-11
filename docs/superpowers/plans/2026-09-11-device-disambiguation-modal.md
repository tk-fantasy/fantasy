# 设备指令消歧：精确同名直执行 + 模糊多命中弹框选择 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「目标设备不明确」从 LLM 自由发挥改成确定性分层判定 —— 精确同名（含一设备多子实体）直接全执行不问；模糊多命中弹框让用户勾选；说了品类词但设备不存在时不许瞎猜（如实说 + 给候选）。

**Architecture:** 复用现有 `pending_rules` 两段式确认范式（工具返回结构化标记 → 前端 `Dialog.Finish` 后弹框 → REST 落库 → 往 `model_messages` 追加合成消息），**不引入 LangGraph checkpointer/interrupt**（当前 agent 未编 checkpointer，无法中途暂停）。判定逻辑集中在 `text_match.classify_target()`，拦截点在 `tools.py` 的 `call_service` 语义闸门内（执行时拦截 —— 意图已由 LLM 解析完，不会对「客厅有哪些灯」这类提问句误弹框）。

**Tech Stack:** FastAPI + LangGraph（无版本/结构改动）、Vue 3 `<script setup>`、Vitest + @vue/test-utils、pytest + pytest-asyncio。

---

## 执行状态（2026-09-11）

**Task 1-7 已完成**，下方各 Task 的 checkbox 视为已勾选（保留原文作为计划存档）。
**Task 8（虚拟设备手测）待人工执行** —— 需要模拟器 + 浏览器 + 真实 HA 环境。

回归结果：后端 `pytest -q` **3471 passed / 2 failed**，2 个失败是**既有**问题
（`scripts/new-version.py:73` 的 `subprocess.run(text=True)` 缺 `encoding="utf-8"`，
Windows 按 GBK 解中文 `git log` 输出时解码线程死掉 → `r.stdout` 为 None → `.strip()`
抛 AttributeError；已用「本次改动之前的历史 `3f9af47`」复现，证明与消歧改动无关）。
前端 `npm run test` **383 passed / 50 files**，`npm run build` 成功。

### 实施中发现并修掉的问题（计划里没写到的）

1. **复合句必须整句放行**（新增 `_looks_compound`）。写闸门测试时发现「开灯关窗帘」
   被归一化成「灯关窗帘」→ 两轮子串匹配全空 → 误落 `category_miss` 弹框，文案还会
   把归一化中间态念给用户听。判据：「开/关」出现 ≥2 次或含显式连接词。
   只数这两个字是权衡过的——把「调」也算进去会让「空调开到26度」被误判。
   **已知局限**：复合句句内的歧义不受闸门保护，「开灯关窗帘」里的「开灯」仍由模型
   自己挑一盏（旧行为）。逐子句切分是另一件事，本次不做。
2. **品类尾词收窄只作用于 area 兜底轮**。计划原写「候选按 domain 过滤」，实测发现
   name 轮命中的候选不能收窄：「开灯」命中「客厅灯带」，它以「带」结尾不共享尾词
   「灯」，收窄会把它误删（用户说开灯，灯带不该被排除在候选外）。
3. **尾词门槛从「≥2 个实体共享」放宽到 ≥1**，并加兜底：收窄后为空就退回未过滤列表
   （否则「打开客厅」会被过滤成空 → 变成放行，比不过滤更糟）。
4. **exact/all_marker 扩展集为空时必须拒绝**（`test_tools_ops_coverage.py::
   test_semantic_mismatch_rejected_with_candidates` 抓到的真 bug）。「打开加湿器」
   而模型去开 `switch.other` 时，同 domain 扩展集为空，初版直接放行执行了。
   现用 `effective` 变量统一：扩展成功则以扩展集为准（天然不算错配），扩展为空则
   落回错配拒绝。
5. **`drop_selection_drafts(except_query=...)`**：同轮第二次工具调用若判为干净解决，
   会把弹框刚拿到的 `pending_id` 一起抹掉 → 用户点确认只能收到「已过期」。
6. **`confirm_selection` 让 `AppException` 穿透**：否则禁控设备的 403 会被压成
   `exec_failed`/502，丢掉状态码语义。
7. **`toolNames.js` 的 `summarizeToolResult` 需要 need_selection 分支**：否则工具卡片
   会把「一个设备都没动」显示成「已执行」——这是谎报，比不显示更糟。
8. **前端工具短名是 `call_service`**（全名 `ha_devices___call_service`），计划初稿写的
   `ha_call_service` 是错的，已在实施前更正。

### 未提交的文件（按用户要求：改但不提交，避免卷入其在制品）

`app/tools.py`、`app/routes/ha_routes.py`、`app/schema/api_schemas.py`、
`frontend/src/views/ChatView.vue`、`frontend/src/utils/toolNames.js`、
`tests/test_ha_routes.py`、`tests/test_call_service_disambiguation.py`、
`frontend/tests/views/ChatView.test.js`

> 这些文件在本次开工前就已有用户的未提交改动（`app/tools.py` 单独就有 341 行），
> 且实施期间仍在被并行编辑（`integrations/feishu/ws_client.py` 12:16、
> `tests/test_feishu_ws_client.py` 12:35 —— 后者正好落在全量测试那 3 分钟里，
> 造成一次与本次改动无关的误报失败）。整文件 `git add` 会把这些在制品一起卷进来，
> 故留给用户自行拆分提交。

已提交：`text_match.py` / `device_registry.py` / `pending_selections.py` 及各自测试、
`DeviceSelectModal.vue` 及其测试（这些文件开工前都是干净的）。

---

## 背景：现状根因（已核实）

- `match_devices()`（`app/utils/text_match.py:25`）是**双向子串**匹配。用户说「月球的灯」→ 剥「的」→ `月球灯` → 与任何实体名都无子串关系 → **返回空**。
- `app/tools.py` 的 `query→entity 语义校验` 段（约 462-500 行）在 `matched` 为空时**故意放行**（原注释：无法区分「设备不在列表」与「泛指无设备名」，避免误伤「太热了→开空调」）。
- system prompt 永远注入**全量**设备目录（`prompt_service.py` 明确注释「不再按 query 是否匹配设备名来决定注入与否」）。
- 三者叠加 → LLM 拿全量目录自由发挥，把灯全开了。**所以「加消歧弹框」本身修不了月球案例**（匹配为空 → 检测不到歧义 → 不弹框），必须补 `category_miss` 这一层。
- 现有多命中处理：`_rank_by_relevance()`（`text_match.py:165`）只排序不删减，主控 domain 优先、诊断 domain 降权、精确相等优先；最终选哪个完全由 LLM 决定，用户无感知。

## 判定分层（`classify_target` 的核心契约）

按顺序判定，**`exact` 必须先于 `all_marker`**（否则「成都的灯」会被「都」误判为全开）：

| tier | 条件 | 行为 |
|---|---|---|
| `exact` | 归一化 query == 设备名 / 实体名(含别名) / `label`；或实体名 = query + **分隔符** + 子功能名（MIoT「A灯 会客厅灯 左键」） | 命中集全部执行，不问 |
| `all_marker` | query 含 所有 / 全部 / 都 / 整个 / 全 | 命中集全部执行，不问 |
| `unique` | 候选恰好 1 个 | 直接执行（现状） |
| `ambiguous` | 候选 ≥2 且展示名不同 | `need_selection` → 弹框 / 口头列举 |
| `category_miss` | 候选为空，但 query 与同 domain 实体名有公共**品类尾词**（≥1 字、≥2 个实体共享，如「灯」） | `need_selection`，文案明说「没找到『月球灯』」 |
| `none` | 候选为空且无品类尾词 | 放行（保住「太热了→开空调」「把飞利浦那盏打开」） |

**品类尾词**是本方案唯一的新型机制，一份实现两处用：
1. `category_miss` 判定（「月球灯」→ 尾词「灯」→ 三盏灯作候选）；
2. **候选 domain 过滤**（见下）。

### domain 过滤：依据 query 的品类尾词，不是 LLM 选的 domain

实测「把客厅的灯关了」：归一化 `客厅灯` 与 `客厅吊灯` **无连续子串关系**（客/厅/吊/灯）→ name 轮为空 → 落 `match_devices` 的 area 兜底轮 → `客厅` 是 `客厅灯` 的子串 → **客厅所有设备全进候选**（吊灯、风扇、窗帘、插座、温湿度 sensor）。

若用「LLM 所选实体的 domain」过滤候选，会有漏洞：LLM 选错 domain（说灯却去开风扇）时，过滤反而把错误**洗白**成 `unique` 放行。故：

> domain 过滤依据 **query 的品类尾词**（`灯` → 命中 床头灯/厨房灯/客厅吊灯 → domain=light）；query 里找不到品类尾词时，才退回用 LLM 所选实体的 domain。

效果：「把客厅的灯关了」→ 候选 = area 命中 ∩ light = 客厅吊灯 → `unique` → 直接执行；LLM 若选了风扇 → target ∉ 候选 → 按现有闸门拒绝。

## Global Constraints

- **`match_devices()` 对外行为不得变化**：`tests/test_text_match.py` 现有用例一条不改全绿。剥离逻辑抽成 `_normalize_query()` 后由两者共用，避免口径漂移。
- **`need_selection` 返回体绝对不能含 `"error"` 键**：`app/mcp/langchain_tools.py:68-77` 见到 `error` 键就加 `Error:` 前缀 → `app/agents/langgraph_agent.py:321` 判 `is_error` → `app/agents/dispatcher.py:155-182` 塞进 `state.failed_tools` 触发**失败重试回路**，模型会被逼着「修正」自己再猜一个实体。必须是 success 形状（`is_error=False`），这也与 `pending_confirm` 同构 —— 前端 `capturePendingRule` 同样要求 `payload.success`。
- **`category_miss` 只用于语义闸门，不得用于存在性反查**（`app/tools.py` 约 425 行那条 `entity_id 不存在 → 反查候选` 的自愈回路）。否则 `tests/test_device_registry.py::test_call_service_rejection_no_match_hint`（断言「打开火星基地的灯」无 `candidates` 且 hint 为「没有匹配到任何真实设备」）会挂，且会诱导模型从候选里挑一个 —— 正是要修的瞎猜行为。存在性反查继续用裸 `match_devices`。
- **热路径不得调用 `build_device_snapshot()`**（`app/services/device_registry.py:62`）：它每次跑 `resolve_controls` + `flip_state_value` + 3 个 DB scope，太重。只用 Task 2 新增的轻量索引。
- **闸门任何异常一律放行**（沿用现有 `except → logger.warning("call_service: 语义校验失败，放行")` 口径）。这是 `tests/test_call_service_operable.py` / `test_call_service_semantic_map.py` 能继续绿的前提 —— 那些 fixture 只 mock 了 `get_all_devices`，没有 `get_all_devices_grouped`。
- **候选必须过滤 `entity_operable` 黑名单**：禁控设备对 AI 不可见，不得出现在弹框或 `candidates` 里（沿用现有过滤逻辑）。
- **`exact` 扩展只在同 domain 内**：「开大门」若同时命中 `switch.大门` 和 `lock.大门`，只扩展与 LLM 所选实体同 domain 的那些 —— 避免「开大门」顺手把门锁开了。
- **弹框必须延后到 `Dialog.Finish` 才开**：dispatcher 轮末才 append `model_messages`，轮中确认会导致历史乱序（`app/routes/rule_routes.py:174-177`、`ChatView.vue` 的 `openPendingRuleModal` 注释已记录此硬约束）。
- **前端工具短名是 `call_service`**，不是 `ha_call_service`：全名 `ha_devices___call_service`，`shortToolName()`（`frontend/src/utils/toolNames.js`）按 `___` 切分取后半。
- **新 REST 端点必须带鉴权**：`Depends(get_current_user)` + `require_owned_session`（`app/core/auth.py:335`）。**不得复用 `POST /api/ha/call_service`**（`app/routes/ha_routes.py:326`）—— 该端点没有鉴权依赖、也绕过 `entity_operable` 黑名单（既有问题，本计划不修，但不能继承）。
- 注释中文、说明约束原因；文案全中文；提交信息用仓库既有风格（`feat:` / `fix:` / `refactor:` / `test:` + 中文摘要）。
- Python 用 conda 环境的解释器跑测试（不在系统 PATH）。

---

### Task 1: 匹配分层 `classify_target`（TDD）

**Files:**
- Modify: `app/utils/text_match.py`
- Test: `tests/test_text_match.py`

**Interfaces:**
- Produces: `_normalize_query(query: str) -> str`（从 `match_devices` 抽出的剥离循环，行为等价）；`MatchResult` dataclass（字段 `tier: str` / `candidates: list[dict]` / `normalized: str`）；`classify_target(query: str, entries: list[dict], *, domain: str | None = None) -> MatchResult`。
- Consumes: 现有 `_rank_by_relevance()`、`_PRIMARY_DOMAINS`、`_DIAGNOSTIC_DOMAINS`。
- `entries` 元素口径 = Task 2 `build_match_index()` 的产出（`entity_id` / `name` / `domain` / `device_name` / `label` / `area_name` / `state`）；为兼容现有测试，`label` 缺失时退回 `name`。

- [ ] **Step 1: 先写分层测试**

在 `tests/test_text_match.py` 末尾追加 `TestClassifyTarget`，复用文件顶部现有的 `DEVICES` fixture，并补一个多子实体设备（口径照抄 `tests/test_device_registry.py:37-44` 的 `A_LAMP_SUBS`）：

```python
# 一设备多可控子实体（真实 MIoT 命名形态）：设备名「A灯」，5 个 switch 子实体
A_LAMP_ENTRIES = [
    {"entity_id": "switch.a_bk_onoff",   "name": "A灯 总开关",        "domain": "switch",
     "device_name": "A灯", "label": "A灯 总开关",        "area_name": "公司", "state": "off"},
    {"entity_id": "switch.a_first_key",  "name": "A灯 第一键",        "domain": "switch",
     "device_name": "A灯", "label": "A灯 第一键",        "area_name": "公司", "state": "off"},
    {"entity_id": "switch.a_on_p2",      "name": "A灯 会客厅灯 左键", "domain": "switch",
     "device_name": "A灯", "label": "A灯 会客厅灯 左键", "area_name": "公司", "state": "off"},
    {"entity_id": "switch.a_on_p3",      "name": "A灯 会客厅灯 右键", "domain": "switch",
     "device_name": "A灯", "label": "A灯 会客厅灯 右键", "area_name": "公司", "state": "off"},
    {"entity_id": "switch.a_second_key", "name": "A灯 第二键",        "domain": "switch",
     "device_name": "A灯", "label": "A灯 第二键",        "area_name": "公司", "state": "off"},
]
```

必须覆盖的断言（每条都要写）：

| 用例 | 期望 |
|---|---|
| `classify_target("开A灯", A_LAMP_ENTRIES)` | `tier == "exact"`，candidates = 全部 5 个（设备级精确 → 整组） |
| `classify_target("开启A灯 会客厅灯 左键", A_LAMP_ENTRIES)` | `tier == "exact"`，candidates 只有 `switch.a_on_p2` |
| `classify_target("开客厅灯", DEVICES)` | **不得** `exact`（`客厅灯` 与 `客厅吊灯` 无分隔符边界，不能互为精确同名） |
| `classify_target("开B灯", 两个 name 均为「B灯」的 light)` | `tier == "exact"`，candidates = 2 |
| `classify_target("开灯", DEVICES)` | `tier == "ambiguous"`，candidates = 4 盏灯 |
| `classify_target("把所有灯关掉", DEVICES)` | `tier == "all_marker"`，candidates = 4 盏灯 |
| `classify_target("成都的灯都关了", 含设备名「成都灯」)` | `tier == "exact"`（exact 先于 all_marker，「都」不得触发全开） |
| `classify_target("月球的灯", DEVICES)` | `tier == "category_miss"`，candidates = 4 盏灯（尾词「灯」） |
| `classify_target("把飞利浦那盏打开", DEVICES)` | `tier == "none"`，candidates == []（现有 `test_alias_yields_empty` 同口径） |
| `classify_target("太热了", DEVICES)` | `tier == "none"`（无品类尾词 → 放行，保住「太热了→开空调」） |
| `classify_target("开客厅吊灯", DEVICES)` | `tier == "unique"` |
| `classify_target("把客厅的灯关了", 客厅含 light+fan+cover+switch)` | `tier == "unique"`，candidates 只有那盏灯（品类尾词「灯」→ domain=light 过滤，风扇/窗帘/插座不得进候选） |
| `classify_target("开灯", DEVICES)` 且 `domain="light"` | 与不传 domain 结果一致（显式 domain 与尾词推断一致时无冲突） |
| 候选 > 12 个 | `len(candidates) == 12`（上限截断） |

- [ ] **Step 2: 抽出 `_normalize_query`，确认零回归**

把 `match_devices()` 里的整个剥离循环（`action_words` / `particles` / `request_prefixes` / `param_words` / `enum_words` / `link_words` / `num_value_re` / 剥「的」/ 每轮 `strip()`）原样搬进 `_normalize_query(query) -> str`，`match_devices` 改为调用它，`candidates_q` 构造与两轮 `_match_by` 逻辑保持不变。

Run: `conda run python -m pytest tests/test_text_match.py -q`
Expected: 现有全部用例 PASS（Step 1 新增的会失败，属预期）。

- [ ] **Step 3: 实现 `classify_target`**

要点：
- 品类尾词提取：对归一化 query 与每个 entry 的 `name`/`label` 求**最长公共后缀**；取全体最大值，要求长度 ≥1 且**被 ≥2 个 entry 共享**才算成立（单个实体共享不构成「品类」）。尾词对应的 domain 集合即过滤依据。
- 分隔符边界（设备级 exact 的子实体识别）：`name.startswith(nq)` 且紧随其后的字符 ∈ `{" ", "-", "_", "·", "—", "（", "("}` 才算同一设备的子功能。这条是「`客厅灯` 不得吞掉 `客厅灯带`」的唯一防线，必须有专门用例。
- 候选排序复用 `_rank_by_relevance()`；上限 12（超出截断，调用方自行提示「还有 N 个」）。
- `exact` 命中集不做 domain 收窄（domain 收窄在 Task 4 扩展 `entity_id` 时做，见 Global Constraints）。

Run: `conda run python -m pytest tests/test_text_match.py -q`
Expected: 全部 PASS。

- [ ] **Step 4: 提交**

```bash
git add app/utils/text_match.py tests/test_text_match.py
git commit -m "feat(match): 新增 classify_target 分层判定——精确同名/全量词/歧义/品类缺失"
```

---

### Task 2: 轻量匹配索引 `build_match_index`

**Files:**
- Modify: `app/services/device_registry.py`
- Test: `tests/test_device_registry.py`

**Interfaces:**
- Produces: `async def build_match_index(ha_service: Any) -> list[dict]`，元素 `{entity_id, name, domain, device_id, device_name, label, area_name, state}`。
- Consumes: `ha_service.get_all_devices_grouped()`（优先）、`ha_service.get_all_devices()`（回退）、本模块已有的 `derive_sub_name()`（`label` 拼装口径与 `entry_label()` 一致）、`DIAGNOSTIC_DOMAINS`。

- [ ] **Step 1: 先写测试**

在 `tests/test_device_registry.py` 追加 `TestBuildMatchIndex`，复用文件内已有的 `_make_ha_service()`（它同时 mock 了 flat 与 grouped）与 `A_LAMP_SUBS`：

- 索引不含 `sensor.*` / `binary_sensor.*`（`DIAGNOSTIC_DOMAINS` 被排除）；
- 多可控实体设备：`device_name == "A灯"`、`label == "A灯 会客厅灯 左键"`（与 `entry_label()` 完全一致）、`sub_name` 语义由 `derive_sub_name()` 保证；
- 单可控实体设备：`label == name`（不带 MIoT 子名噪声）；
- **grouped 不可用时回退 flat**：构造一个只有 `get_all_devices` 的 mock（`get_all_devices_grouped` 抛异常），断言仍返回非空索引且 `device_name` 为空/等于 name；
- **flat 条目缺 `name` 键时从 `attributes.friendly_name` 取名**（`tests/test_call_service_operable.py::_build_deps` 的 fixture 就是这种形态，闸门要靠这条不炸）。

- [ ] **Step 2: 实现**

- 优先 `get_all_devices_grouped()`：遍历 devices → 遍历 entities → 排除 `DIAGNOSTIC_DOMAINS` → 多可控实体设备才拼 `sub_name`（口径照抄 `build_device_snapshot` 里的 `multi = len(controllable) > 1`）。
- `except Exception` 或 grouped 为空 → 回退 `get_all_devices()`，`device_name` 取 `name`、`label` 取 `name`。
- 不做黑名单过滤（调用方 Task 4 已有 `entity_operable` 过滤逻辑，避免两处口径）。
- 不读 `entity_note`、不调 `resolve_controls`、不调 `flip_state_value`（这是它比 `build_device_snapshot` 轻的全部原因，写进 docstring）。

Run: `conda run python -m pytest tests/test_device_registry.py -q`
Expected: 全部 PASS（含现有 `TestCandidateLookup` 等用例）。

- [ ] **Step 3: 提交**

```bash
git add app/services/device_registry.py tests/test_device_registry.py
git commit -m "feat(registry): 新增轻量 build_match_index——闸门消歧用，不含 controls/flip_state"
```

---

### Task 3: 待选草稿存取 `pending_selections`

**Files:**
- New: `app/services/pending_selections.py`
- Test: `tests/test_pending_selections.py`

**Interfaces:**
- Produces: `KIND_DEVICE_SELECTION = "device_selection"`；`create_selection_draft(session, payload) -> str`；`confirm_selection(session, pending_id, selected_ids, executor) -> dict`；`cancel_selection(session, pending_id) -> bool`。
- Consumes: `pending_rules.pending_store()` / `locate_pending()` / `PENDING_TTL_SECONDS`（复用，不新造暂存区）。
- **`SessionState.pending_confirmations`（`app/services/session_store.py:42`）已是 kind 泛化字典，无需改 schema、无需改序列化。**

- [ ] **Step 1: 先写测试**

`tests/test_pending_selections.py`（fixture 照抄 `tests/test_pending_selections.py` 同目录既有测试的 DB 单例隔离写法，见 `tests/test_device_registry.py:20-35`）：

- 建草稿 → `locate_pending(session, pid, KIND_DEVICE_SELECTION)` 能取回，`kind` / `query` / `domain` / `service` / `data` / `candidates` 完整；
- TTL 过期（monkeypatch `created_at` 到 `PENDING_TTL_SECONDS + 1` 之前）→ `confirm_selection` 返回 `{"ok": False, "reason": "not_found"}`；
- **选中项不在候选内 → 拒绝**，返回 `{"ok": False, "reason": "invalid_selection"}`，且草稿**不被摘除**（用户可重选）；
- `selected_ids` 为空 → 同上拒绝；
- confirm 成功 → `executor` 被调用一次、参数是逗号拼接的 `entity_id`、草稿被 pop；
- `executor` 抛异常 → 返回 `{"ok": False, "reason": "exec_failed"}`，草稿**保留**（与 `confirm_pending` 落库失败不摘草稿同口径）；
- `cancel_selection` → 草稿消失，返回 True；重复 cancel 返回 False。

- [ ] **Step 2: 实现**

草稿结构：

```python
{
  "kind": KIND_DEVICE_SELECTION,
  "created_at": time.time(),
  "query": query,               # 用户原话，用于弹框标题与合成消息
  "domain": domain, "service": service, "data": data,   # LLM 已解析好的动作
  "candidates": [{"entity_id", "label", "domain", "area_name", "state"}],
  "reason": "ambiguous" | "category_miss",   # 决定弹框文案（后者要说「没找到 X」）
}
```

模块 docstring 要写清与 `pending_rules.py` 的分工：本模块只管草稿生命周期，**不构造任何渠道特有的错误结构**（工具要 hint、REST 要 message，各自映射）。

Run: `conda run python -m pytest tests/test_pending_selections.py -q`
Expected: 全部 PASS。

- [ ] **Step 3: 提交**

```bash
git add app/services/pending_selections.py tests/test_pending_selections.py
git commit -m "feat(pending): 新增 device_selection 待选草稿存取，复用 pending_confirmations 暂存区"
```

---

### Task 4: `call_service` 语义闸门改造（核心）

**Files:**
- Modify: `app/tools.py`（替换 `query→entity 语义校验` 段，约 462-500 行）
- Test: New `tests/test_call_service_disambiguation.py`

**Interfaces:**
- Consumes: Task 1 `classify_target`、Task 2 `build_match_index`、Task 3 `create_selection_draft` / `cancel_selection`。
- Produces: 闸门在 `ambiguous` / `category_miss` 时返回
  ```python
  {"success": False, "status": "need_selection", "pending_id": ...,
   "candidates": [{"entity_id", "label", "domain", "area_name", "state"}],
   "reason": "ambiguous" | "category_miss", "query": ..., "hint": ...}
  ```
  —— **无 `"error"` 键**（Global Constraints）。

- [ ] **Step 1: 先写测试**

新建 `tests/test_call_service_disambiguation.py`，harness 照抄 `tests/test_call_service_semantic_map.py:19-35`（`ToolDeps` + `_register_ha_call_service(deps)` + `mgr.get_tool("ha_devices___call_service")`），设备 fixture 照抄 `tests/test_device_registry.py::_make_ha_service` + `A_LAMP_SUBS`：

- **ambiguous**：3 盏不同名灯 + `current_query = "开灯"` + LLM 只选了 1 盏 → `result["status"] == "need_selection"`；**断言 `"error" not in result`**；断言 `call_with_probe` 未被调用（`patch` 成 `AsyncMock` 后 `assert_not_awaited`）；断言 candidates 是 3 盏灯的 label。
- **need_selection 不得被判为工具失败**：把上一步的返回体喂给 `app.mcp.langchain_tools.mcp_to_langchain_tool` 的 `_coroutine`，断言输出**不以 `Error:` 开头**（这条是防失败重试回路的哨兵测试，必须写）。
- **exact 扩展**：`A_LAMP_SUBS` + `current_query = "开A灯"` + LLM 只选 `switch.a_bk_onoff` → 实际下发的 `entity_id` 是**全部 5 个**逗号拼接（`fake_call` 捕获第 4 个参数断言）。
- **跨 domain 不扩展**：设备「大门」含 `switch.da_men` + `lock.da_men`，`current_query = "开大门"`，LLM 选 `switch.da_men` → 下发**只有** `switch.da_men`，`lock.da_men` 不得被带上。
- **all_marker**：`current_query = "把所有灯关掉"` + LLM 只选 1 盏 → 下发全部 3 盏。
- **unique**：`current_query = "开客厅吊灯"` → 原样执行，`status` 不在返回体里。
- **category_miss**：`current_query = "打开火星基地的灯"` + LLM 选了真实存在的灯 → `status == "need_selection"`、`reason == "category_miss"`、candidates 为同品类灯。
- **none 放行**：`current_query = "太热了"` + LLM 选空调 → 正常执行。
- **黑名单不进候选**：`emoji_pref_upsert("entity_operable", <某灯>, "0")` → candidates 里没有它。
- **闸门异常放行**：`build_match_index` 打桩抛异常 → 仍正常执行 + `logger.warning`（沿用现有口径）。
- **遗留草稿清理**：会话里先塞一个 `device_selection` 草稿，再跑一次 `unique` 指令 → 草稿被 pop（语音渠道用户直接说「客厅吊灯」走的就是这条路）。

- [ ] **Step 2: 改造闸门**

把现有 `query→entity 语义校验` 段替换为分层分派，保留其外层 `try/except → 放行` 与黑名单过滤：

```python
index = await build_match_index(deps.ha_service)
index = [e for e in index if e.get("entity_id") not in _disabled]   # 沿用现有黑名单过滤
res = classify_target(query, index, domain=None)   # domain 由品类尾词推断，见 Task 1
```

- `exact` / `all_marker`：把 `eid_list` 扩展为「候选中与 LLM 所选实体**同 domain** 的全部 entity_id」，`logger.info("call_service 消歧扩展: %s → %s", ...)`，然后继续走原有链路（存在性校验、黑名单、语义映射、`call_with_probe`、回读核实、`_verify_readback` **全部不动**）。
- `unique`：维持现状语义（target 不在候选内才拒）。
- `ambiguous` / `category_miss`：`create_selection_draft(...)` → 返回上一步的 `need_selection` 结构。`hint` 必须明确写：「用户的指令「{query}」匹配到 N 个设备，已请用户选择；请口头列举候选并等待用户回答，**不要自己猜、不要重试、不要声称已执行**」。
- `none`：放行（现状）。
- 干净解决时（`exact`/`unique`/`all_marker`）顺手 `cancel_selection()` 清掉会话内遗留草稿。

⚠️ 扩展 `eid_list` 后，下游用到 `eid_list` 的三处（语义映射共识判断、`_readback_entity_state`、`_verify_readback`）都要拿扩展后的列表 —— 语义映射的「全部实体共识才替换」在扩展后仍成立（同设备子实体通常同映射），但要在测试里覆盖到。

- [ ] **Step 3: 全量后端回归**

Run: `conda run python -m pytest tests/test_call_service_disambiguation.py tests/test_call_service_semantic_map.py tests/test_call_service_operable.py tests/test_device_registry.py tests/test_text_match.py -q`
Expected: 全部 PASS。后三个文件**一条用例都不许改** —— 它们绿着才证明「异常放行」「flat 回退」「category_miss 不进存在性反查」这三条约束真的守住了。

- [ ] **Step 4: 提交**

```bash
git add app/tools.py tests/test_call_service_disambiguation.py
git commit -m "feat(tools): call_service 语义闸门改为分层消歧——精确同名扩展、模糊多命中转用户选择"
```

---

### Task 5: REST 选择端点

**Files:**
- Modify: `app/routes/ha_routes.py`、`app/schema/api_schemas.py`
- Test: Modify `tests/test_ha_routes.py`

**Interfaces:**
- `POST /api/ha/pending/{pending_id}/select`，body `PendingSelectRequest {session_id: str, entity_ids: list[str]}`
- `POST /api/ha/pending/{pending_id}/cancel`，body `PendingConfirmRequest {session_id}`（复用 `api_schemas.py:503-505` 现有模型）
- 结构与顺序约束注释照抄 `app/routes/rule_routes.py:245-281`。

- [ ] **Step 1: 先写测试**

在 `tests/test_ha_routes.py` 追加：未登录 401；会话不属于当前用户 403（`require_owned_session`）；草稿不存在/过期 404（`code="pending_selection_not_found"`）；`entity_ids` 不在候选内 400；成功后 `session.model_messages` 追加了合成消息且 `store_session` 被调用；`cancel` 幂等。

- [ ] **Step 2: 实现**

- `PendingSelectRequest` 加到 `app/schema/api_schemas.py`（紧邻 `PendingConfirmRequest`）。
- 端点必须 `Depends(get_current_user)` + `require_owned_session(container, session_id, current_user)`。
- **执行走与工具同源的路径**，不复用 `POST /api/ha/call_service`：`call_with_probe` + `entity_operable` 黑名单校验 + `record_device_op(..., source="AI")` + `container.ha_service.invalidate_states_cache()`。把这段抽成 `ha_routes` 内的私有 helper，供 select 端点调用。
- 成功后追加合成消息（口径照抄 `rule_routes.py:270-273`）：
  ```python
  session.model_messages.append(
      {"role": "user", "content": f"（我已通过界面选择：{names}，指令已执行）"})
  await container.session_store.store_session(session)
  ```
- 失败按 reason 映射状态码（照抄 `rule_routes.py` 的 `{"invalid_selection": 400, "exec_failed": 502}` + 默认 404）。

Run: `conda run python -m pytest tests/test_ha_routes.py -q`
Expected: 全部 PASS。

- [ ] **Step 3: 提交**

```bash
git add app/routes/ha_routes.py app/schema/api_schemas.py tests/test_ha_routes.py
git commit -m "feat(api): 新增设备消歧待选确认端点 /api/ha/pending/{id}/select|cancel（带鉴权与会话归属校验）"
```

---

### Task 6: 选择弹框组件（TDD）

**Files:**
- New: `frontend/src/components/DeviceSelectModal.vue`
- Test: New `frontend/tests/components/DeviceSelectModal.test.js`

**Interfaces:**
- Consumes: `AdvancedModal.vue`（外壳，props `{title}`、emit `close`）；`apiPost`（`frontend/src/utils/api.js`）。
- Produces: props `{pendingId, sessionId, query, candidates, serviceLabel, reason}`；emits `['confirmed','cancelled','expired','close']`。REST 调用与 404→`expired` 的处理照抄 `ReviseChatModal.vue` 的 `confirmPending()`（约 209-215 行）。

- [ ] **Step 1: 先写测试**

`frontend/tests/components/DeviceSelectModal.test.js`（写法照抄 `frontend/tests/components/ReviseChatModal.test.js`）：

- 渲染全部 candidates（label + 区域 + 当前状态）；
- 默认预勾「状态会真的改变」的项：`serviceLabel` 含开启 → 预勾 `state` 为 off 的；含关闭 → 预勾 on 的；
- 全选 / 清空按钮；
- 点确认 → `apiPost` 打到 `/api/ha/pending/{id}/select`，body `{session_id, entity_ids}` **只含勾选项**；成功后 emit `confirmed`；
- 一个都没勾 → 确认按钮 disabled；
- `apiPost` 抛 404 → emit `expired`；
- 点取消 → `apiPost` 打到 `/cancel` + emit `cancelled`；
- `reason === 'category_miss'` → 标题/正文出现「没找到「{query}」」字样。

Run: `cd frontend && npm run test -- DeviceSelectModal`
Expected: 先红。

- [ ] **Step 2: 实现组件**

- 复选列表用原生 `input type="checkbox"`（仓库现有 checkbox 都是原生 + 自定义样式，见 `AdvancedView.vue:1018`，不引第三方组件库）。
- 样式全部走 CSS 变量（`var(--color-border)` / `var(--space-*)` / `var(--text-*)`），**明暗双主题都要看得过去**（用户会切主题）。
- 文案中文。

Run: `cd frontend && npm run test -- DeviceSelectModal`
Expected: 全绿。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/DeviceSelectModal.vue frontend/tests/components/DeviceSelectModal.test.js
git commit -m "feat(ui): 新增设备消歧选择弹框 DeviceSelectModal"
```

---

### Task 7: ChatView 接线

**Files:**
- Modify: `frontend/src/views/ChatView.vue`
- Test: Modify `frontend/tests/views/ChatView.test.js`（若该文件不存在则新建，写法参照 `frontend/tests/components/` 下既有用例）

**Interfaces:**
- Consumes: Task 5 端点、Task 6 组件、`shortToolName()` / `parseToolResult()`（`frontend/src/utils/toolNames.js`）。

- [ ] **Step 1: 捕获标记**

新增 `capturePendingSelection(payload)`，在 `case 'Template.CallToolResult'`（约 221-231 行）里紧挨 `capturePendingRule(payload)` 调用：

```js
function capturePendingSelection(payload) {
  if (!payload?.success) return                        // need_selection 是 success 形状
  if (shortToolName(payload.tool_name) !== 'call_service') return   // 短名是 call_service
  const data = parseToolResult(payload.tool_response)  // result 是 JSON 字符串
  if (data?.status !== 'need_selection' || !data.pending_id) return
  pendingSelection.value = { pendingId: data.pending_id, query: data.query,
                             candidates: data.candidates || [], reason: data.reason }
}
```

- [ ] **Step 2: 延后弹框**

在 `case 'Dialog.Finish'`（约 263-271 行）里 `openPendingRuleModal()` 之后调 `openPendingSelectionModal()`，并把 `openPendingRuleModal` 上方那段「轮末才弹是硬约束」的注释一并复制到新函数上（约束同源，别只留一处）。

- [ ] **Step 3: handler + 模板挂载**

- `onSelectionConfirmed(executed)` / `onSelectionCancelled()` / `onSelectionExpired(msg)` / `onSelectionModalClose()` 四个 handler，各推一条 `role: 'system'` 消息并 `scrollToBottom()`（口径照抄 `onRuleConfirmed` 等，约 308-345 行）。
- `onSelectionModalClose` 只关弹框、不动后端草稿（TTL 内用户仍可在聊天里直接说设备名走口头路径）—— 与 `onRuleModalClose` 同口径。
- 模板挂在 `ReviseChatModal` 旁边（约 988-999 行），`v-if="showDeviceSelect && pendingSelection"`。
- 清会话 / 切会话处（约 501-504 行，`pendingRuleDraft.value = null` 旁边）一并重置 `pendingSelection`。

- [ ] **Step 4: 测试 + 提交**

补 ChatView 用例：收到 `need_selection` 的 `CallToolResult` 时**不立即**弹框，收到 `Dialog.Finish` 后才弹（这条是硬约束的哨兵测试）。

```bash
cd frontend && npm run test && npm run build
git add frontend/src/views/ChatView.vue frontend/tests
git commit -m "feat(ui): 聊天页接入设备消歧弹框——轮末触发，与待确认规则弹窗同源约束"
```

---

### Task 8: 全量回归 + 虚拟设备手测

**⚠️ 安全前置（必须按顺序做完再开始手测）**

真实设备与虚拟设备**共存于同一个 HA**，「开灯」会同时命中真灯和虚拟灯。用户单位是真灯，误操作后果实际发生。故：

1. `GET /api/simulator/status` → 确认 `running: true`。模拟器关着时 `ha_service._virtual_suppress_set()` 会把白名单虚拟实体全部隐藏，AI 眼里只剩真灯 —— 这时任何测试都会直接打到真实设备。
2. 设备页确认 3 盏虚拟灯（床头灯 / 厨房灯 / 客厅吊灯）可见。
3. **把真实灯全部标为「禁止 AI 操作」**（设备页开关 → `entity_operable`）。黑名单实体对 AI 完全不可见、进不了候选，比「弹框里小心别勾错」可靠得多。
4. 手测结束后**逐条解除**第 3 步的黑名单。

**虚拟设备清单**（`config.json:196-210` 白名单 + `ha_config/mqtt/lights.yaml`）：

| 类型 | 名称 | entity_id |
|---|---|---|
| 灯 | 床头灯 / 厨房灯 / 客厅吊灯 | `light.chuang_tou_deng` / `light.chu_fang_deng` / `light.ke_ting_diao_deng` |
| 空调 | 中央空调 | `climate.zhong_yang_kong_diao` |
| 窗帘 | 客厅窗帘 | `cover.ke_ting_chuang_lian` |
| 风扇 | 客厅风扇 | `fan.ke_ting_feng_shan` |
| 加湿器 | 卧室加湿器 | `humidifier.wo_shi_jia_shi_qi` |
| 插座 | 厨房/客厅/卧室智能插座 | `switch.chu_fang_zhi_neng_cha_zuo` 等 3 个 |
| 传感器 | 客厅温度 / 客厅湿度 | `sensor.ke_ting_wen_du` / `sensor.ke_ting_shi_du` |

**模拟器覆盖不到的用例 → 用别名造**

`ha_config/mqtt/lights.yaml` 里每盏灯都没有 `device:` 块 → HA 里各自是独立单实体设备 → `get_all_devices_grouped()` 生成 `virtual:` 组、**每组只有 1 个可控实体**。设备级 exact（一名多子实体）在虚拟设备上天然不存在，用 `PUT /api/ha/entity-aliases` 造：

- [ ] **Step 1: 造同 domain 同名（测 exact 扩展）**

把 `light.chu_fang_deng` 和 `light.ke_ting_diao_deng` 的别名都设为「B灯」。
注意：别名会**同时写 HA `entity_registry.name`**（`ha_routes.py:82-115`，HA 写入失败会回滚 Aether 侧）。

- [ ] **Step 2: 造跨 domain 同名（测不扩展这条安全规则）**

把 `switch.chu_fang_zhi_neng_cha_zuo` 的别名也设为「B灯」。期望：「开B灯」只动两盏灯，**插座不动**。

- [ ] **Step 3: 跑手测清单**

| 指令 | 期望 |
|---|---|
| 开灯 | 弹框列 3 盏虚拟灯（预勾当前 off 的）→ 勾 2 盏 → 只有这 2 盏亮 |
| 开启厨房灯 | 不弹框，唯一命中直接开 |
| 月球的灯 | 弹框 + 文案「没找到『月球灯』」，**不是**默默全开（本次核心回归点） |
| 把所有灯关掉 | 不弹框，3 盏全关 |
| 开B灯 | 不弹框，两盏同名灯一起动作，**插座不动** |
| 把客厅的灯关了 | 不弹框，只有客厅吊灯关，风扇/窗帘/插座不动 |
| 太热了 | 无回归，仍推断中央空调 |
| 客厅有哪些灯 | 不弹框（提问不触发 `call_service`） |
| 弹框期间不点，改在聊天里说「客厅吊灯」 | 正常执行，且遗留草稿被清 |

- [ ] **Step 4: 语音 / 飞书渠道**

无界面渠道说「开灯」→ 期望收到口头候选列举（「要开哪个？床头灯、厨房灯还是客厅吊灯？」）而非默默执行 → 回答「厨房灯」→ 正确执行。网页弹框与口头确认共用同一份草稿（`pending_rules` 的双入口设计）。

- [ ] **Step 5: 还原环境**

- 三个别名全部置空串还原（`PUT /api/ha/entity-aliases`，`alias: ""` → 同时清 HA 自定义名）；
- 解除 Step 3 前置里给真实灯加的 `entity_operable` 黑名单；
- 设备页确认真实灯恢复可控、虚拟灯名字复原。

- [ ] **Step 6: 全量测试 + 构建**

```bash
conda run python -m pytest -q
cd frontend && npm run test && npm run build
```
Expected: 后端全绿（本仓库基线约 99.8% 覆盖，不得下降）、前端全绿、构建成功。

---

## 明确不做

- 不引入 LangGraph checkpointer / `interrupt`（改动面太大，现有 pending 范式够用）。
- 不动 system prompt 的全量目录注入策略（`prompt_service.py` 里的「按 query 做 top-k 裁剪」是既有 TODO，与本次正交）。
- 不做跨会话「记住上次选择」（同会话内的重复弹框靠 `all_marker` 与精确命名缓解）。
- 不修 `POST /api/ha/call_service` 缺鉴权 + 绕过黑名单这个**既有问题**（单独提 issue，避免本次改动面扩散）。

## 已知代价

- 模糊指令多一次 LLM 往返（约 1-3s）才弹框 —— 这是「执行时拦截」的固有成本，换来的是不会对提问句误弹框。
- 判定索引基于 5s states 缓存 + 60s registry 缓存，弹框候选极端情况下可能滞后（与现有 candidates 反查同口径）。
- 别名只作用在**实体层**：虚拟设备组的 `device_name` 取 HA 原始 friendly_name（`ha_service.py:296-302`）不吃别名 → 设备级 exact 仍按原名匹配。Task 8 Step 1-2 的别名用例测的是**实体级** exact，这是符合预期的行为，不是 bug。
