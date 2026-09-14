# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### 对话工具瘦身——删 http_request/verify_action/scene_create，联网工具改开关控制
- **为什么**：22 个工具对弱模型（glm-4-flash/agnes-3.0-flash 实测）选择精度压力过大——查询自动化规则时反复绕道 `http_request` 直连 HA 内网（被 net_guard 拦截）或误查定时任务列表，通用工具的宽泛描述会"吸走"意图
- **删 http_request**：通用 HTTP 客户端对对话模型无不可替代场景（查天气/设备/规则都有专用工具），且是内网安全面；net_guard 防线与其相关测试一并清理
- **verify_action 沉淀为代码层**：控制正确性由 call_service 的「每控必核」回读 + validator 的断言核查在代码层保证，不再依赖模型"记得"手动复核；工具与 handler 删除
- **删 scene_create**：对话建场景使用率低且与设备控制意图混淆（"开灯"被误路由成建场景）；场景创建收敛到 REST /api/scenes 与规则页 UI
- **联网工具改开关**：web_search/fetch_webpage 常驻 manager，`web_search.enabled`（默认关）在 agent 重建时决定是否暴露——高级设置「网页搜索（Exa）」面板新增「启用联网工具」开关，保存即时重建 agent 生效（免重启）；未传 enabled 的保存不覆盖已有配置
- 工具数 22 → 17（联网关）/ 19（联网开）


#### 幻觉设备自动匹配 + 模型信息面修正（"开大门"死局根治）
- **问题**：用户说「有人就开大门」，glm-4-flash 幻觉出 `cover.front_door`（模拟器只有 media_player/light/switch，根本没有门类设备），`build_rule` 校验重试 3 轮耗尽后仍带病返回——草稿照常生成、弹窗照常弹出，confirm 实体校验每次 400，用户点两次都被拒，规则永远建不成
- **确定性自动匹配**：重试耗尽后 `rule_service._auto_repair_actions` 代码层兜底——按动作描述 → summary/name 依次作 query 调 `match_devices`（主控 domain 优先，sensor/binary_sensor 排除），取第一替换，同步修正 domain/service（open→turn_on、close→turn_off；真有 cover 设备则保留 open_cover），幻觉设备的 data 重置；替换明细挂 `rule.auto_corrections`
- **零匹配拦截**：修不好的挂 `validation_errors`，`automation_rule_create` 工具与 `POST /api/task/rule` 都不出草稿——tool_error 附主控设备候选让模型如实告知，**绝不硬塞不相干设备**（用户没核出来就确认了，比确认不了更危险）
- **透明化**：工具 note 与确认弹窗（ReviseChatModal 摘要区横幅）都标明「没找到 X，已自动匹配为 Y，请核对」——用户核对的是替换前后对照，而不是系统的猜测；不提示的话核对形同虚设
- **重试反馈带友好名**：此前重试错误只贴 60+ 个拼音 entity_id，模型根本对不上"门"是哪个（3 轮都修不对的原因之一）；现在错误消息附 match_devices top3 候选（名称 + entity_id），提高模型自愈率
- **模型信息面修正（"这条规则控制的 id 是哪个"答不上）**：`automation_rule_list` 每条规则附 actions 摘要（entity_id + 友好名 + 描述，此前只有 actions_count，模型无依据可答）；plan 模式 `explain_rule` 在消息里附「实体对照（entity_id → 设备名）」且 brief 补 camera_id；RULE_EXPLAIN_PROMPT 删掉"entity_id 拼音能翻译出设备名"的错误指引——本部署 id 是厂商乱码，猜必错，改为"如实念 entity_id 并对照设备名，对照表查不到就说查不到"

#### 创建规则关键词门控（「创建规则」才建，普通条件式按普通指令执行）
- **问题**：glm-4-flash 对「如果有人就开灯」这类条件式话术的工具路由不可靠——实测约一半轮次直接调 `call_service` 执行设备动作（被设备消歧层接住），甚至出现**零工具调用、文字谎报"规则已创建"**的幻觉轮（干净会话下也复现）。工具 description 与返回 note 里的引导救不了「根本没调工具」的轮次
- **产品决策**：规则创建改为**关键词门控**——用户消息里明确出现「创建规则 / 新建一条规则」等字样才走创建；普通条件式描述按普通指令执行，不再指望模型自己"想到"建规则
- **判定单一真相源**：`pending_rules.wants_rule_creation(text)`——「规则」+ 创建类动词（创建/新建/建个/设个/加条…）同时出现才命中；必须带「规则」二字，避免抢走「创建场景」「创建定时任务」等其他功能意图
- **硬门（工具层）**：`automation_rule_create` handler 读 `session.current_query`（两条 dispatch 路径都赋值，渠道无关），未命中关键词直接返回 `tool_error`，hint 引导模型「普通条件式按普通指令执行、不要重试；用户想建规则请用『创建规则：…』说法」。零工具调用的幻觉轮本就不经过工具，落库侧不变量（`confirm_pending` 摄像头绑定校验）继续兜底
- **软推（提示词层）**：关键词命中时 `build_system_prompt` 注入本轮指令——必须调 `automation_rule_create` 生成草稿、不要直接执行设备动作、不要只在文字里说已创建；用户只是询问规则时正常回答不调工具
- **工具 description 同步改写**：明确「只在用户消息出现创建类字样时调用」，降低误调率；`automation_rule_revise`/`confirm` 不受门控（草稿存在后的口头「改成…」「确认」照常）

