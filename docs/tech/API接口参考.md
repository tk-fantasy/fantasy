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

> **多用户隔离范围**：`user_id` 隔离**会话历史**、**个人 LLM keys/providers/home_info**，以及 `chat`/`summary`/`stt` 三个角色的运行时客户端（主对话 agent、Validator、定时任务 reminder、自动化规则 build_rule/context-only 评估都按 `user_id` 解析 chat key，见 `resolve_key_for_role_user`）。`vision`/`embed` 角色仍全局共享。切换用户会 `reload_all_clients` 重载全局客户端。详见《系统架构概述》。

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
| GET | `/api/ha/entity-aliases` | JWT | 无 | 全部实体别名映射 `{entity_id: alias}` |
| PUT | `/api/ha/entity-aliases` | JWT | `EntityAliasRequest` | 设置/删除实体别名（空串=删除；同步到 HA entity_registry.name，HA 失败则回滚） |
| GET | `/api/ha/entity-notes` | JWT | 无 | 全部实体备注映射 `{entity_id: note}`（用户自定义，注入 LLM 认知） |
| PUT | `/api/ha/entity-notes` | JWT | `EntityNoteRequest` | 设置/删除实体备注（空串=删除；只存 Aether DB，不同步 HA） |
| GET | `/api/ha/services` | JWT | 无 | HA 服务定义 `{domain:{service:{fields,required}}}` |
| POST | `/api/ha/call_service` | JWT | `HAServiceCallRequest` | 调用 HA 服务（经控件范围探测保底） |
| GET | `/api/ha/config` | JWT | 无 | HA 配置（token 脱敏） |
| POST | `/api/ha/config` | JWT | `HAConfigRequest` | 保存 HA 配置 |
| POST | `/api/ha/test` | JWT | 无 | 测试 HA 连接 |
| GET | `/api/ha/entity-operable` | JWT | 无 | AI 操作黑名单（被禁止 AI 操作的实体 `{entity_id: "0"}`） |
| PUT | `/api/ha/entity-operable` | JWT | `EntityOperableRequest` | 设置/恢复实体 AI 可操作权限，立即刷新 catalog |
| GET | `/api/ha/action-maps` | JWT | 无 | 全部动作语义映射 `{entity_id: {mappings}}` |
| PUT | `/api/ha/action-maps` | JWT | `ActionMapRequest` | 设置/更新实体映射（空 mappings=删除；target 须属该域且≠源 service） |
| GET | `/api/ha/entity-services` | JWT | 无 | 按域分组的可用服务列表（供语义映射页拉取） |

```jsonc
// HAServiceCallRequest
{ "domain": "light", "service": "turn_on", "entity_id": "light.living_room", "data": {} }
// HAConfigRequest
{ "url": "http://homeassistant:8123", "token": "长期访问令牌" }
// EntityAliasRequest / EntityNoteRequest
{ "entity_id": "switch.door_relay", "alias": "大门开关" }   // alias/note 留空表示删除
// EntityOperableRequest
{ "entity_id": "lock.front_door", "operable": false }   // false=禁止 AI 操作；true=恢复
// ActionMapRequest
{ "entity_id": "switch.door_relay",
  "mappings": {
    "turn_on":  { "target": "turn_off", "description": "继电器反向：开=关门" },
    "turn_off": { "target": "turn_on",  "description": "继电器反向：关=开门" }
  } }
```

> `entity_operable` 黑名单与 `entity_action_map` 映射都存 `emoji_preferences` 表（scope 隔离）。`call_service` 执行链：entity_id 存在性校验 → **operable 黑名单硬拦截** → query→entity 语义校验（防语义近邻顶替）→ **语义映射无条件替换 service**（批量需共识）→ state 隐含翻转（对称翻转对自动 on↔off 反转，`get_entities`/`get_device_manual`/catalog 同步翻转）。详见《03-设备控制/设备语义映射》《03-设备控制/AI设备操作权限》。

> **没有 `/api/ha/devices`**——设备数据通过 `/api/ha/entities` 获取。HA 集成通过官方镜像 `homeassistant/home-assistant:stable` 运行，详见《MQTT设备接入协议》。备注（entity_note）与别名（entity_alias）都存 `emoji_preferences` 表按 scope 隔离，备注只影响 AI 认知（注入后台目录缓存/规则生成/工具返回），别名同步到 HA 并影响界面显示。

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

