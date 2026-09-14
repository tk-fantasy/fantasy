# 邀请码/安装码二维码展示 + 邀请码 24h 过期 — 设计文档

日期：2026-09-14
状态：已批准（方案 A）

## 背景与动机

安装码（首次部署首用户注册用）目前只能从启动日志横幅或 8011 进度接口的裸 JSON 里读，管理员签发邀请码后也需要人工抄 8 位码发给家人。8 位码人工转抄易错、体验差。目标：**扫二维码即可拿码/进注册页**，同时给邀请码加 24 小时自动过期，兜住"码泄露后长期有效"的风险。

## 范围

1. 8011 启动进度页升级：浏览器访问时显示安装码大字 + 二维码
2. 运维页邀请码二维码：未使用的码可弹出大图二维码，内容为注册深链
3. 注册页支持 `?code=` 自动填码
4. 邀请码签发后 24 小时自动过期
5. 后端/前端测试补齐

**不做**（明确排除）：终端 ASCII 二维码、动态轮换二维码、邀请码过期时间可配置、旧码列表清理功能。

## 设计决策

### ① 安装码二维码 — `app/startup_progress.py`

- 二维码**服务端生成**，选 `segno`（纯 Python、零传递依赖、MIT），加入 `requirements.txt`。8011 服务必须在主应用启动前自包含，无法复用前端 JS 包。
- `do_GET` 路由调整：
  - `/progress` 与 `/`：返回极简 HTML 页（内联 CSS，无外部资源）——大字显示安装码、二维码 SVG（data-URI 内嵌）、提示文案。页面 JS 零依赖、零脚本，全部服务端渲染。
  - `/api/startup-progress`：**保持纯 JSON 原样不动**，`LoadingView` 轮询与外部脚本兼容。
- 安装码二维码内容 = **纯文本码**（如 `AB3D-EF7H`）。不拼 URL——8011 页开在部署机 localhost，拼出的链接手机打不开。
- 无码状态（已有用户后 `setup_code` 不在 snapshot 里）：页面显示"已完成初始化，无需安装码"。
- **优雅降级**：segno 导入失败（ImportError）时返回无二维码的纯文字页，不抛错。`do_GET` 内任何渲染异常都 catch 后 fallback 到 JSON/纯文字，绝不阻断进度服务。
- 安全性：不变。二维码展示的信息与现有 JSON 接口完全相同（`snapshot()` 本就含 `setup_code`），无新增暴露面。页面可达性仍由 `_PROGRESS_HOST`（默认 127.0.0.1）控制。

### ② 邀请码二维码 — `frontend/src/views/OperationsView.vue`

- npm 新增 `qrcode` 包（MIT），`QRCode.toDataURL` 客户端生成。
- 列表中**未使用、未吊销、未过期**的码显示「二维码」按钮；点击弹出模态框：大尺寸二维码（方便摄像头对焦）+ 深链 URL 明文 + 码文本。
- 二维码内容 = **注册深链**：`${window.location.origin}/login?mode=register&code=${encodeURIComponent(code)}`。
  - 用浏览器地址栏地址而非后端传值，因为服务端不可靠知道自己的局域网 IP。
- **localhost 检测**：`location.hostname` 为 `localhost`/`127.0.0.1` 时，模态框内显示警示"当前是 localhost 地址，手机扫码打不开，请用局域网 IP 访问本页"。

### ③ 注册页自动填码 — `frontend/src/views/LoginView.vue`

- 现有 `?mode=register` 逻辑（`onMounted`）旁增加：`route.query.code` 存在时写入 `inviteCode.value`（trim + 转字符串）。
- 二维码深链固定同时带 `mode=register` 和 `code=`，扫开即注册模式 + 已填码。

### ④ 邀请码 24 小时过期 — `app/services/invite_service.py`

- `create_invite`：新增字段 `expires_at = created_at + _INVITE_TTL_MS`（模块常量 `24 * 3600 * 1000`，不做配置项）。
- `verify_registration_code`：命中码后，`revoked_at`/`used_at` 之外再判 `expires_at`——已过期视同无效。**报错文案维持统一一句**（"邀请码无效或已被使用"），不区分不存在/已用/已吊销/已过期，维持无侧信道原则。
- **向后兼容**：存量码无 `expires_at` 字段 → 视为永不过期。过期判定统一为 `expires_at` 非零且 `now > expires_at`；字段缺失按 0 处理，自然放行。
- 吊销、列表逻辑不变；列表条目自然多出 `expires_at` 字段。

### 运维页状态显示 — `inviteStatus()`

现有三态（未使用/已使用/已吊销）增加**已过期**：`expires_at` 非零且 < now 且未使用未吊销 → 显示"已过期"。列表中每枚未过期未用的码显示剩余有效期。

## 数据结构变化

邀请码条目新增一个字段（KV JSON 内，无建表/迁移）：

```json
{
  "code": "AB3D-EF7H",
  "note": "给妈妈",
  "created_by": "dad",
  "created_at": 1757800000000,
  "expires_at": 1757886400000,
  "used_by": "",
  "used_at": 0,
  "revoked_at": 0
}
```

## 测试计划

- **后端** `tests/test_invite_service.py` 补用例：create_invite 含 expires_at；未过期码可注册；过期码 403 且文案与无效码一致；无 expires_at 的存量条目不受影响。
- **后端** 8011 页面：`/progress` 返回 HTML 且含安装码与 SVG；`/api/startup-progress` 仍为 JSON；无 setup_code 时显示初始化完成文案（用直接调用 handler 或起测试端口的方式）。
- **前端** `frontend/tests/views/LoginView.test.js` 补用例：`?mode=register&code=X` 自动切注册模式且填码。
- **前端** OperationsView：模态框深链拼接与 localhost 警示（视 jsdom 兼容性，最低限度测 URL 生成函数）。

## 风险与权衡记录

- **仓库出现两个二维码实现**（segno + npm qrcode）：结构性代价——8011 必须自包含，Vue 端必须拼浏览器地址。两端均为单一用途小依赖，接受。
- **localhost 二维码扫不开**：用页内警示缓解，不做自动探测局域网 IP（不可靠）。
- **过期兜底而非防泄露**：24h 过期限定泄露码的最长存活期，防的是"长期潜伏"不是"当场使用"；后者由一次性 + 吊销覆盖。