#### 视觉规则必须显式绑定摄像头（堵住"全局视觉规则"危险态）
- **问题**：聊天建规则时 `camera_id` 完全由模型自己猜，猜不出就落库成 `type=vision` + `camera_id=''`。而 `automation_service.evaluate` 对未绑定的规则在**每一路**摄像头上都评估 —— 两路摄像头任意一路有人都触发。这正是 `utils/ruleMismatch.js` 判为 `red`、TaskView 卡片标红「⚠️ 视觉规则未绑定摄像头」的状态，此前却能被静默创建出来
- **判定单一真相源**：`pending_rules.is_vision_rule` / `needs_camera`（type 缺失或非法一律按 vision，与 `rule_service` 的兜底方向、前端 `ruleMismatch.js` 同口径）。`automation_rule_create`/`revise` 的工具返回加 `needs_camera` 字段
- **网页（聊天）**：确认弹窗内嵌摄像头选择器（chip 列表 = 启用的摄像头 + 「全部摄像头（全局）」）。视觉规则**默认不选、不选就不能确认**；选「全部摄像头」时出红字警告并用警示色而非主色高亮——它是合法但危险的选择，必须是个显式、知情的决定。摘要区显示「识别为：视觉规则」标签，type 被兜底误判时用户当场能看见并走 ✏️修改 纠正
- **网页（规则页）**：创建改两段式 —— 先 `POST /api/rules/preview` 只解析不落库，视觉规则弹新的 `CameraBindModal` 选完再 `POST /api/rules` 落库，非视觉规则直接落库沿用 header 的作用范围。原来 header 的「当前作用范围」是**打字之前**定的，而规则是不是视觉类型要等 LLM 解析完才知道，在「全局」范围下输入「有人就开灯」就直接产出红色规则；作用范围与解析结果错配时弹窗内给警告
- **服务端兜底**：`confirm` 的 `camera_id` 三态（`None`=没选 / `""`=显式全局 / 非空=绑定某路），视觉规则没选 → 400 `camera_required`；`POST /api/rules` 同一条不变量。前端禁用按钮只是体验，不变量守在服务端。摄像头 id 拿 `camera_manager.list_cameras()` 校验，非法 → 400 且 message 里列出可选项；一路摄像头都没有 → 400 `no_camera_available`（建一条永不触发的规则比不建更糟）
- **新接口**：`POST /api/rules/preview`（只解析不落库）、`POST /api/rules/pending/{id}/camera`（给草稿改绑）。后者是**渠道无关**的通用能力，未来飞书交互卡片的按钮回调直接调它。`RulePayloadRequest` 补 `name`/`summary`/`action_descriptions`/`camera_id` 四个可选字段（默认 None 且 `create_rule` 会过滤掉，旧调用方行为不变）—— 此前 preview 的产物走 `POST /api/rules` 会静默丢掉这些字段
- **模型措辞**：`needs_camera` 为真时 note 明确「有图形界面时用户会在选择器里选，你不必追问、也不要逐个念摄像头名字，更不要替用户猜一路」，且**刻意不把摄像头清单放进工具返回** —— 网页端弹窗自带选择器，模型再念一遍只与界面重复
- **不变量守在共享层，不是路由层**：校验放在 `pending_rules.confirm_pending` 里。只在 REST 路由校验的话，语音走 `automation_rule_confirm` 工具能直接绕过、照样落库一条未绑定的全局视觉规则 —— 洞只堵了一半。工具侧拿到 `camera_required` 后按 hint 把候选摄像头报给用户，不替用户挑、不谎称已创建
- **`camera_chosen` 标记**：`camera_id=""` 既可能是「显式选了全部摄像头」（合法）也可能是「压根没选」（要拦），光看规则区分不出。`set_pending_camera` 在用户做出选择时打标，`confirm_pending` 据此放行显式全局
- **飞书渠道问答（全部在 `integrations/feishu/`，核心零感知）**：飞书是宿主侧集成，接口只有 `start(dispatch_fn, loop)`，**没有注册 agent 工具的通道**（那是子进程插件的 `agent_tools` 能力，且 `_make_plugin_tool_handler` 转发给子进程的 context 只有 `{user_id, query}`，够不到草稿）。所以问答做成插件自己的状态机：本轮 LLM 回完后查核心是否留下缺摄像头的草稿 → 追问「看哪一路：研发部、门口」→ 下一条消息**被插件吃掉**不进 LLM，解析成 camera_id 后调 `set_pending_camera` → 提示回复「确认」创建。两条消息路径（`_handle_chat` 管道车道 / `_handle_and_reply` 斜杠命令）都接了，漏一条等于对主路径失效；等待窗口 5 分钟（比草稿 TTL 短，隔太久的那句更可能是新指令）；认不出或歧义就把消息**还给 LLM**，绝不吞用户指令；一路摄像头都没有时直接说清"规则不会触发"，不让用户白答一轮