### 5.1 全局 LLM 配置 /global/* （二级密码门禁）

全局 key 存 `config.json` 顶层 `llm_keys`，所有用户共享。`vision`/`embed` 历史上就是全局；`chat`/`summary`/`stt` 可在 per-user 配置里切到全局兜底（`use_global` flag）。

**门禁规则**：写操作（设置/改/删全局 key、改全局 settings）必须带二级密码；读操作和重置密码只需 JWT。

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/global/password/status` | JWT | 无 | 查询是否已设置二级密码（不暴露哈希） |
| POST | `/api/global/password` | JWT | `SecondaryPasswordSetupRequest` | **首次设置**二级密码；已设置返回 409 |
| POST | `/api/global/password/verify` | JWT | `SecondaryPasswordVerifyRequest` | 验证二级密码（无状态，前端解锁用） |
| DELETE | `/api/global/password` | JWT | 无 | **重置**二级密码（不验证原密码，设计如此——丢了密码的自救入口） |
| GET | `/api/global/llm_keys` | JWT | 无 | 全局 key 列表（隐藏明文 api_key） |
| POST | `/api/global/llm_keys` | JWT + 二级密码 | `GlobalLLMKeyRequest` | 新增/更新全局 key（新增时自动测试连接） |
| DELETE | `/api/global/llm_keys/{key_id}` | JWT + 二级密码 | 无（`?password=` query） | 删除全局 key（保留 .env 中的密钥） |
| GET | `/api/global/llm/settings` | JWT | 无 | 全局 providers（角色→key_id 映射） |
| POST | `/api/global/llm/settings` | JWT + 二级密码 | `GlobalLLMSettingsRequest` | 给全局角色指定 key |

```jsonc
// SecondaryPasswordSetupRequest（首次设置，至少 6 位）
{ "password": "..." }
// SecondaryPasswordVerifyRequest
{ "password": "..." }
// GlobalLLMKeyRequest（password=二级密码；新增时 api_key 必填，编辑时留空表示不改密钥）
{ "id": "", "base_url": "https://...", "model": "glm-4-flash", "type": "chat", "api_key": "...", "chat_path": "/chat/completions", "embed_path": "/v1/embeddings", "password": "二级密码" }
// GlobalLLMSettingsRequest
{ "role": "chat", "key_id": "k1", "password": "二级密码" }
```

**二级密码丢失自救**：忘了二级密码无需手改 config.json——直接调 `DELETE /api/global/password` 清除，然后重新 `POST /api/global/password` 设新密码即可。清除期间已配置的全局 key 仍在，但任何修改全局 key 的写操作都会被拒（要求先设新密码）。

**全局 chat key 热重载**：改全局 `chat` 角色后自动 `_rebuild_agent()` 重建主对话 agent（`GLOBAL_KEY_HOT_RELOAD=True`）；rebuild 失败返回 `restart_required=true` 提示重启。其他角色（vision/embed/summary/stt）走 `_reload_key_pools` 即可。

**per-user `use_global` flag**：在 per-user `POST /api/llm/settings`（第 5 节）提交，仅对 `chat`/`summary`/`stt` 有效。`use_global=true` 时该角色走全局兜底，同时 `key_id` 被清空；`key_resolver` 解析时先判 `use_global` 再查 per-user key，避免被旧 per-user key 拦截。

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

### 6.1 自动化引擎调参 /automation

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/automation/status` | JWT | 无 | 自动化 Agent 状态（运行中、静默评估开关/间隔、冷却、dhash 阈值、评估计数） |
| POST | `/api/automation/silent` | JWT | `{enabled, interval_seconds}` | 静默评估开关 + 周期（事件触发 + 定时器兜底） |
| POST | `/api/automation/vision-recognizer` | JWT | `{enabled}` | 视觉识别器开关（当前 AI 预览路的 VL 预览开关，解耦自动化） |
| POST | `/api/automation/cooldown` | JWT | `{cooldown_seconds}` | 默认冷却时间 |
| POST | `/api/automation/dhash-threshold` | JWT | `{threshold}` | 调全局 `vision.motion_threshold` + 热更新旧单摄主路（滑块拉满 = 关 dhash 退化为定时器）。多摄像头下每路阈值存 `cameras` 表，不受此接口影响 |

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
| POST | `/api/scheduled-tasks/{task_id}/revise` | JWT | `{text}` | 自然语言修订任务（LLM 解析出新的 schedule/payload） |
| PUT | `/api/scheduled-tasks/{task_id}` | JWT | `ScheduledTaskCreateRequest` | 直接更新任务 |
| POST | `/api/scheduled-tasks/{task_id}/explain` | JWT | 无 | LLM 解释任务（下次触发时间等） |

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

