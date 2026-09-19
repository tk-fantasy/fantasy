# 飞书扫码一键接入 — 设计文档

日期：2026-09-19
状态：已与用户确认设计，待实施

## 1. 背景与目标

Aether 已有飞书机器人集成（`integrations/feishu/`，WebSocket 长连接，宿主侧加载）。当前接入流程要求用户手动到飞书开放平台建应用、加机器人能力、开权限、订阅事件、发版，再回管理页粘贴 App ID / App Secret，门槛高、步骤多。

目标：在插件管理页的飞书配置弹窗提供「扫码一键接入」——用户用飞书 App 扫一次二维码并在手机上确认，凭证自动写入 Aether 并热拉起长连接，全程不接触 App ID / Secret。手动填凭证的现有方式原样保留，作为兜底。

## 2. 调研结论（实现依据）

- OpenClaw（开源 AI 助手）的飞书插件已验证了「扫码自动创建机器人」：扒其 npm 包 `@openclaw/feishu` 源码（`dist/.setup/app-registration-*.mjs`，原始出处 `extensions/feishu/src/app-registration.ts`），它调用飞书账号体系的设备授权式应用注册端点，轮询拿到 `client_id/client_secret`。
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
- Aether 侧落地条件现成：`httpx` 已有；前端 `qrcode` npm 包已在 `OperationsView.vue` 使用；`set_host_config` + `container.restart_host_integration_fn("feishu", loop)` 构成现成的「写配置 + 热重连」链路（与插件配置保存同一条）。

## 3. 方案选型

- **已选**：扫码一键 + 手动兜底（本设计）。
- 落选 A：仅引导向导 + 自检——不动未公开端点、零风险，但用户仍要手动建应用，「一键」程度低。
- 落选 B：项目预注册公共飞书应用走正规 OAuth 授权码流——依赖创建应用 API 的公开性（存疑）且要求项目长期维护公共应用身份，链路最长、风险面最大。

## 4. 后端设计

### 4.1 新模块 `app/integration/feishu_qr_setup.py`

纯函数式小模块，封装与 `accounts.feishu.cn` 的交互与会话状态，不 import 飞书插件，便于单测。

- `QrSetupSession`：模块级单会话（家用单管理员场景足够；再次 start 覆盖前一会话）。字段：`device_code`、`qr_url`、`user_code`、`expires_at`、`interval`（秒，默认 5）、`last_poll_ts`、`consecutive_errors`。
- `start_session(domain="feishu") -> dict`：init（校验 client_secret）→ begin → 建会话，返回 `{"qr_url", "user_code", "expires_in", "interval"}`。任何一步失败抛 `FeishuQrSetupError`（带用户可读文案）。
- `poll_once() -> dict`：
  - 无会话或已过 `expires_at` → `{"status": "expired"}`（过期的同时清会话）；
  - 距 `last_poll_ts` 不足 `interval` → 直接回会话上记录的 `last_status`（初值 `pending`），不请求飞书（限速）；
  - 请求飞书一次，按第 2 节映射为 `pending / scanned / denied / expired / success`；
  - 网络异常或响应结构异常 → `consecutive_errors += 1`，累计 < 5 回 `pending`，≥ 5 回 `{"status": "error", "message": "飞书扫码通道异常，请稍后重试或改用手动配置"}`；
  - `success` 结果含 `app_id`、`app_secret`（仅内存传递，不写日志）。
- `cancel_session()`：清会话。
- `consume_result() -> tuple[str, str] | None`：供路由层在 success 后取走凭证并清会话（取走即失效，防重复消费）。
- 超时等常量：默认 `interval=5`、`expires_in=3600`（以 begin 实际返回为准，缺失时用默认）。

### 4.2 路由（`app/routes/integration_routes.py`，均 `Depends(get_current_admin)`）

| 路由 | 行为 |
|------|------|
| `POST /api/integrations/feishu/qr_setup/start` | 调 `start_session()`；成功 `{"success": True, "data": {qr_url, user_code, expires_in, interval}}`；失败 `{"success": False, "message": 友好文案（引导切手动配置）}` |
| `GET /api/integrations/feishu/qr_setup/poll` | 调 `poll_once()`；`success` 时：`set_host_config("feishu", {原配置 + app_id + app_secret})` → `container.restart_host_integration_fn("feishu", loop)`（与配置保存同链路，返回字段带 `applied: "restarted"|"saved"`）→ `consume_result()` 清会话。响应 `{"success": True, "data": {status, applied?, app_id_masked?}}`，**secret 不出后端**。`denied/expired/error` 附 `message` |
| `POST /api/integrations/feishu/qr_setup/cancel` | `cancel_session()`，`{"success": True}` |