#### 聊天建规则的网页确认弹窗（两段式确认闭环）
- **网页端点一下即落库**：`automation_rule_create` 只生成 10 分钟 TTL 的待确认草稿，此前落库完全依赖模型在用户口头说「确认」后再调 `automation_rule_confirm`——网页端对这个状态毫无感知，用户没打字确认，草稿静默过期，规则永远建不成（"如果有人就打开研发部灯"失败的根因）。现在聊天页收到 `pending_confirm` 工具结果后自动弹确认框（复用 `ReviseChatModal` 的 💡了解/✏️修改 两模式 + 「✅ 确认创建」），确认走 REST 直接落库、绕过模型
- **弹框时机是硬约束**：等本轮 `Dialog.Finish` 到达才弹。`dispatcher` 在轮末才把 user/assistant 消息 append 进 `model_messages`，而确认端点要往同一列表追加「（我已通过界面确认…）」让下一轮 LLM 知道规则已建——轮中弹框会让这条确认排在原始请求**之前**，模型读到乱序历史
- **新接口**（均校验会话归属，404/403 与 `/api/sessions/*` 一致）：`POST /api/rules/pending/{id}/explain|revise|confirm|cancel`，body 带 `session_id`（草稿按会话存）。路径 4 段不与已落库规则的 `/api/rules/{rule_id}/xxx`（3 段）冲突
- **渠道解耦**：确认核心逻辑提取为 `app/services/pending_rules.py`（草稿存取/TTL 懒过期/实体存在性校验/落库），工具 handler 与 REST 路由共用，落库口径单一。核心代码零渠道分支——语音/飞书仍走口头确认，飞书插件本次零改动；未来要做交互卡片确认，全部实现在 `integrations/feishu/` 内调这几个通用端点即可
- **工具措辞改渠道中立**：`automation_rule_create` 的 description 与 note 此前只教模型"等用户口头确认"，网页端会引导出"请回复确认"与自动弹窗打架，也是昨晚"我已为您创建了"式误导的来源。改为明确「规则尚未创建、确认后才生效」并说明网页端会自动弹框

#### 告警/周报主动推送渠道（飞书 Notifier 接线）
- **通知渠道真正可用**：`alert_service.register_notifier` 此前零调用（CHANGELOG 宣传的"推送走 Notifier"实际只有 WS + 日志兜底），现在飞书插件启动成功后自注册为推送渠道——离线告警/HA 断连/恢复通知/家庭周报都会主动推到飞书（warning 前缀 ⚠️、error 前缀 🚨）。推送目标：管理页新配置项「推送目标 chat_id」优先，否则自动推到最近与机器人聊天的会话；两者皆无（部署后无人说过话）则静默跳过。核心宿主零 import 插件，解耦方式与子进程插件反向 RPC 一致；`stop()`/热重启时注销，不留死渠道
- **按需编码省 CPU**：MJPEG 编码改为观众驱动——`mjpeg_generator` 生命周期内计数观众（`_viewers`），无人观看时 `_process_frame` 跳过亮度准备与 JPEG 编码这两步每帧最贵的操作。此前每路摄像头无观众也常开 30fps 编码，与"惰性编码"设计注释不符；采集/dHash 运动检测/视觉推理/环形缓冲/状态更新不受影响，虚拟摄像头同契约