## 8. 视觉关注项（按摄像头） /cameras/{id}/focuses

关注项自多摄像头化后**按摄像头分桶**（`camera_id=""` 空桶为全局/兼容旧数据），管理入口在 `/cameras` 页的配置弹窗：

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/cameras/{camera_id}/focuses` | JWT | 无 | 某路摄像头的全部关注项 |
| POST | `/api/cameras/{camera_id}/focuses` | JWT | `{text}` | 新增一条 |
| PUT | `/api/cameras/{camera_id}/focuses/{focus_id}` | JWT | `{text?, enabled?}` | 更新（字段均可空） |
| DELETE | `/api/cameras/{camera_id}/focuses/{focus_id}` | JWT | 无 | 删除 |

```jsonc
// 创建/更新请求
{ "text": "猫是否在沙发上" }          // 新增
{ "enabled": false }                 // 仅启停
```

> 旧全局接口 `/api/vision/focus`、`/api/vision/focuses` 仍保留（`settings_routes.py`），无 `camera_id` 时读写空桶，仅供兼容。关注项用于 `classify_frame` 分类提示词注入 + 系统提示词注入，OR 关系。持久化在 `vision_focuses` KV（扁平 list 含 `camera_id` 字段）。详见《05-摄像头视觉/视觉关注项配置》。

---

## 9. 摄像头（多路） /cameras

多摄像头由 `CameraManager`（`app/services/camera_manager.py`）统一管理，配置存数据库 `cameras` 表（老单摄配置首次启动自动迁移成一行）。`/api/state`、`/api/video_feed` 为兼容旧入口（返回主摄像头）。

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/cameras` | JWT | 无 | 列出全部摄像头 |
| POST | `/api/cameras` | JWT | `CameraCreateRequest` | 新增（enabled=1 立即启动 worker） |
| GET | `/api/cameras/{camera_id}` | JWT | 无 | 单路详情 |
| PUT | `/api/cameras/{camera_id}` | JWT | `CameraUpdateRequest` | 更新（重建该路 stream） |
| DELETE | `/api/cameras/{camera_id}` | JWT | 无 | 删除 |
| GET | `/api/cameras/{camera_id}/video_feed` | JWT | 无 | 该路 MJPEG 流 |
| POST | `/api/cameras/{camera_id}/test-stream` | JWT | 无 | RTSP 试连 |
| POST | `/api/cameras/{camera_id}/display/enable`·`disable` | JWT | 无 | AI 预览开关（单路互斥，enable 会自动停旧路） |
| GET | `/api/cameras/{camera_id}/state` | JWT | 无 | 该路状态 `CameraStateModel` |
| POST | `/api/cameras/{camera_id}/ptz/move`·`stop`·`step` | JWT | `{direction}` | 该路云台控制 |
| POST | `/api/cameras/{camera_id}/discovery/find` | JWT | 无 | ONVIF 发现（按 MAC 扫描子网） |
| POST | `/api/cameras/{camera_id}/discovery/manual-ip` | JWT | `{ip}` | 手动更新摄像头 IP |
| GET | `/api/ha/areas` | JWT | 无 | HA 区域列表（摄像头归属下拉） |

