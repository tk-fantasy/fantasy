# 飞书扫码一键接入 — 设计文档

日期：2026-09-19
状态：设计已确认（v2：二维码面板在配置弹窗内呈现，全链路插件化解耦），待实施

## 1. 背景与目标

Aether 已有飞书机器人集成（`integrations/feishu/`，WebSocket 长连接，宿主侧加载）。当前接入流程要求用户手动到飞书开放平台建应用、加机器人能力、开权限、订阅事件、发版，再回管理页粘贴 App ID / App Secret，门槛高、步骤多。

目标：在插件管理页的飞书配置弹窗内提供「扫码一键接入」——用户用飞书 App 扫一次二维码并在手机上确认，凭证自动写入 Aether 并热拉起长连接，全程不接触 App ID / Secret。手动填凭证的现有方式原样保留，作为兜底。

架构要求（用户确认）：

1. 扫码面板必须在配置弹窗（modal）内呈现；
2. 全链路按插件机制实现、与核心解耦——删掉 `integrations/feishu/` 目录，扫码功能连同其 UI、接口、会话状态整体消失，核心零残留。

## 2. 调研结论（实现依据）

- OpenClaw（开源 AI 助手）的飞书插件已验证了「扫码自动创建机器人」：其 npm 包 `@openclaw/feishu` 源码（`dist/.setup/app-registration-*.mjs`，原始出处 `extensions/feishu/src/app-registration.ts`）调用飞书账号体系的设备授权式应用注册端点，轮询拿到 `client_id/client_secret`。
- 该端点为 `POST https://accounts.feishu.cn/oauth/v1/app/registration`（`application/x-www-form-urlencoded`），**不在飞书公开 API 文档中**，但对任意客户端开放。2026-09-19 实测可用。
- 线上协议（三步）：
  1. **init**：`action=init` → `{"nonce": "...", "supported_auth_methods": ["private_key_jwt", "client_secret"]}`。校验 `client_secret` 在列，否则中止。
  2. **begin**：`action=begin&archetype=PersonalAgent&auth_method=client_secret&request_user_info=open_id` → `{"device_code": "...", "user_code": "P85M-4GNR", "verification_uri": "https://open.feishu.cn/page/launcher", "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=P85M-4GNR", "expires_in": 3600, "interval": 5}`。二维码内容即 `verification_uri_complete`（不加 OpenClaw 的 `from`/`tp` 埋点参数）。
  3. **poll**：`action=poll&device_code=...`，按 `interval` 轮询：
     - 未扫：`{"error": "authorization_pending"}` → pending
     - `{"error": "slow_down"}` → 间隔 +5s，当 pending
     - 用户拒绝：`{"error": "access_denied"}` → denied
     - 超时：`{"error": "expired_token"}` → expired
     - 成功：`{"client_id": "cli_...", "client_secret": "...", "user_info": {"open_id": "...", "tenant_brand": ...}}`。poll 响应中出现 `user_info` 而尚无 `client_id` 时可视为「已扫码，待确认」→ scanned。
- **产品限制（接受）**：该流程创建的是 `PersonalAgent`（个人代理）型应用，**私聊仅对扫码者本人开放**，适合家用场景；全员可用的需求走手动建应用的老路（保留不删）。
- Aether 侧落地条件现成：`httpx` 已有；前端 `qrcode` npm 包已在 `OperationsView.vue` 使用；`PluginSlot.vue` 已具备 `import.meta.glob('../../../../integrations/*/frontend/*.vue')` 动态加载插件前端组件的能力；`set_host_config` + `container.restart_host_integration_fn` 构成现成的「写配置 + 热重连」链路。

## 3. 方案选型

- **已选**：扫码一键 + 手动兜底（本设计）。
- 落选 A：仅引导向导 + 自检——不动未公开端点、零风险，但用户仍要手动建应用，「一键」程度低。
- 落选 B：项目预注册公共飞书应用走正规 OAuth 授权码流——依赖创建应用 API 的公开性（存疑）且要求项目长期维护公共应用身份，链路最长、风险面最大。

## 4. 总体架构