#### 设备状态事件流（家庭报告数据源扩展）
- **设备动态进事件流**：新增 `device_event_service`，经 HA WebSocket 订阅 `state_changed`，断线自动重连；控制类设备（灯/开关/窗帘/门锁/空调等）与 binary_sensor/人员定位每次真实翻转记一条，数值传感器按小时窗口聚合（每实体每小时最多 1 条「变化 N 次（min~max）」），设备掉线（unavailable）即时记——高频传感器不再有稀释告警的风险；`device_events.enabled` 默认开、`sensor_flush_seconds` 可调
- **主控操作事件**：AI 对话控制与设备页手动控制分别落 `device_op` 事件（`tools.py` / `ha_routes.py` 插桩），周报可统计「AI 帮你操作设备 N 次」
- **周报统计扩展**：stats 并入设备动态（AI/手动操作次数、有动态的设备数）与对话轮数（近 7 天用户消息数）；LLM prompt 改为「统计摘要 + 事件记录」双输入，传感器逐条事件不进 LLM 输入（防止挤出告警窗口）；`/report` 页时间线新增「设备」类型筛选
- `weekly_report.enabled` 默认改为开启（周报统计要有数据跑起来才有意义）

### Fixed

#### 自然语言改绑摄像头是静默 no-op
- `rule_service.revise_rule` 喂给 LLM 的 `current_brief` 白名单（`name/condition/type/actions/action_descriptions/cooldown_seconds/summary`）**不含 `camera_id`**，且输出后 `parsed.setdefault("camera_id", current_rule.get("camera_id",""))` 把它钉回原值。用户说「改绑到门口摄像头」时 LLM 根本看不到这个字段，`change_summary` 照样回一句"已绑定门口"——弹窗显示「✅ 已绑定」而 `camera_id` 纹丝不动。飞书里事后改绑走的也是这条死路（创建时一句话带上"用门口摄像头"没事，那时模型能直接给 `automation_rule_create` 传 `camera_id`）
- 白名单补上 `camera_id`，并新增 `_resolve_revised_camera`：LLM 没输出 → 保留原值；输出了但不是真实摄像头 → 重置回原值（幻觉 id 会让规则绑到不存在的那一路，`automation_service` 按 `camera_id` 过滤 → 永不触发，且界面上看不出问题，比不改更糟）；`type` 改成 `time`/`weather` → 清空绑定（`camera_id` 对非视觉规则没有意义，留着会被 `ruleMismatch` 标 orange）
- 摄像头列表经函数内 `get_container()` 惰性取（container 反向依赖 services，模块级 import 会成环）；取不到就放行，与 `pending_rules.find_missing_entities` 的「校验失败放行」同口径
- `prompt_service.RULE_SYSTEM_PROMPT_TEMPLATE` 的 `type` 字段标注必填（三选一的判定规则和示例本来就写得很全，只补这一处）

#### 聊天确认弹窗的摄像头列表永远是空的
- `ChatView` 挂载时并不拉 `/api/cameras`（只有 `useCameraPreview` 开摄像头预览时才调 `loadCameras`），所以弹窗的 `cameras` prop 恒为 `[]`，选择器只剩「全部摄像头（全局）」一项——视觉规则根本没法绑具体某路。改成打开弹窗时懒加载（列表为空才拉），不给每次进聊天页都加一个请求

#### 路由错误被前端渲染成成功（27 处 `ApiResponse(success=False, ...)`）
- `ApiResponse` 只有 `code`/`message`/`data` 三个字段，**没有 `success`**。Pydantic 默认 `extra='ignore'` 把这个 kwarg 静默丢掉，发出去的是 `code="ok"` + HTTP 200；前端 `api.js` 的 `_unwrap` 只在非 2xx 时抛错，且 `json.data ?? json` 在 `data=null` 时返回整个信封对象——于是 `ReviseChatModal` 拿到 `result.rule === undefined`，预览不更新，却照样 push `✅ ${result.summary || '已更新'}`。**LLM 改规则失败时用户看到的是「已更新」**
- 27 处统一改为 `raise AppException(msg, code=..., http_status=...)`，按语义分流：服务未就绪 503、资源不存在 404、入参/指令不合法 400、LLM 侧失败 502、落库失败 500。涉及 `rule_routes`(5)、`scheduler_routes`(16)、`scene_routes`(3)、`report_routes`(2)、`ha_routes`(1)
- 前端零改动即修复：`ScheduledTasksView`/`SceneBar`/`FamilyReportView`/`ReviseChatModal` 全是 `apiPost` + try/catch，catch 里显示 `e.message`，改成非 2xx 后自动走对分支
- **`ha_routes` 的 `POST /ha/config` 是例外，刻意保留 200**：它返回 `data={"saved": False}`（非 null，`??` 不触发），`AdvancedView.vue` 明确依赖 `data.saved === false` 把 probe 失败就地显示在输入框旁、而不是弹全局错误。只删掉那个会被丢弃的 `success=False` 死参数，并加注释锁住这个契约
- 重写 25 个锁住旧错误行为的测试断言（`assert "未就绪" in out.message` + HTTP 200 → `pytest.raises(AppException)` + 断言 `http_status`）；`test_routes_misc_coverage.py` 新增 `_expect_error` helper 说明这个坑的机制，`test_revise_error_swallowed` 随之改名 `test_revise_error_raises`

