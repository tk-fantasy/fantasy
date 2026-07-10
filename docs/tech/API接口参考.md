# API 接口参考

> 对应代码：`app/main.py`（直接注册路由 + 中间件）、`app/routes/*.py`（各业务路由）

本文列出 Aether 后端全部 HTTP/WebSocket 接口。所有路径以实际挂载为准——除 `setup_router`/`doc_router`/`ws_router` 无 prefix 外，其余 14 个 router 全部挂载在 `prefix="/api"` 下。

## 认证机制

### 全局网关 `api_token_guard`

`app/main.py` 的 `api_token_guard` 中间件对**所有 `/api/*` 路径**强制认证，**例外**三类：

1. 路径以 `/api/auth` 开头（注册/登录/刷新/登出公开）
2. 路径恰好等于 `/api/output/latest/graph.json`（语义图公开读取）
3. 路径不以 `/api` 开头（静态资源、SPA、`/search`、`/doc/content` 等）

非例外的 `/api/*` 请求必须携带以下任一凭证，否则返回 401：

- **JWT**：`Authorization: Bearer <access_token>` 头，或 `aether_token` httpOnly cookie
- **APP_TOKEN 兜底**：`X-API-Token: <APP_TOKEN>` 头（`APP_TOKEN` 环境变量配置，留空则不启用）

JWT 细节：access 24h / refresh 7d，HS256，`JWT_SECRET` 环境变量（自动持久化 `app/data/.jwt_secret`），密码哈希 `pbkdf2_sha256`。详见《08-运维排查/API Token安全鉴权》。

### 速率限制

`global_rate_limit` 中间件按 IP 限流 **120 次/分钟**，豁免 `/ws/*`、非 `/api`、`/api/auth`。auth 路由另有独立限流：注册 3/min、登录 5/min。

### WebSocket 认证

`/ws/*` 不走 HTTP 中间件，在 handler 内调用 `_ws_verify_token` 校验，按顺序取 token：query 参数 `token` → `aether_token` cookie → `X-API-Token` 头。

### 响应包装

所有接口统一返回 `ApiResponse[T]`：

```jsonc
{ "success": true, "data": <T>, "message": "" }      // 成功
{ "success": false, "data": null, "message": "错误原因" } // 失败
```

下表「认证」列：**JWT** = 全局中间件网关；**JWT+用户** = 中间件 + 路由内 `Depends(get_current_user)` 双重；**公开** = 中间件豁免；**WS** = handler 内校验。

---

## 1. 认证 /auth

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| POST | `/api/auth/register` | 公开 | `AuthRegisterRequest` | 注册，3/min 限流，首个用户即普通用户（无管理员特权） |
| POST | `/api/auth/login` | 公开 | `AuthLoginRequest` | 登录，5/min 限流，成功设 httpOnly cookie |
| POST | `/api/auth/refresh` | 公开 | 无 | 刷新，refresh_token 从 cookie 读 |
| POST | `/api/auth/logout` | 公开 | 无 | 登出，清 cookie |
| GET | `/api/auth/me` | JWT+用户 | 无 | 当前用户信息 |

```jsonc
// AuthRegisterRequest / AuthLoginRequest
{ "username": "alice", "password": "secret" }

// register/login 返回
{ "user": { "id": "uuid", "username": "alice", "display_name": "alice" } }

// me 返回完整 user 记录
```

注册时会初始化该用户的空 `user_settings`（`llm_keys=[]`、`providers={}`）。

---