```
frontend（通用，不认识飞书）            app/（核心，不认识飞书）
PluginManageView 配置弹窗              integration_routes /integration/{id}/method/{m}
  └─ <PluginSlot slot="plugin_config_modal">   │ 通用桥接：宿主集成 call_method 优先，
       └─ 动态加载                              │ 子进程插件回落；config_changed → 热重启
          integrations/feishu/frontend/FeishuQrSetup.vue
              │ POST /api/integrations/feishu/method/qr_start|qr_poll|qr_cancel
              ▼
          integrations/feishu/main.py  call_method（方法白名单分发）
              └─ qr_setup.py（设备授权三步 + 单会话状态）
                    └─ config_helper.set_host_config("feishu", …)
```

核心侧只新增/扩展**通用机制**（不出现任何插件名）；策略与状态全部在插件目录内。

## 5. 核心侧：通用机制扩展

### 5.1 宿主集成可贡献 UI
- `main.py::_load_host_integration_meta`：meta.py 若声明 `UI_CONTRIBUTIONS`，并入注册信息。
- `integration_layer.py::list_ui_contributions`：遍历 `self.host_integrations`，把其中的 `ui_contributions` 按与 manifest 相同的形状（`plugin_id/slot/type/props/state_key/action`，缺省为空）并入返回。约 10 行。

### 5.2 method RPC 桥接宿主集成
- `register_host_integration` 的 info 携带 `call_method` 句柄（main.py 注册时放入；`_restart_host_integration` 重注册自然刷新句柄）。
- `integration_routes.py::call_plugin_method`：先查 `layer.host_integrations[plugin_id].get("call_method")`，有则 `await call_method(method, params)`；无则回落现有 supervisor 子进程路径。框架方法黑名单（`_FRAMEWORK_METHODS`）与管理员鉴权对两类一视同仁。

### 5.3 `config_changed` 约定（核心的唯一"聪明"）
宿主集成方法修改了自身配置，可在返回 data 顶层声明 `config_changed: True`；桥接层看到即调 `container.restart_host_integration_fn(plugin_id, loop)`，并在响应 data 附 `applied: "restarted"`（重启异常时 `"saved"`）。核心只认约定，不认识飞书。

### 5.4 配置弹窗通用渲染
`PluginManageView.vue` 的 AdvancedModal 内容里（插件徽标行之下、配置表单之上）插入 `<PluginSlot slot="plugin_config_modal" />`。`PluginSlot` 组件本身无需改动：无贡献时 `v-if` 渲染为空，对其他插件零影响。

## 6. 插件侧（全部位于 `integrations/feishu/`）

### 6.1 `qr_setup.py`（新）
设备授权客户端 + 单会话状态（家用单管理员场景，重复 start 覆盖前会话）。

- `QrSetupSession`：`device_code`、`qr_url`、`user_code`、`expires_at`、`interval`（秒，默认 5）、`last_poll_ts`、`last_status`（初值 `pending`）、`consecutive_errors`。
- `start_session() -> dict`：init（校验 client_secret）→ begin → 建会话，返回 `{qr_url, user_code, expires_in, interval}`。失败抛 `FeishuQrSetupError`（用户可读文案）。
- `poll_once() -> dict`：
  - 无会话或已过 `expires_at` → `{"status": "expired"}`（过期同时清会话）；
  - 距 `last_poll_ts` 不足 `interval` → 直接回 `last_status`，不请求飞书（限速）；
  - 请求飞书一次，按第 2 节映射 `pending / scanned / denied / expired / success`，成功结果含 `app_id`、`app_secret`（仅内存传递，不写日志）；
  - 网络异常/结构异常 → `consecutive_errors += 1`，< 5 回 `pending`，≥ 5 回 `{"status": "error", "message": "飞书扫码通道异常，请稍后重试或改用手动配置"}`。
- `cancel_session()`；`consume_result() -> tuple[str, str] | None`（取走凭证并清会话，取走即失效）。

### 6.2 `main.py` 增 `call_method(method, params) -> dict`
方法白名单：`qr_start` / `qr_poll` / `qr_cancel`，其余一律拒绝。

- `qr_start` → `start_session()`，返回其结果；
- `qr_poll` → `poll_once()`；`success` 时：读 `get_host_config("feishu")` 合并新 `app_id/app_secret` 后 `set_host_config`（保留 `notify_chat_id` 等其余字段）→ `consume_result()` → 返回 `{"status": "success", "config_changed": True, "app_id_masked": "cli_xxx***"}`（secret 不出后端）；
- `qr_cancel` → `cancel_session()`。