#### 口头确认路径实际不可用（跨轮丢 pending_id）
- `session.model_messages` 不持久化 tool 消息（工具结果只内联进 assistant 回复），跨轮后模型看不到上一轮工具返回的 `pending_id`——用户在飞书/小爱里第二轮说「确认」时它无 id 可传，`automation_rule_confirm` 必然失败。`confirm`/`revise` 现在在 id 缺失或失效时回退到「会话内唯一未过期草稿」，多于 1 个才报错要求用户指明；两个工具的 JSON schema 同步把 `pending_id` 改为非必填，否则模型只能编一个 id 出来
- 落库抛错时不再摘除草稿（此前 `add_rule` 异常直接冒泡，草稿去留取决于调用方），用户可重试；错误按 `reason` 分流：设备已消失 400、落库失败 500、草稿没了 404，工具侧对应不同 hint
- 前端 `api.js` 的错误对象补挂 `err.status`（HTTP 状态码）——此前调用方拿不到状态码，无法区分"草稿已过期该重说需求"和"设备不存在可以先改"
- `_check_session_owner` 从 `session_routes` 提到 `core/auth.require_owned_session`：新的 pending 端点按 session_id 读写会话内部状态，同样必须过归属校验（规则落库后会真实驱动设备，跨用户确认的后果比读会话严重）

#### 摄像头 PUT 回显明文密码（H1）+ 提示词幽灵工具
- `PUT /api/cameras/{id}` 响应漏套 `_mask_camera`，唯一未脱敏的摄像头写接口——任何登录用户编辑保存即拿到该路 RTSP/PTZ 明文密码（GET/POST/单 GET 均已脱敏）。一行补齐
- `chat_assistant.capabilities`/`guidelines` 向模型声明 `question`、`todowrite` 工具，但工具注册表里两者不存在（实际 16 个），模型照指引调用必命中"未知工具"。已从 config 声明中移除，指引改为直接向用户提问/自行拆解步骤

#### 摄像头「在线仍报离线」根因修复
- **RTSP 防爆破锁定自锁循环**：设备离线/凭证被拒时 worker 固定 60s 冷退避携带凭证反复鉴权（实测 54 小时 2400+ 次），触发摄像头端防爆破锁定——密码正确也 401，越试越锁。开流加 socket 超时（`stimeout`/`timeout` 双写兼容 FFmpeg 版本，不可达时 ~21s → ~5s 失败）；冷启动退避分档 60s → 300s → 900s 封顶（`vision.cold_open_max_backoff_seconds`），给设备端锁定静默过期的机会
- **重启后恢复通知永久丢失**：告警状态纯内存，重启清空导致「已恢复在线」不再推送（实测 232 条离线告警 0 条恢复）。`alert_service` 启动时从 `family_events` 按时间序回放重建未恢复告警（仅 camera:*/ha:connection 等有恢复语义的 source），重启后恢复在线正常补发通知
- `camera_manager.get_state` 无 stream 回退补 `camera_opened` 键（消除重建窗口期误判离线拍）；已删除摄像头的离线计数顺手回收

---

## [1.1.0] - 2026-08-28

### Added

#### 场景模式（与插件完全解耦）
- 设备页顶部「🏠 场景模式」条（SceneBar）：场景芯片一键应用、输入名字把当前设备状态存成场景（capture 覆盖 light/switch/cover/fan/climate/humidifier 六域）、× 删除；全家成员可建/用/删
- 聊天工具 `scene_list` / `scene_apply` / `scene_create`（内置工具 13→16 个）；「打开观影模式」「把现在的状态存成睡眠模式」一句话搞定
- 新接口：`GET/POST /api/scenes`、`POST /api/scenes/{id}/apply`、`DELETE /api/scenes/{id}`；数据存 `scenes` 表

