# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### 设备状态事件流（家庭报告数据源扩展）
- **设备动态进事件流**：新增 `device_event_service`，经 HA WebSocket 订阅 `state_changed`，断线自动重连；控制类设备（灯/开关/窗帘/门锁/空调等）与 binary_sensor/人员定位每次真实翻转记一条，数值传感器按小时窗口聚合（每实体每小时最多 1 条「变化 N 次（min~max）」），设备掉线（unavailable）即时记——高频传感器不再有稀释告警的风险；`device_events.enabled` 默认开、`sensor_flush_seconds` 可调
- **主控操作事件**：AI 对话控制与设备页手动控制分别落 `device_op` 事件（`tools.py` / `ha_routes.py` 插桩），周报可统计「AI 帮你操作设备 N 次」
- **周报统计扩展**：stats 并入设备动态（AI/手动操作次数、有动态的设备数）与对话轮数（近 7 天用户消息数）；LLM prompt 改为「统计摘要 + 事件记录」双输入，传感器逐条事件不进 LLM 输入（防止挤出告警窗口）；`/report` 页时间线新增「设备」类型筛选
- `weekly_report.enabled` 默认改为开启（周报统计要有数据跑起来才有意义）

### Fixed

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