## 2. 用户 /users

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/users` | JWT+用户 | 无 | 已完成配置的用户列表（有 LLM keys 的） |
| GET | `/api/users/me` | JWT+用户 | 无 | 当前用户 |
| POST | `/api/users/switch` | JWT+用户 | `UserSwitchRequest` | 切换用户，应用其个人配置 |
| GET | `/api/users/{username}/llm_keys` | JWT+用户 | 无 | 指定用户的 LLM keys |
| POST | `/api/users/{username}/llm_keys` | JWT+用户 | `UserLLMKeysRequest` | 保存 LLM keys |
| GET | `/api/users/{username}/providers` | JWT+用户 | 无 | 指定用户的 providers 配置 |
| POST | `/api/users/{username}/providers` | JWT+用户 | `UserProvidersRequest` | 保存 providers 配置 |

```jsonc
// UserSwitchRequest
{ "username": "bob" }
// UserLLMKeysRequest
{ "keys": [ { "base_url": "...", "model": "...", "api_key": "...", "id": "..." } ] }
// UserProvidersRequest
{ "providers": { ... } }
```

> **多用户隔离范围**：`user_id` 仅隔离**会话历史**和**个人 LLM keys/providers/home_info**。运行时 LLM 客户端是**全局**的——切换用户会 `reload_all_clients` 重载全局客户端，不并发隔离多个用户的 LLM 调用。详见《系统架构概述》。

---

## 3. 会话与聊天 /sessions, /chat

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| POST | `/api/chat` | JWT | `ChatRequest` | 同步聊天（非流式） |
| POST | `/api/sessions` | JWT+用户 | 无 | 创建会话 |
| GET | `/api/sessions` | JWT+用户 | 无 | 列出会话（按 updated_at 倒序） |
| GET | `/api/sessions/{session_id}` | JWT+用户 | 无 | 获取单个会话 |
| DELETE | `/api/sessions/{session_id}` | JWT+用户 | 无 | 删除会话 |
| POST | `/api/sessions/{session_id}/fork` | JWT+用户 | 无 | 分叉会话 |
| POST | `/api/sessions/{session_id}/undo` | JWT | 无 | 撤销最后一对用户-助手消息 |
| POST | `/api/sessions/{session_id}/clear` | JWT | 无 | 清空消息保留元数据 |
| POST | `/api/sessions/{session_id}/compress` | JWT | 无 | 手动触发摘要压缩 |

```jsonc
// ChatRequest
{ "request_id": "可选", "session_id": "可选", "query": "你好" }
```

流式聊天走 WebSocket `/ws/chat`（见第 13 节），不走 `/api/chat`。

---

## 4. Home Assistant /ha

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/ha/entities` | JWT | 无 | HA 实体列表 |
| GET | `/api/ha/services` | JWT | 无 | HA 服务定义 `{domain:{service:{fields,required}}}` |
| POST | `/api/ha/call_service` | JWT | `HAServiceCallRequest` | 调用 HA 服务 |
| GET | `/api/ha/config` | JWT | 无 | HA 配置（token 脱敏） |
| POST | `/api/ha/config` | JWT | `HAConfigRequest` | 保存 HA 配置 |
| POST | `/api/ha/test` | JWT | 无 | 测试 HA 连接 |

```jsonc
// HAServiceCallRequest
{ "domain": "light", "service": "turn_on", "entity_id": "light.living_room", "data": {} }
// HAConfigRequest
{ "url": "http://homeassistant:8123", "token": "长期访问令牌" }
```

> **没有 `/api/ha/devices`**——设备数据通过 `/api/ha/entities` 获取。HA 集成通过本地镜像 `aether-ha:local` 运行，详见《MQTT设备接入协议》。

---