#### 家庭报告与离线告警
- **离线告警**（`alert_service` 模块单例，`alerts.enabled` 默认开）：摄像头离线（约 1 分钟确认）、HA 断连（约 3 分钟）、定时任务失败、插件熔断四类事件源；30 分钟同源冷却 + 恢复通知；推送走 Notifier 渠道（如飞书）+ 在线聊天页 WS，降级为 `[Alert]` 日志
- **家庭周报**（`weekly_report_service`）：周日 20 点（`weekly_report.hour`）聚合近 7 天 `family_events` → summary 角色 LLM 写 200 字周报，LLM 不可用退化纯统计；默认关闭（`weekly_report.enabled=false`）；`/report` 家庭报告页（时间线 + 周报 + 手动生成）+ 聊天斜杠命令 `/report`
- `family_events` 统一事件流（告警/恢复/任务成败/自动化触发，90 天自动修剪）

#### 模型家族适配插件（model_adapter）
- 集成插件平台新增进程内能力：插件声明 `model_adapter` 能力后不 spawn 子进程，宿主进程内 import 其 `adapters.py`，按当前 chat 模型（per-user 优先）改写本轮消息；插件启停/上传/删除后热刷新注册表
- 内置 `integrations/qwen-adapter`：Qwen 系 `/no_think` 注入降首字延迟（实测 22.6s → 5.0s）；宿主零家族特判，新家族接入零宿主改动
- 新接口：`GET /api/llm/status`（各角色实际生效模型 + 连通性）

#### 安全与稳定性批次（第 1~4 批）
- **安全**：MQTT 1884 仅绑定宿主回环；管理员分级收权；`/healthz` 免认证存活探针 + docker healthcheck（事件循环卡死自愈）；`mem_limit: 2g`；取消跟踪 `ha_config/.storage/core.config`（家庭精确经纬度）
- **稳定性**：SQLite 损坏自愈；WS 断连重连、写序、资源泄漏等 10 项修复；会话自动治理（每用户保留最近 `storage.max_sessions_per_user`=50 个）；凭据轮换清单（`docs/凭据轮换清单.md`）
- 周期健康检查可配（`health.ha_interval_seconds`=60 / `health.llm_interval_seconds`=0 默认关）

#### 其他新增
- **家庭报告页事件统计图表**：/report 新增统计区——4 张数字卡（设备操作/自动化触发/任务成功率/告警）+ 每日事件趋势堆叠柱状图 + 设备操作 TOP5 横向条形图（AI/手动堆叠）+ 操作发起方占比条；新聚合接口 `GET /api/events/stats?days=N`（1-90）；`family_events` 表加结构化 `actor` 列（幂等迁移），device_op 写入 AI/手动发起方，存量旧数据统计时按 message 前缀回退兼容；图表组件 EventCharts 照 SensorChart 范式（ECharts 按需注册补 BarChart/PieChart/Legend、CSS 变量主题、IntersectionObserver 懒加载）
- **报告页图表下钻 + 布局重构**：页面改为"天数切换 + 生成周报在顶部 → 图表为主体 → 周报/数字卡/时间线在下方"；点击趋势柱状图某一天的柱子直接下钻到那天的事件时间线（面包屑返回总览，类型可再筛）；`GET /api/events` 新增 `date=YYYY-MM-DD` 精确查某天（本地时区）；时间线 500 条截断改按类型保底配额（每类保底 80 条再按时间补齐），修复高频 device_state 把低频 automation/task 整类挤出时间线导致"统计有数、列表看不到"的观感割裂
- 规则与任务的对话式修订：`POST /api/rules/{id}/revise`·`/explain`、`POST /api/scheduled-tasks/{id}/revise`·`/explain`、`PUT /api/rules/{id}`·`/api/scheduled-tasks/{id}`
- 设备历史查询 `GET /api/ha/history`（传感器趋势图）；视觉识别日志 `GET/DELETE /api/vision-logs`；`POST /api/setup/ha`；`POST /api/weather/test`；`GET /api/files/browse`（管理员）