### 6.3 `meta.py`
增 `UI_CONTRIBUTIONS = [{"slot": "plugin_config_modal", "type": "custom_component"}]`。

### 6.4 `frontend/FeishuQrSetup.vue`（新）
自包含面板组件（风格沿用现有 `setting-row`/`btn-primary` 类）：

- 挂载时经 `GET /api/integrations/feishu/config` 自查接入态：`app_id` 有值或 secret `is_set` → 折叠显示「已接入 ✓ / 重新扫码」，否则展开扫码流程；
- 扫码流程：`qr_start` → `QRCode.toDataURL(qr_url)` 渲染 + 显示 `user_code`（扫码不可用时手输备用）→ 每 5 秒 `qr_poll` → 文案随状态：等待扫码 → 已扫码，请在手机上确认 → ✓ 已接入，机器人已启动；显示剩余时间倒计时；
- `expired` / `denied` / `error`：提示 + 「重新获取二维码」按钮（手动表单就在弹窗下方，文案引导可用）；
- `success`（响应含 `applied`）：派发 `window` CustomEvent `aether:plugins-changed`，`PluginManageView` 监听并刷新插件列表（通用约定）；
- 组件卸载或用户点取消：清定时器 + `qr_cancel`。

## 7. 用户流程（修订后）

打开插件管理 → 飞书卡片「配置」→ 弹窗顶部即扫码面板（未接入时展开）→ 飞书 App 扫码 → 手机确认 → 面板「✓ 已接入，机器人已启动」，列表自动刷新运行态；下方手动表单原样保留（改配置 / 全员可用场景走这里）。

## 8. 错误处理汇总

| 场景 | 行为 |
|------|------|
| qr_start 时飞书端点不可达/结构变化 | method 返回友好文案，面板提示；手动表单就在弹窗下方（架构决定的常驻兜底） |
| qr_poll 网络抖动 | 连续错误 <5 当 pending，≥5 报通道异常 |
| 1 小时未扫 | expired，面板一键重取 |
| 用户手机端拒绝 | denied，面板提示可重来 |
| `config_changed` 后热重启异常 | 桥接层回 `applied: "saved"`，面板按「凭证已保存，但插件未运行」口径提示 |

## 9. 安全

- method 路由现有 `get_current_admin` + `_FRAMEWORK_METHODS` 黑名单对宿主集成方法同样生效；插件内另有白名单二次收敛。
- `device_code` / 凭证只在内存，成功或取消即清；日志不落 secret；secret 不回传前端（落库后走现有脱敏回显）。
- poll 按 `interval` 限速，前端轮询不会放大为对飞书的请求风暴。

## 10. 测试计划

- **桥接机制**（`tests/` 现有 integration_routes 测试模式）：宿主集成 `call_method` 分发成功、`config_changed` 触发 `restart_host_integration_fn` 且响应带 `applied`、框架方法拦截、无 `call_method` 时子进程路径不回归、`list_ui_contributions` 含宿主集成贡献、删除 feishu 目录后机制空转（回归）。
- **qr_setup 模块**（monkeypatch httpx）：init 校验、begin 参数与会话、poll 各状态映射（pending/scanned/denied/expired/success/slow_down）、限速路径、连续错误阈值、consume 一次性、config 合并保留其余字段。
- **前端**：浏览器 GUI 走查面板展开/折叠、二维码渲染、轮询状态文案、取消清理（真实扫码一步人工完成）。

## 11. 风险与兜底

- 注册端点是飞书未公开 API，未来可能变更或收紧——手动配置是一等公民路径，任何扫码链路故障都不影响接入；且扫码整体位于插件目录，必要时可整目录回退。
- `PersonalAgent` 型应用私聊仅限扫码者本人（群内 @ 是否可用未验证，以飞书实际表现为准）。

## 12. 后续增强（本次不做）

- Lark（国际版）`tenant_brand` 自动切换。
- 扫码成功后经 `application/v6` 拉取 owner `open_id`，为「飞书身份 ↔ Aether 账号」绑定铺路。
- 扫码通道自检按钮（探测端点可用性）。