## 5. LLM 密钥与设置 /llm_keys, /llm/settings

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/llm_keys` | JWT+用户 | 无 | 当前用户 LLM keys（不含密钥值） |
| POST | `/api/llm_keys` | JWT+用户 | `LLMKeyRequest` | 添加/更新 key（新增时自动测试连接） |
| DELETE | `/api/llm_keys/{key_id}` | JWT+用户 | 无 | 删除 key |
| GET | `/api/llm/settings` | JWT | 无 | 当前 LLM 设置 |
| POST | `/api/llm/settings` | JWT+用户 | `LLMSettingsRequest` | 应用 LLM 设置 |
| POST | `/api/models/test` | JWT | `ModelTestRequest` | 测试模型连接 |

```jsonc
// LLMKeyRequest
{ "base_url": "https://...", "model": "glm-4-flash", "type": "", "api_key": "...", "id": "" }
// LLMSettingsRequest — 把某个 key 绑定到某个角色
{ "role": "chat", "key_id": "k1", "max_concurrency": 8, "thinking": null, "multimodal": null }
// ModelTestRequest
{ "base_url": "...", "model": "...", "role": "chat", "api_key": "...", "chat_path": "/chat/completions", "embed_path": "/v1/embeddings" }
```

5 个 LLM 角色：`chat` / `vision` / `summary` / `embed` / `stt`。详见《配置参考》。

---

## 6. 自动化规则 /rules, /task/rule

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| POST | `/api/task/rule` | JWT | `RuleCreateRequest` | 自然语言→结构化规则（LLM 解析） |
| GET | `/api/rules` | JWT | 无 | 列出全部规则 |
| POST | `/api/rules` | JWT | `RulePayloadRequest` | 直接创建规则（含 condition） |
| POST | `/api/rules/{rule_id}/enabled` | JWT | `RuleEnabledRequest` | 启停规则 |
| DELETE | `/api/rules/{rule_id}` | JWT | 无 | 删除规则 |

```jsonc
// RuleCreateRequest
{ "text": "如果有人挥手就打开客厅灯" }
// RulePayloadRequest
{ "condition": "画面中有人挥手", "actions": [ { "mcp_tool_name": "ha_devices___call_service", "mcp_tool_input": {...} } ], "enabled": true, "cooldown_seconds": 10 }
// RuleEnabledRequest
{ "enabled": true }
```

规则创建与评估详见《自动化引擎详解》。

---

## 7. 定时任务 /scheduled-tasks

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| POST | `/api/scheduled-tasks/parse-schedule` | JWT | `ScheduleParseRequest` | 自然语言→`{schedule, summary}` |
| GET | `/api/scheduled-tasks` | JWT | 无 | 列出全部任务（按 created_at 正序） |
| POST | `/api/scheduled-tasks` | JWT | `ScheduledTaskCreateRequest` | 创建任务 |
| POST | `/api/scheduled-tasks/{task_id}/enabled` | JWT | `ScheduledTaskEnabledRequest` | 启停任务 |
| POST | `/api/scheduled-tasks/{task_id}/run` | JWT | 无 | 手动触发一次（调试） |
| DELETE | `/api/scheduled-tasks/{task_id}` | JWT | 无 | 删除任务 |

```jsonc
// ScheduleParseRequest
{ "phrase": "每天早上8点" }
// 返回 { "schedule": { "kind": "cron", "expr": "0 8 * * *" }, "summary": "cron: 0 8 * * *" }

// ScheduledTaskCreateRequest
{
  "name": "起床提醒",
  "schedule": { "kind": "cron", "expr": "0 8 * * *" },          // at / every / cron
  "payload": { "kind": "message", "message": "该起床了" },        // tool / message
  "enabled": true
}
// ScheduledTaskEnabledRequest
{ "enabled": true }
```

调度器未就绪时（`scheduler_service is None`）所有 CRUD 返回 `{"success":false,"message":"调度器未就绪"}`。详见《定时调度引擎详解》。

---

## 8. 视觉关注项 /vision/focus(es)

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/vision/focus` | JWT | 无 | 旧接口，返回第一条关注 |
| POST | `/api/vision/focus` | JWT | `VisionFocusRequest` | 旧接口，追加一条关注 |
| GET | `/api/vision/focuses` | JWT | 无 | 全部关注项 |
| POST | `/api/vision/focuses` | JWT | `VisionFocusesCreateRequest` | 新增 |
| PUT | `/api/vision/focuses/{focus_id}` | JWT | `VisionFocusesUpdateRequest` | 更新 |
| DELETE | `/api/vision/focuses/{focus_id}` | JWT | 无 | 删除 |

```jsonc
// VisionFocusRequest (旧)
{ "focus": "画面中的人和他们的行为" }
// VisionFocusesCreateRequest
{ "text": "画面中的人和他们的行为" }
// VisionFocusesUpdateRequest（字段均可空）
{ "text": null, "enabled": true }
```

关注项用于 `classify_frame` 分类提示词注入 + 系统提示词注入，OR 关系。详见《05-摄像头视觉/视觉关注项配置》。

---

## 9. 摄像头与云台 /state, /video_feed, /ptz

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/state` | JWT | 无 | 摄像头状态 `CameraStateModel` |
| GET | `/api/video_feed` | JWT | 无 | MJPEG 视频流（multipart StreamingResponse） |
| GET | `/api/ptz/status` | JWT | 无 | PTZ 是否启用 + `step_ms` |
| POST | `/api/ptz/move` | JWT | `PtzMoveRequest` | 开始持续转动（按住式） |
| POST | `/api/ptz/stop` | JWT | 无 | 停止转动 |
| POST | `/api/ptz/step` | JWT | `PtzStepRequest` | 步进（点按式，后端自动停转） |

```jsonc
// PtzMoveRequest / PtzStepRequest
{ "direction": "up" }   // up / down / left / right

// /api/ptz/status 返回
{ "enabled": true, "step_ms": 200 }