### Changed
- **统一设备注册表**：AI 视图/闸门候选/前端三视图同源；禁止 AI 操作的实体改为**渲染层直接排除（对 AI 不可见）**，`call_service` 硬校验兜底；子功能短名（剥父设备名前缀）；`call_service` 候选自愈（编造 entity_id 时反查真实候选重试）
- **摄像头离线过期帧防护**：MJPEG 宽限期（`vision.offline_hold_seconds`，默认 10s，超时发 NO SIGNAL）后不再发缓存帧；离线路视觉规则直接跳过（不用旧帧）；`vision_chat` 离线不调模型、如实告知 `camera_offline=true`
- **定时任务回复语音+文字同步推送**：语音走 sink 广播、文字经 `ws_registry` 推 `Template.ToastStream` 给在线聊天页；`run` 接口默认等结果并返回 `last_status`/`last_reply`
- **自动化三驱动**：dhash 事件 + 视觉静默循环（300s）+ 非视觉静默循环（`automation.nonvision_silent_interval_seconds`=30，time/weather 规则唯一评估来源）；`evaluate` 按规则 type 路由（time/weather→chat LLM，vision→VL）+ 设备状态门控（动作已在目标态 0 LLM 跳过）
- 视觉关注未配置时**不再兜底**「画面中的人和他们的行为」，模型自由描述画面
- 多帧抓拍默认 `frame_interval_ms` 2000→1000（存量旧值自动迁移）；vision_chat 升级多帧问答
- 定时任务 reminder/message 的 per-user chat 客户端改走 `build_per_user_chat_client`（强制 enabled）
- **设备详情趋势图平铺**：历史趋势从"仅当前选中实体的单一图位"改为设备下每个传感器各一张卡（带实体名），宽屏两列网格、弹窗自动加宽至 880px；超过 4 个折叠并提供「展开其余」；SensorChart 改为进入视口才初始化/拉取（几十个属性实体不再齐发请求），修复画布在 `v-show` 隐藏期间以 0×0 初始化的问题，实体切换丢弃过期响应防止旧数据串图

### Removed
- **数据出网策略（egress_policy 三档）**：`cloud`/`hybrid`/`local` 模式开关整体移除——是否出网由各角色配置的模型端点决定（内网端点即零出网），《数据流向说明》已改版（v2）
- 在线更新源通道（升级收敛为升级包离线投放 + git 脚本）；`update.git_token`/`git_repo_path` 废弃
- 旧单摄全局入口 `/api/state`、`/api/video_feed`、`/api/advanced/test/rtsp`、公开 `/search` 与 `/api/output/latest/graph.json` 豁免（一律走认证接口）

---

## [1.0.0] - 2026-08-16

### Added

#### 设备语义映射（/semantics）
- 新增 `/semantics` 页面（斜杠命令 `/semantics` 进入）：两层配置——先选实体，再配 service→target 映射
- `call_service` 执行前**无条件替换 service**（AI 凭直觉调用，过滤器纠正，映射规则不进提示词防双重错误）；批量 entity_id 按共识制替换
- **state 隐含翻转**：对称翻转对（如继电器 turn_on↔turn_off 反接）自动翻转 on/off，`get_entities`/`get_device_manual`/设备目录/`call_service` 返回一致，AI 不再说反话
- 新接口：`GET/PUT /api/ha/action-maps`、`GET /api/ha/entity-services`（target 合法性校验：须属该域且 ≠ 源 service）

#### AI 设备操作权限（entity_operable 黑名单）
- 设备详情子实体新增「AI 可操作」绿/红徽章，可把危险设备（门锁/童锁）标为禁止 AI 操作，完全可逆
- 三层防线：设备目录标注 `⛔AI禁操作` + system prompt 约束 → `call_service` 硬拦截 → DB 异常放行防锁死全屋
- 新接口：`GET/PUT /api/ha/entity-operable`；写入后立即刷新设备目录缓存
- `get_entities` 返回实体 `ai_operable` 权限字段

#### 运维中心（/operations，仅管理员可见）
- 运维能力全部按钮化，无需登录主机操作文件：诊断包导出、部署体检、备份/恢复、离线升级、在线升级
- **诊断包导出**：一键打包脱敏信息（config 打码、日志尾部 2MB/总量 10MB、docker ps 经 UDS）
- **部署体检**：端口/HA/RTSP/DNS/磁盘/内存/NTP 检查，每项"通过/警告/失败 + 怎么办"三段式，<10s
- **在线升级**：配置更新源地址（任意静态 HTTP），检查更新 + 一键升级；两层校验（渠道整包 sha256 + 包内镜像 sha256/min_compatible）
- **备份/恢复**：应用侧备份（config + .env + HA/MQTT 配置 + 数据卷），保留最近 3 份；恢复前预检 + confirm 确认 + 自动重启
- 全部运维操作写审计日志（`logs/audit/ops_audit.jsonl`）
- 命令行配套：`scripts/backup.sh`、`restore.sh`、`upgrade.sh`、`build-update-pack.py`、`diagnose.py`、`export_diag.py`、`update-from-git.sh`

#### 数据出网策略（egress_policy 三档）
- `cloud`（云端对话）/ `hybrid`（混合）/ `local`（纯内网）三档模式，「高级设置 → 数据出网模式」切换，聊天页实时徽标
- `local` 模式**硬拦截**公网模型端点（Key 保存/测试卡点）；切回云端/混合立即放行
- 引导页数据流向声明确认（SHA-256 + 时间 + 操作人入库），确认后不可误触跳过
- **内置 Ollama**：`docker compose --profile local-llm up -d ollama`，OpenAI 兼容端点 `http://ollama:11434/v1`，零出网可落地