```jsonc
// 摄像头字段（cameras 表）
{ "id": "cam_xxx", "name": "客厅", "enabled": 1, "source_type": "rtsp|usb",
  "usb_index": 0, "rtsp_url": "rtsp://...", "rtsp_username": "", "rtsp_password": "",
  "area": "", "device_mac": "", "discovery_enabled": 1,
  "ptz_enabled": 0, "ptz_ip": "", "ptz_port": 80, "ptz_username": "", "ptz_password": "",
  "ptz_speed": 0.5, "ptz_step_ms": 300, "display_enabled": 1,
  "motion_hash_size": 16, "motion_threshold": 15, "motion_check_interval": 1.0,
  "vision_min_infer_interval": 8.0, "vision_max_idle_interval": 120.0,
  "vision_use_img_count": 3, "frame_interval_ms": 2000 }
```

### 9.1 旧单摄接口 /state, /video_feed

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/state` | JWT | 无 | 主摄像头状态 `CameraStateModel` |
| GET | `/api/video_feed` | JWT | 无 | 主摄像头 MJPEG 视频流 |

```jsonc
// /api/state 返回 CameraStateModel（含 presence/action/infer_count 等）
```

> 旧全局 PTZ 路由（`/api/ptz/*`）已删除——云台配置收敛到 per-camera（`cameras` 表的 `ptz_*` 字段），控制走 `POST /api/cameras/{camera_id}/ptz/move|stop|step`。新前端已全部走 `/api/cameras/*`（含每路 PTZ）。`/api/video_feed` 在 handler 内显式校验 JWT（除了全局中间件）。PTZ 走 ONVIF（zeep，`asyncio.to_thread` 包同步调用）。详见《05-摄像头视觉/摄像头接入与配置》。

---

## 9.2 ONVIF 摄像头发现 /discovery

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/discovery/status` | JWT | 无 | 发现服务状态 |
| POST | `/api/discovery/find` | JWT | `{target_mac?, subnet?, timeout?}` | 扫描子网找摄像头（camera_id 空时用旧配置） |
| POST | `/api/discovery/manual-ip` | JWT | `{new_ip}` | 手动指定 IP |

> 多路模式优先走 `/api/cameras/{id}/discovery/*`（按 cameras 行读 MAC/子网/凭证）；worker 掉线连续开流失败时也会自动触发发现找回 IP。

---

## 10. MCP 与 Agent 状态 /mcp, /automation/status

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/mcp/servers` | JWT | 无 | 已连接外部 MCP server + 全部注册工具 |
| POST | `/api/mcp/servers` | JWT | `MCPConnectRequest` | 运行时连接新 server（白名单校验） |
| DELETE | `/api/mcp/servers/{name}` | JWT | 无 | 断开指定 server |
| GET | `/api/automation/status` | JWT | 无 | 自动化 Agent 状态（运行中/静默评估/dhash 阈值/冷却/评估计数） |

```jsonc
// MCPConnectRequest
{ "name": "my-server", "cmd": "npx", "args": ["-y", "@some/mcp-server"] }
```

`/api/mcp/servers` 的 POST 仅允许连接 `config.json` 预声明过的 server（白名单防 RCE），连接成功后 60s 超时，自动 `_rebuild_agent`。`/api/automation/status` 只返回**自动化 Agent**状态，**不含调度器**（调度器无独立状态端点）。详见《MCP工具参考》《外部MCP Server集成》。

---

## 11. 高级配置 /advanced

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/advanced/config` | JWT | 无 | 高级配置（网页搜索/视觉/RAG） |
| POST | `/api/advanced/config` | JWT | `AdvancedConfigRequest` | 保存高级配置 |
| GET | `/api/advanced/embed-status` | JWT | 无 | Embed 模型状态 + 各搜索功能可用性 |
| POST | `/api/advanced/test/exa` | JWT | `{api_key}` | 测试 Exa 搜索连通性 |
| POST | `/api/advanced/test/rtsp` | JWT | `{url, username, password}` | 测试 RTSP 流连通性 |

```jsonc
// AdvancedConfigRequest（三段均可空，只更新提供的段）
{
  "web_search": { "exa": { "api_key": "可选，留空匿名调用" } },
  "vision": { "downscale_max_side": 448, "jpeg_quality": 70, "min_infer_interval_seconds": 3.0, "read_retry_count": 3, "read_retry_interval_seconds": 0.1, "release_cooldown_seconds": 0.8, "max_backoff_seconds": 15.0, "rtsp_transport": "tcp" },
  "rag": { "recent_turns": 5, "retrieve_top_k": 6, "retrieve_top_n": 3, "soft_max_turns": 12, "hard_max_turns": 16, "soft_max_tokens": 12000, "hard_max_tokens": 16000, "soft_max_chars": 24000, "hard_max_chars": 32000, "summary_blocks": 2 }
}
```

> vision 段：per-camera 配置（rtsp_url/motion_threshold/ptz 等）已迁至 `cameras` 表，此段只含全局 VLM 编码/采集鲁棒性参数。摄像头本身在 `/cameras` 页管。

Exa 搜索 Key 在此页配置（**不是** `/models` 页），无环境变量。详见《06-集成扩展/Exa网页搜索配置》。

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
| GET | `/api/emoji/search` | JWT | 无（query: q, top_k 默认 20） | 语义搜索 emoji |
| GET | `/api/emoji/preferences` | JWT | 无 | 全部偏好 |
| PUT | `/api/emoji/preferences` | JWT | `EmojiPreferenceRequest` | 保存/更新偏好（无 scope 白名单） |
| DELETE | `/api/emoji/preferences/{scope}/{key}` | JWT | 无 | 删除偏好（恢复默认） |
| POST | `/api/emoji/rebuild` | JWT | 无 | 重建 emoji 向量索引（后台异步；需 embed Key；重建中重复触发返回 409） |
| GET | `/api/emoji/rebuild/status` | JWT | 无 | 重建进度（running/total/done/errors/message） |

```jsonc
// EmojiPreferenceRequest
{ "scope": "device", "key": "light.living_room", "emoji_char": "💡" }
// scope: device（/halist 设备图标）/ scheduled_task（定时任务图标）/ task_condition / task_action_node / weather / entity_alias / entity_note
//   entity_alias = 设备别名（同步到 HA entity_registry.name）
//   entity_note   = 设备备注（只存 Aether，注入 AI 认知，不同步 HA）
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

### 虚拟设备开关 /simulator

前提：aether 容器挂载了宿主 docker.sock（compose 已默认挂载）。socket 不可用时接口返回 `available: false`，前端隐藏开关。

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/api/simulator/status` | JWT | simulator + mosquitto 容器运行状态 |
| POST | `/api/simulator/stop` | JWT + 管理员 | 停止虚拟设备容器，并刷新设备视图（模拟器设备离线隐藏） |
| POST | `/api/simulator/start` | JWT + 管理员 | 启动虚拟设备容器，并刷新设备视图 |

> 停止后模拟器设备状态变 `unavailable`，设备列表按「全部实体离线才隐藏」规则过滤（演示设备整体消失，真实设备不受影响）。入口在「高级」页「虚拟设备」段。

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

---

## 15. 运维中心 /ops

> 所有运维接口需要管理员权限（`Depends(get_current_admin)`），操作写入审计日志。

### 诊断包导出

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/ops/diagnostics` | JWT + 管理员 | 下载脱敏诊断包（zip） |
| POST | `/api/ops/diagnose` | JWT + 管理员 | 运行部署体检，返回结构化报告 |
| GET | `/api/ops/audit` | JWT + 管理员 | 最近运维审计记录（最多 50 条） |
| DELETE | `/api/ops/audit` | JWT + 管理员 | 清空审计日志（清空动作留一条 audit_clear 记录） |

**诊断包内容**：
- 脱敏后的 `config.json`（密钥字段打码）
- 系统信息（uname、docker ps、磁盘/内存）
- 最近日志（每文件尾部 2MB，总量 10MB）

**部署体检项**：
- 端口占用（8010、8123、1884）
- 网络连通性（HA、RTSP、DNS）
- 资源检查（磁盘 > 2GB、内存 > 2GB）
- 时间同步（NTP）

### 版本与升级

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/ops/version` | JWT + 管理员 | 当前版本 + 升级历史 |
| GET/PUT | `/api/ops/update/git` | JWT + 管理员 | git 一键升级配置（Gitee 令牌只回 configured，不回明文） |
| POST | `/api/ops/update/git/check` | JWT + 管理员 | git 检查更新（fetch + 比对当前/远程 commit） |
| POST | `/api/ops/update/git/apply` | JWT + 管理员 | git 一键升级（拉取→重建→健康自检→失败回退，长请求） |
| GET | `/api/ops/update/git/status` | JWT + 管理员 | git 升级最近结果 + 日志尾 |
| POST | `/api/ops/update-pack/export` | JWT + 管理员 | 一键导出当前版本为升级包（后台任务） |
| GET | `/api/ops/update-pack/export/status` | JWT + 管理员 | 导出进度（running/done/error） |
| GET | `/api/ops/update-pack/download` | JWT + 管理员 | 下载导出的升级包 |
| GET | `/api/ops/update-pack/local` | JWT + 管理员 | 扫描 backups/ 已投放的升级包 |
| POST | `/api/ops/update-pack/local/{name}/apply` | JWT + 管理员 | 安装本地升级包（校验→load→重启→删包） |

```jsonc
// GET /api/ops/version 返回
{
  "version": "1.0.0",
  "docker_socket": "true",
  "history": [
    { "from": "0.9.0", "to": "1.0.0", "at": "2026-08-16T10:00:00Z" }
  ]
}
```

**升级流程**：上传 → 校验 sha256 → docker load → 切换 tag → 健康检查（180s）→ 失败自动回滚

### 在线更新

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/ops/update/settings` | JWT + 管理员 | 读更新源地址 |
| POST | `/api/ops/update/settings` | JWT + 管理员 | 写更新源地址 |
| GET | `/api/ops/update/check` | JWT + 管理员 | 检查是否有新版本 |
| POST | `/api/ops/update/apply` | JWT + 管理员 | 一键升级（下载 → 校验 → 升级） |

```jsonc
// 更新源配置
{ "manifest_url": "https://your-server.com/update-channel.json" }

// GET /api/ops/update/check 返回
{
  "has_update": true,
  "latest_version": "1.1.0",
  "current_version": "1.0.0",
  "changelog": "### Added\n- 新功能 A\n- 新功能 B"
}
```

### 备份与恢复

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/ops/backups` | JWT + 管理员 | 备份列表 |
| POST | `/api/ops/backups` | JWT + 管理员 | 立即备份（保留最近 3 份） |
| DELETE | `/api/ops/backups/{name}` | JWT + 管理员 | 删除指定备份 |
| GET | `/api/ops/backups/{name}/validate` | JWT + 管理员 | 恢复前预检 |
| POST | `/api/ops/backups/{name}/restore` | JWT + 管理员 | 恢复并自动重启 |

```jsonc
// POST /api/ops/backups 返回
{
  "name": "aether-backup-20260816-100000.tar.gz",
  "size_mb": 15.2,
  "created_at": "2026-08-16T10:00:00Z"
}

// 恢复请求（必须 confirm=true）
{ "confirm": true }
```

**备份内容**：`config.json` + `.env` + HA 配置 + MQTT 配置 + aether-data 数据卷

---

## 16. 数据出网策略 /egress

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/egress` | JWT | 获取当前策略状态 |
| POST | `/api/egress` | JWT | 设置出网模式 |
| POST | `/api/egress/confirm` | JWT | 确认数据流向声明 |

```jsonc
// GET /api/egress 返回
{
  "mode": "cloud",           // cloud / hybrid / local
  "mode_label": "云端对话",
  "confirmed": true,
  "confirmed_at": "2026-08-15T10:00:00Z",
  "confirmed_by": "alice",
  "endpoints": [
    { "role": "chat", "base_url": "https://api.openai.com/v1", "private": false, "configured": true },
    { "role": "vision", "base_url": "http://ollama:11434/v1", "private": true, "configured": true }
  ],
  "warnings": [],
  "notes": []
}

// POST /api/egress 设置模式
{ "mode": "local" }

// POST /api/egress/confirm 确认声明
// （body 为空，用户信息从 JWT 提取）
```

**三档模式**：
- `cloud`：云端模式（默认），对话走 HTTPS 到模型厂商
- `hybrid`：混合模式，对话走云端，敏感角色建议内网
- `local`：纯内网模式，所有端点必须内网，公网端点硬拦截

---

## 17. 文档/RAG /doc, /search

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/output/latest/graph.json` | 公开 | 无 | 最新语义图 |
| GET | `/search` | 公开 | 无（query: q, top_k） | 语义图节点搜索（FAISS 向量检索，回退关键词） |
| POST | `/api/doc/chat` | JWT | `{message}` | RAG 文档助手流式聊天（SSE） |
| POST | `/api/doc/rebuild` | JWT | 无 | 重建文档向量索引（异步，后端线程池执行） |
| GET | `/api/doc/rebuild/status` | JWT | 无 | 重建进度轮询 |
| GET | `/doc/content` | 公开 | 无（query: doc_id） | 读 docs 下 markdown 内容 |

```jsonc
// /api/doc/rebuild/status 返回
{
  "rebuilding": true,           // 是否在重建
  "total": 100, "done": 42,     // 进度（done/total，前端画进度条）
  "errors": 0,                  // 失败文档数
  "message": "正在向量化文档...", // 阶段提示（启动中/向量化/完成）
  "model": "BAAI/bge-m3",       // 当前 embed 模型
  "chunk_count": 312            // 已生成 chunk 数
}
```

> **没有 `/docs`**——文档内容接口是 `/doc/content`（无 /api 前缀，公开）。`/api/doc/chat` 是 SSE 流式 RAG 聊天。重建进度 UI 在「模型」页（`/models`，改 embed 模型后提示重建）和「高级」页（`/advanced` 文档向量重建段）两处展示。

### 语义图 /sg

| 方法 | 路径 | 认证 | Body | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/sg/config` | JWT | 无 | 当前构建参数（向量模型、LLM 模型、阈值、是否就绪，不含密钥） |
| GET | `/api/sg/status` | JWT | 无 | 构建任务状态（idle/running/done/error、进度、消息） |
| POST | `/api/sg/build` | JWT | 无 | 触发一次语义图构建（异步，立即返回状态，前端轮询 /sg/status） |
| POST | `/api/sg/cancel` | JWT | 无 | 取消正在运行的构建任务 |
| GET | `/api/sg/latest` | JWT | 无 | 最近一次构建的 graph.json（节点/边统计 + 完整图谱） |

> 语义图用你在 `/models` 配置的 `embed`（向量）和 `chat`（LLM）角色构建，与 RAG 复用同一向量模型，保证维度一致。构建产物存于 `app/sg/output/`，5 步流水线：解析文档 → 向量化 → 实体抽取 → 邻居关系分析 → 导出图。

---

## 13. 集成插件配置 /integrations

插件管理页（/plugin）点插件卡片打开详情弹窗：无 `config_schema` 的只显示描述；
有的渲染配置表单（`type: secret` 字段脱敏回显、保存留空=保持原值）。

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/integrations` | JWT | 插件列表（含 `config_schema` 与 `has_config_set`） |
| POST | `/api/integrations/{id}/config` | JWT + 管理员 | 保存配置并热生效（写审计） |
| GET | `/api/integrations/{id}/config` | JWT | 读配置表单数据（secret 只回 `is_set` + 脱敏值） |

保存后自动生效方式：宿主侧集成（飞书）stop+start 热重连；子进程插件（小爱）重启进程，
`AETHER_PLUGIN_CONFIG` 环境变量注入，SDK 覆盖 manifest `config_schema` 默认值。
配置持久化在 `config.json → integration.host_configs.<插件id>`；未配置时飞书回退
`.env`（`FEISHU_APP_ID` 等），老部署无需迁移。

```jsonc
// GET /api/integrations/feishu/config 返回
{
  "schema": { "app_id": {"type": "string", "required": true, "label": "App ID"},
              "app_secret": {"type": "secret", "required": true, "label": "App Secret"} },
  "values": { "app_id": "cli_aaaabbbb",
              "app_secret": {"is_set": true, "masked": "DsFx…gSOy"} },
  "has_config_set": true
}
```

---

## 14. WebSocket /ws

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

## 15. 启动期 8011 端口

冷启动期间（LLM 客户端初始化较慢），`scripts/startup_progress.py` 在 **8011** 端口提供临时的启动进度服务，前端轮询展示进度。主服务（8010）就绪后该服务自动退出。详见《系统健康检查指南》。