// /api/state 返回 CameraStateModel（含 presence/action/infer_count 等）
```

`/api/video_feed` 在 handler 内显式校验 JWT（除了全局中间件），返回 `multipart/x-mixed-replace` MJPEG 流。PTZ 走 ONVIF（zeep，`asyncio.to_thread` 包同步调用）。详见《05-摄像头视觉/摄像头接入与配置》。

---

## 10. MCP 与 Agent 状态 /mcp, /agents/status

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/mcp/servers` | JWT | 无 | 已连接外部 MCP server + 全部注册工具 |
| POST | `/api/mcp/servers` | JWT | `MCPConnectRequest` | 运行时连接新 server（白名单校验） |
| DELETE | `/api/mcp/servers/{name}` | JWT | 无 | 断开指定 server |
| GET | `/api/agents/status` | JWT | 无 | 自动化 Agent 状态 |

```jsonc
// MCPConnectRequest
{ "name": "my-server", "cmd": "npx", "args": ["-y", "@some/mcp-server"] }
```

`/api/mcp/servers` 的 POST 仅允许连接 `config.json` 预声明过的 server（白名单防 RCE），连接成功后 60s 超时，自动 `_rebuild_agent`。`/api/agents/status` 只返回**自动化 Agent**状态，**不含调度器**（调度器无独立状态端点）。详见《MCP工具参考》《外部MCP Server集成》。

---

## 11. 高级配置 /advanced

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/advanced/config` | JWT | 无 | 高级配置（网页搜索/视觉/RAG） |
| POST | `/api/advanced/config` | JWT | `AdvancedConfigRequest` | 保存高级配置 |
| GET | `/api/advanced/embed-status` | JWT | 无 | Embed 模型状态 + 各搜索功能可用性 |

```jsonc
// AdvancedConfigRequest（三段均可空，只更新提供的段）
{
  "web_search": { "exa": { "api_key": "可选，留空匿名调用" } },
  "vision": { "downscale_max_side": 448, "jpeg_quality": 70, "motion_hash_size": 16, "motion_threshold": 15, "motion_check_interval_seconds": 0.2, "min_infer_interval_seconds": 3.0, "max_idle_interval_seconds": 60.0, "vision_use_img_count": 3, "frame_interval_ms": 1000 },
  "rag": { "recent_turns": 5, "retrieve_top_k": 6, "retrieve_top_n": 3, "soft_max_turns": 12, "hard_max_turns": 16, "soft_max_tokens": 12000, "hard_max_tokens": 16000, "soft_max_chars": 24000, "hard_max_chars": 32000, "summary_blocks": 2 }
}
```

Exa 搜索 Key 在此页配置（**不是** `/keys` 页），无环境变量。详见《06-集成扩展/Exa网页搜索配置》。

---

## 12. 其他业务接口

### 家庭信息 /home

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/home/info` | JWT+用户 | 无 | 当前用户家庭信息 |
| POST | `/api/home/info` | JWT+用户 | `HomeInfoRequest` | 更新家庭信息 |

```jsonc
// HomeInfoRequest（字段均可空，只更新非空值）
{ "home_name": "我家", "owner_name": "张三", "province": "上海", "city": "上海", "district": "浦东" }
```

> 注意：家庭信息存 `user_settings.home_info`（按用户隔离）。天气查询地则是全局的——天气服务读 `config.json` 的 `home` 段而非 `user_settings.home_info`，家庭场景下一家人共用一个地点，属有意设计，详见《07-个性化/家庭信息与主题设置》。

### 天气 /weather

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/weather` | JWT | 无（query: location） | 天气，15 分钟缓存 |
| GET | `/api/weather/locate` | JWT | 无 | IP 自动定位 |
| GET | `/api/weather/city` | JWT | 无（query: q） | 城市搜索→Location ID |
| GET | `/api/weather/indices` | JWT | 无（query: location） | 生活指数 |
| GET | `/api/weather/config` | JWT | 无 | 天气 API 配置（private_key 脱敏） |
| POST | `/api/weather/config` | JWT | `WeatherConfigRequest` | 保存配置 |

### Emoji /emoji

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/emoji/search` | JWT | 无（query: q） | 语义搜索 emoji，top_k=30 |
| GET | `/api/emoji/preferences` | JWT | 无 | 全部偏好 |
| PUT | `/api/emoji/preferences` | JWT | `EmojiPreferenceRequest` | 保存/更新偏好 |
| DELETE | `/api/emoji/preferences/{scope}/{key}` | JWT | 无 | 删除偏好（恢复默认） |

