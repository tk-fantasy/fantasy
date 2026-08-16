# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-16

### Added

#### 运维中心（Operations Center）
- 新增 `/operations` 运维中心页面，集中管理所有运维功能
- **诊断包导出**：一键打包脱敏的诊断信息（日志、配置、系统信息），方便远程排障
- **部署体检**：端口占用、网络连通性、资源检查、RTSP 可达性等全面体检
- **在线升级**：配置更新源地址，一键检查更新并升级
- **备份与恢复**：应用侧备份（config.json + .env + 数据卷），保留最近 3 份
- 所有运维操作写审计日志（`logs/audit/ops_audit.jsonl`）

#### 数据出网策略（Egress Policy）
- 三档模式切换：`cloud`（云端对话）、`hybrid`（混合模式）、`local`（纯内网）
- 聊天页面显示当前模式徽标
- 引导页声明确认，记录用户确认信息（hash + 时间 + 操作人）
- `local` 模式下硬拦截公网模型端点（密钥保存/测试时拦截）

#### 安全增强
- **管理员分级**：首个注册用户自动成为管理员
- 危险接口收权：插件上传/删除、HA 连接配置、模拟器开关、运维操作、二级密码管理
- 认证旁路修复：修复 WebSocket 连接可能绕过 JWT 校验的问题
- 敏感信息脱敏：API 返回的密钥、URL 等字段统一脱敏

#### 摄像头 PTZ 重构
- 删除旧全局 PTZ 体系，收敛到 per-camera 配置
- 每路摄像头独立配置 PTZ（IP、端口、凭证、速度）
- ONVIF 发现按摄像头进行（`/api/cameras/{id}/discovery/*`）

#### 离线升级框架
- 版本号规范（`version.json` + `AETHER_APP` 常量）
- 升级包结构：manifest + 镜像 tar + 升级脚本
- 两层校验：渠道整包 sha256 + 包内镜像 sha256
- 失败自动回滚（健康检查 180s 超时触发）

#### 交付物料
- SLA 模板（服务级别协议）
- 免责声明模板（AI 误操作风险条款、数据保留期限）

### Changed

- **MQTT 凭证参数化**：Mosquitto 关闭匿名访问，使用 `aether/aether` 凭证
- **配置文件移出版本库**：`config.json` 加入 `.gitignore`，仅保留 `config.example.json`
- **斜杠命令精简**：`/camera` → `/cameras`，`/semantics` 入口改为斜杠命令
- **界面导航调整**：移除侧边栏"监控"入口，改为斜杠命令

### Fixed

- 修复 Mosquitto 健康检查配置错误
- 修复前端测试用例陈旧导致的测试失败
- 修复 PluginSlot 非数组贡献防护
- 修复批量 entity_id 映射问题（toggle 等未映射动作状态翻转）

### Removed

- 删除旧全局 PTZ 路由（`/api/ptz/*` 保留但标记为旧接口）
- 删除 SearXNG 搜索引擎容器（替换为云端 Exa MCP）
- 删除 `automation-redesign.md` 设计稿（已归档）

---

## [0.9.0] - 2026-08-11

### Added

- 多路摄像头管理（`CameraManager`）：支持添加多路 RTSP/USB 摄像头
- 摄像头 per-camera 配置：每路独立配置关注项、运动检测参数、PTZ
- AI 预览单路切换：多路摄像头中只有一路走 VL 推理
- ONVIF 摄像头发现：自动扫描子网，找回 IP 变更的摄像头
- 斜杠命令系统：聊天输入 `/` 快速跳转到各功能页面
- 飞书机器人集成：Webhook + 定向 speak_to + session 隔离

### Changed

- Docker 服务从 3 个增加到 4 个（新增 aether-simulator 模拟器）
- MQTT 配置从允许匿名改为凭证认证
- HA 连接配置从独立 `/ha` 页面改为高级设置卡片
- LLM 密钥管理从高级设置改为 `/models` 页面

---

## [0.8.0] - 2026-08-06

### Added

- JWT 多用户鉴权：每个用户独立账号，登录后拿 access/refresh token
- per-user LLM 密钥：每个用户可配置自己的模型密钥，互不影响
- 全局密钥二级密码：全局 key 的写操作需二级密码保护
- 会话按用户隔离：每个用户的聊天记录互不可见

---

## [0.7.0] - 2026-07-20

### Added

- 语义知识图谱（RAG）：文档向量化 + FAISS 检索 + 实体共现构图
- 3D 可视化：交互式图谱展示
- Embed 模型变更检测：自动检测并提示重建索引

---

## [0.6.0] - 2026-07-01

### Added

- 定时任务：自然语言生成 cron 表达式，任务名自动生成
- 自动化规则：条件联动设备，多条件组合
- 视觉触发规则：运动检测触发自动化评估

---

## [0.5.0] - 2026-06-15

### Added

- 摄像头视觉感知：RTSP/USB 接入，运动检测触发 VL 推理
- 视觉关注项配置：指定关注的对象和区域
- MCP 工具生态：内置天气/搜索/设备控制工具，支持外部 MCP Server

---

## [0.1.0] - 2026-05-01

### Added

- 项目初始化
- 基础聊天功能
- Home Assistant 设备控制
- Docker Compose 部署