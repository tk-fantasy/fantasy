# API 鉴权安全设置

Aether 现在用 **JWT 多用户鉴权**：每个用户独立账号，登录后拿 access/refresh 两种 token，自动续期。早期版本的单一 `APP_TOKEN` 仍作为兜底保留，但主流程已经是 JWT。

## 一、要不要开登录

Aether 默认就需要登录——首次使用要先**注册一个账号**。这是多用户隔离的基础：每个用户的 LLM Key、会话、家庭信息、Emoji 偏好都各自独立。

- 一个人在家用 → 注册一个账号就够
- 家里多人用 → 每人一个账号，互不干扰
- 想远程访问 → 务必配强密码 + HTTPS

## 二、JWT 机制

登录成功后，服务端在浏览器种两个 **httpOnly Cookie**（前端 JS 读不到，防 XSS 窃取）：

| Cookie | 类型 | 有效期 | 作用 |
|--------|------|--------|------|
| `aether_token` | access | 24 小时 | 访问 API 的凭证 |
| `aether_refresh_token` | refresh | 7 天 | access 过期后用它换新 |

- 算法 `HS256`，密钥从 `JWT_SECRET` 环境变量读；没设则自动生成并存到 `app\data\.jwt_secret`（持久化，重启不丢 token）；都没有则每次启动随机（重启后所有 token 失效，会告警）。
- 密码用 `pbkdf2_sha256` 哈希存储，不明文。
- access 过期后，前端自动调 `/api/auth/refresh`（用 refresh cookie）换新的一对，用户无感。

## 三、注册与登录

**首次使用**：打开前端，会跳到登录页，点「注册」创建第一个账号。

| 接口 | 方法 | 限流 | 说明 |
|------|------|------|------|
| `/api/auth/register` | POST | 3 次/分钟 | 注册新用户，初始化该用户的 `llm_keys`/`providers`/`home_info`，同时种 cookie |
| `/api/auth/login` | POST | 5 次/分钟 | 登录，验证密码后种 cookie |
| `/api/auth/refresh` | POST | — | 用 refresh cookie 换新的 access+refresh |
| `/api/auth/logout` | POST | — | 清除两个 cookie |
| `/api/auth/me` | GET | — | 查看当前登录用户（需已登录） |

> 注册接口的限流是为了防爆破。第一个注册的用户**预留**了管理员字段，但目前没启用特殊权限。

## 四、哪些接口需要登录

几乎所有 `/api/*` 都需要有效的 JWT。**例外**（免登录）：

- `/api/auth/*`（登录、注册、刷新、登出本身）
- 前端静态页面、`/assets/*`、`/`（SPA 本身能打开）
- `/api/output/latest/graph.json`（语义图公开数据）

也就是说：**`/api/health`、`/api/state`、`/api/agents/status`、`/api/video_feed` 全都要登录才能访问。** 这点要特别注意——如果你想做 Docker/k8s 的无认证健康探针，目前没有现成的免认证端点，得自己加或用 cookie。

### WebSocket 鉴权

`/ws/chat` 和 `/ws/doc/chat` 不走 HTTP 中间件，单独在握手时验 token，优先级：

1. `token` 查询参数（`/ws/chat?token=xxx`）
2. `aether_token` cookie
3. `Authorization: Bearer` 请求头

前端 WebSocket 连接默认带 cookie，所以正常使用不用管。

## 五、APP_TOKEN 兜底（旧方式）

如果你在 `.env` 里设了 `APP_TOKEN`，它会作为 JWT 之外的**兜底认证**：

- HTTP 请求带 `X-API-Token: <token>` 请求头 → 通过
- WebSocket 带 `X-API-Token` 头或 `app_token` 查询参数 → 通过

兜底通过时 `user_id` 为空，走默认配置。这主要用于脚本/自动化调 API 时不想走完整登录的场景。

> 日常使用不需要设 `APP_TOKEN`，JWT 登录就够了。它只是兼容老脚本的逃生通道。

## 六、多用户隔离

每个用户的下列数据完全独立，互不可见：

- LLM 密钥（`llm_keys`）和角色绑定（`providers`）
- 聊天会话
- 家庭信息（`home_info`）

下列数据则是一家人共享的（家庭场景有意设计，不做用户隔离）：

- Emoji 偏好（表里有 `user_id` 列但查询未过滤，实际全局共享）
- 定时任务、天气查询地

切换用户用 `POST /api/users/switch`，会重载 LLM 客户端、重写 `.env` 密钥、重发 cookie。

## 七、安全建议

1. **强密码**：注册时用强密码，别用 `123456`。
2. **设 `JWT_SECRET`**：在 `.env` 里设一个固定的随机串，否则靠 `app\data\.jwt_secret` 兜底（也可以，但迁移机器要带上这个文件）。
3. **HTTPS**：远程访问务必上 HTTPS（Tailscale + HTTPS 或反向代理），否则 cookie 可能被窃听。
4. **防火墙**：只对外开必要端口。

| 端口 | 服务 | 对外建议 |
|------|------|----------|
| 8010 | 后端 API + 前端 | 可对外（走 HTTPS） |
| 8123 | Home Assistant | 仅内网 |
| 1884 | MQTT | 仅内网 |
| 5173 | Vite 开发服务器 | 仅本机（生产环境不存在） |
| 8011 | 启动进度服务 | 仅本机，冷启动期间临时存在 |

5. **限流已内置**：全局 120 次/分钟/IP（`/ws/*` 和 `/api/auth` 除外），登录 5 次/分钟，注册 3 次/分钟。

## 八、CORS

后端只允许来自 `localhost` + 内网网段（`10.*`/`192.168.*`/`172.16-31.*`）+ Tailscale（`100.64.*`/`10.*`）的跨域请求，且 `allow_credentials=True`。外部域名直接调 API 会被拦。

## 九、排查

| 现象 | 原因 |
|------|------|
| 调 API 返回 401「未认证」 | access cookie 过期且 refresh 失败 → 重新登录 |
| 频繁被踢下线 | `JWT_SECRET` 每次启动变了（没设 env 也没持久化文件）→ 设固定 `JWT_SECRET` |
| WebSocket 连不上（1008） | token 无效 → 检查 cookie/查询参数 |
| 脚本调 API 401 | 没带 cookie → 用 `X-API-Token` + `APP_TOKEN` 兜底，或模拟登录拿 cookie |