#### 安全加固
- **管理员分级**：首注册用户即管理员；插件上传/删除、HA 连接配置、模拟器开关、运维操作、二级密码管理收权至管理员
- **登出 token 撤销**：jti 黑名单，登出后未过期 token 立即失效
- 认证旁路修复 + 敏感信息泄漏修复（接口返回密钥/URL 脱敏）
- 删除全局 key 的二级密码从 URL query 改为 request body（防泄漏）
- 插件进程沙箱化（环境白名单 + 异常脱敏）；集成插件 Phase 3 反向 RPC（插件→宿主）

#### 虚拟设备开关
- 「高级」页新增「虚拟设备」段：经 docker.sock 停/启 simulator+mosquitto 容器，停/启后即时刷新设备视图
- 模拟器设备「全部离线才隐藏」过滤规则（真实设备不受影响）

#### 交付物料
- 《SLA 模板》《免责声明模板》（`docs/10-交付物料/`，占位符 `【】`）
- 《数据流向说明》（拓扑/出网点清单/三模式对照，可打印转 PDF）
- 《09-商业化工程化清单》7 条目首轮落地

### Changed
- MQTT 凭证参数化：`MQTT_USER`/`MQTT_PASSWORD` 从宿主 `.env` 注入（默认 aether/aether），mosquitto 真实 healthcheck（QoS1 PUBACK 探活）
- `config.json` 移出 git 跟踪（只保留 `config.example.json` 模板）
- 全部容器 `restart: unless-stopped`（OOM/崩溃自愈）；aether 镜像固定 `aether-app:latest` tag（离线升级与开发流不打架）
- 语义映射入口定为聊天斜杠命令 `/semantics`（曾短暂放侧边栏后撤掉）；斜杠命令描述精简
- 新增 `/plugin` 插件管理、`/operations` 运维中心斜杠命令（后者仅管理员可见）

### Fixed
- 批量 entity_id 映射共识制 + toggle 等未映射动作也翻转 state
- controls 缓存为空时同步触发刷新（备注写入不再丢失）
- 前端陈旧测试修复 + PluginSlot 非数组贡献防护
- 飞书 ws 心跳修复；Validator 去硬编码设备名；多路 discovery 读 per-camera 开关、IP 变更自动重建 stream
- 代码审查四批修复（监听泄漏/online 状态/RTSP 转义/STT 限制/缓存失效/IDOR/鉴权严格化等）

### Removed
- 旧全局 PTZ 体系：`app/routes/ptz_routes.py` 删除，`/api/ptz/*` 端点不再存在（云台收敛到 per-camera，走 `/api/cameras/{id}/ptz/*`）
- 死代码清理（`_extract_json` 重复收敛、零调用代码全删）；归档 superpowers/phase3 计划与设计稿

---

## [0.9.0] - 2026-08-11

### Added
- 多路摄像头管理（`CameraManager`）：RTSP/USB 混用，per-camera 参数（运动阈值/推理间隔/PTZ/关注项）
- AI 预览单路互斥切换；ONVIF 发现找回 IP（worker 掉线自动触发）
- 设备备注（entity_note）注入 AI 认知；实体别名同步 HA
- 斜杠命令系统（13 个）；飞书机器人集成（webhook + 定向 speak_to + session 隔离）

### Changed
- Docker 服务 3→4（新增 aether-simulator）；MQTT 关匿名改凭证认证
- HA 连接配置并入「高级」页卡片；LLM 密钥管理迁到 `/models` 页（per-user + 全局二级密码）

---

## [0.8.0] - 2026-08-06

### Added
- JWT 多用户鉴权（access 24h / refresh 7d，httpOnly cookie）；per-user LLM 密钥与会话隔离
- 全局密钥二级密码门禁；`use_global` 角色兜底开关；启动自愈（key_healing）

---

## [0.7.0] - 2026-07-20

### Added
- 语义知识图谱（RAG）：文档向量化 + faiss 检索 + 实体共现构图，3D 可视化；embed 模型变更检测 + 一键重建

---

## [0.6.0] - 2026-07-01

### Added
- 定时任务（自然语言→cron，任务名自动生成）；自动化规则（条件联动、多条件组合）；视觉触发规则

---

## [0.5.0] - 2026-06-15

### Added
- 摄像头视觉感知：RTSP/USB 接入、dHash 运动门控、VL 推理、per-camera 关注项；MCP 工具生态（内置 13 工具 + 外部 stdio Server）

---

## [0.1.0] - 2026-05-01

### Added
- 项目初始化：基础聊天、Home Assistant 设备控制、Docker Compose 部署
