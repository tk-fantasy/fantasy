# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