覆盖语义：已有凭证时扫码成功 → 覆盖 `app_id/app_secret`，其余字段（`notify_chat_id`、`verification_token`、`encrypt_key`）经「读原配置再合并」保留。

### 4.3 范围决定

- 只支持飞书（`accounts.feishu.cn` / `open.feishu.cn`）。Lark 国际版自动切换（poll 响应 `tenant_brand`）列为后续增强，本次不做。
- 不做 `application/v6` 查询 owner open_id（OpenClaw 用于 DM 白名单；Aether 当前不绑定飞书身份与 Aether 账号，YAGNI，列为后续增强）。

## 5. 前端设计（`frontend/src/views/PluginManageView.vue`）

- 飞书（`plugin_id === "feishu"`）配置弹窗顶部加 tab：「⚡ 扫码一键接入」/「手动配置」。打开时按 `openConfig` 已拉到的配置判断：`app_id` 有值或 secret `is_set` 为真 → 默认手动 tab，否则默认扫码 tab。tab 为该弹窗内特例实现（带注释说明原因），不做通用机制。
- 扫码 tab：
  - 进入时调 `start`，用 `qrcode` 的 `QRCode.toDataURL(qr_url)` 渲染二维码；同时展示 `user_code`（扫码失败时可手输）。
  - 每 5 秒调 `poll`（与后端限速对齐），文案随状态：等待扫码 → 已扫码，请在手机上确认 → ✓ 已接入，机器人已启动。显示剩余时间倒计时。
  - `expired` / `error` / `denied`：提示 + 「重新获取二维码」按钮 + 「改用手动配置」切换链接。
  - `success`：刷新插件列表状态（沿用 saveConfig 成功后的刷新路径），自动切回列表视角。
  - 弹窗关闭或切走 tab：清定时器，调 `cancel`（后端清会话）。
- 手动 tab：现有表单与保存逻辑原样不动。

## 6. 错误处理汇总

| 场景 | 行为 |
|------|------|
| start 时飞书端点不可达/结构变化 | 路由回友好文案，前端引导切手动（手动路径永远保留，属架构决定而非临时兜底） |
| poll 网络抖动 | 计入连续错误，<5 次当 pending，≥5 次报通道异常 |
| 1 小时未扫 | expired，前端可一键重取 |
| 用户手机端拒绝 | denied，前端提示可重来 |
| restart_host_integration_fn 未注入/插件未运行 | `applied: "saved"`，前端按现有「配置已保存，但插件未在运行」口径提示 |

## 7. 安全

- 三个路由均要求管理员（与配置保存一致）。
- `device_code` / 凭证只在内存，成功或取消即清；日志不落 secret。
- poll 路由按 `interval` 限速，前端轮询不会放大为对飞书的请求风暴。
- secret 不回传前端；配置落库后走现有脱敏回显（`GET config` 的 `masked` 机制）。

## 8. 测试计划

- 新增 `tests/test_feishu_qr_setup.py`（模块级，monkeypatch httpx）：init 校验、begin 参数与会话、poll 各状态映射（pending/scanned/denied/expired/success/slow_down）、限速路径、连续错误阈值、consume 一次性。
- 扩展 integration_routes 相关测试（沿用 `tests/test_routes_core_coverage.py` / `test_routes_extra.py` 现有模式）：start 成功/失败响应、poll success 触发 `set_host_config` + restart fn（monkeypatch 捕获）、鉴权 401/403、cancel。
- 前端：浏览器 GUI 走查 tab 切换、二维码渲染、轮询状态文案、取消清理（真实扫码一步人工完成）。

## 9. 风险与兜底

- 注册端点是飞书未公开 API，未来可能变更或收紧——手动配置是一等公民路径，任何扫码链路故障都不影响接入。
- `PersonalAgent` 型应用私聊仅限扫码者本人（群内 @ 是否可用未验证，文档中注明「以飞书实际表现为准」）。

## 10. 后续增强（本次不做）

- Lark（国际版）`tenant_brand` 自动切换。
- 扫码成功后经 `application/v6` 拉取 owner `open_id`，为「飞书身份 ↔ Aether 账号」绑定铺路。
- 扫码通道自检按钮（探测端点可用性）。