```jsonc
// EmojiPreferenceRequest
{ "scope": "entity", "key": "light.living_room", "emoji_char": "💡" }
// scope: entity / domain / task_condition / task_action_node / weather
```

### 语音转文字 /stt

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| POST | `/api/stt/transcribe` | JWT | multipart `audio: UploadFile` | 浏览器录音→STT→文字 |

### 个性化 /unique

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/unique` | JWT | 无 | 聊天助手人格/能力/原则 |
| POST | `/api/unique` | JWT | `UniqueSettingsRequest` | 更新（仅 persona） |

```jsonc
// UniqueSettingsRequest
{ "persona": "你是 Aether，一个温暖的家庭助手..." }
```

### 系统健康 /health, /metrics, /setup

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/health` | JWT | 无 | 健康检查 |
| GET | `/api/metrics` | JWT | 无 | 内存指标快照 |
| GET | `/api/setup/status` | JWT | 无 | 初始配置状态（引导） |

`/api/health` 返回 `HealthData`：

```jsonc
{
  "status": "ok",
  "llm_model": "glm-4-flash",
  "llm_enabled": true,
  "llm_available": true,
  "ha_available": true,
  "camera": { /* CameraStateModel */ },
  "log_file": "C:\\...\\logs\\app.log"
}
```

`/api/metrics` 返回 `metrics_service.snapshot()`：请求计数、延迟分位、工具调用计数、LLM 调用计数、自动化评估计数等。

### 文档/RAG /doc, /search

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/output/latest/graph.json` | 公开 | 无 | 最新语义图 |
| GET | `/search` | 公开 | 无（query: q, top_k） | 语义图节点搜索（FAISS 向量检索，回退关键词） |
| POST | `/api/doc/chat` | JWT | `{message}` | RAG 文档助手流式聊天（SSE） |
| GET | `/doc/content` | 公开 | 无（query: doc_id） | 读 docs 下 markdown 内容 |

> **没有 `/docs`**——文档内容接口是 `/doc/content`（无 /api 前缀，公开）。`/api/doc/chat` 是 SSE 流式 RAG 聊天。

### 语义图 /sg

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/sg/config` | JWT | 无 | 当前构建参数（向量模型、LLM 模型、阈值、是否就绪，不含密钥） |
| GET | `/api/sg/status` | JWT | 无 | 构建任务状态（idle/running/done/error、进度、消息） |
| POST | `/api/sg/build` | JWT | 无 | 触发一次语义图构建（异步，立即返回状态，前端轮询 /sg/status） |
| POST | `/api/sg/cancel` | JWT | 无 | 取消正在运行的构建任务 |
| GET | `/api/sg/latest` | JWT | 无 | 最近一次构建的 graph.json（节点/边统计 + 完整图谱） |

> 语义图用你在 `/keys` 配置的 `embed`（向量）和 `chat`（LLM）角色构建，与 RAG 复用同一向量模型，保证维度一致。构建产物存于 `app/sg/output/`，5 步流水线：解析文档 → 向量化 → 实体抽取 → 邻居关系分析 → 导出图。

---

## 13. WebSocket /ws

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| WS | `/ws/chat` | WS | 主聊天流（LangGraph ReAct Agent，事件流推送） |
| WS | `/ws/doc/chat` | WS | 文档助手流（RAG 流水线 + 流式推送） |

### /ws/chat 事件命名空间

连接后客户端发 `{"query":"...","session_id":"可选"}`，服务端推送以下事件（详见《系统架构概述》Dispatcher 事件流）：

| 事件 | 说明 |
| --- | --- |
| `UI.Status` | 状态提示（如"正在思考..."） |
| `Template.TokenStream` | 流式 token |
| `Template.CallTool` | 工具调用开始（工具卡片，始终展示） |
| `Template.CallToolResult` | 工具调用结果 |
| `Dialog.Finish` | 对话完成 |
| `Dialog.Exception` | 异常 |

WebSocket token 校验顺序：query `token` → `aether_token` cookie → `X-API-Token` 头。

---

## 14. 启动期 8011 端口

冷启动期间（LLM 客户端初始化较慢），`scripts/startup_progress.py` 在 **8011** 端口提供临时的启动进度服务，前端轮询展示进度。主服务（8010）就绪后该服务自动退出。详见《系统健康检查指南》。
