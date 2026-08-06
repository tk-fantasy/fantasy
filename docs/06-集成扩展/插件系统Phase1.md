# Aether 集成插件系统 (Phase 1)

## 概述

Phase 1 实现了工业级插件系统的骨架：每个集成是**独立子进程**，通过 stdio JSON-RPC 与 Aether 通信。Aether 助手的文字回复会广播到所有声明了 `output_sink` 能力的插件。

**已验证的真实场景**：用户在 Aether 对话，小爱音箱同步播报回复。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                   Aether 宿主进程                         │
│                                                          │
│  Dispatcher ──final_content──▶ SinkManager.broadcast()   │
│                                     │                    │
│                                     │ fan-out            │
│                                     ▼                    │
│                            PluginSupervisor              │
│                                     │                    │
│                          ┌──────────┴───────────┐        │
│                          ▼                      ▼        │
│                    PluginProcess          PluginProcess  │
│                    (stdio JSON-RPC)        (启动失败退避重试) │
└──────────────────────────┬───────────────────────────────┘
                           │ stdin/stdout (JSON-RPC 2.0)
                           ▼
                ┌─────────────────────┐
                │  插件子进程          │
                │  - 读 manifest       │
                │  - 实现 OutputSink   │
                │  - 通过反向调用控制硬件│
                └─────────────────────┘
```

## 核心组件

| 文件 | 职责 |
|------|------|
| `app/integration/rpc_protocol.py` | JSON-RPC 2.0 消息构造/解析纯函数 |
| `app/integration/schema.py` | Manifest 的 Pydantic 数据模型 |
| `app/integration/manifest_loader.py` | 扫描 integrations/ 目录、校验清单 |
| `app/integration/plugin_process.py` | 单插件进程的 spawn + stdio + 请求响应配对 |
| `app/integration/plugin_supervisor.py` | 多进程生命周期 + 指数退避重启 + 熔断 |
| `app/integration/sink_manager.py` | 并发广播到所有 output_sink |
| `app/integration/integration_layer.py` | 门面，组装上述组件，挂 AppContainer |
| `app/integration/sdk/` | 插件 SDK（OutputSink/IntegrationPlugin/stdio runtime） |
| `app/routes/integration_routes.py` | `/api/integrations` 管理路由 |

## 如何添加一个插件

1. 在 `integrations/` 下建子目录（如 `integrations/myplugin/`）
2. 写 `manifest.json`：
   ```json
   {
       "id": "myplugin",
       "name": "我的插件",
       "version": "1.0.0",
       "aether_api_version": "1",
       "entry": "plugin.py",
       "capabilities": [{"type": "output_sink", "id": "m1", "priority": 100}]
   }
   ```
3. 写 `plugin.py`：
   ```python
   import asyncio, sys
   from app.integration.sdk.plugin_base import IntegrationPlugin
   from app.integration.sdk.sink_base import OutputSink

   class MySink(OutputSink):
       async def speak(self, text, msg_id=""):
           # 实现播报逻辑
           return {"spoken": text}

       async def interrupt(self):
           return {"interrupted": True}

   class MyPlugin(IntegrationPlugin):
       def setup(self, manifest_dict):
           self.sinks = [MySink()]

   if __name__ == "__main__":
       from app.integration.sdk.stdio_runtime import run_stdio_plugin
       asyncio.run(run_stdio_plugin(MyPlugin, sys.argv[1]))
   ```
4. 重启 Aether，插件自动被发现并启动

### 为新音箱写播报插件

插件系统已解耦——加新音箱**不改 Aether 主代码**，只在 `integrations/` 扔一个文件夹。但每个音箱仍要写各自的插件（调它的 API）。路径取决于音箱怎么被控制：

| 音箱接入方式 | 插件怎么写 | 凭证 |
|------------|-----------|------|
| 已接入 HA（media_player 实体，支持 PLAY_MEDIA / TTS） | 插件调 `tts.speak` 或 `media_player.play_media` | manifest 声明 `secrets: ["ha_url","ha_token"]`，宿主自动注入 |
| 已接入 HA，但只支持 notify 实体念字（如小爱 LX06） | 插件调 `notify.send_message`（小爱即此路） | 同上 |
| 有局域网 HTTP API（如 Sonos） | 插件进程内直连音箱 API，不经 HA | manifest config_schema 让用户填音箱 IP/端口 |
| 只有云 API + 账号登录 | 插件持有云凭证，调厂商云接口 | secrets 扩展新类型（如 `sonos_token`），宿主映射注入 |

**解耦的边界**：框架让"加插件不改主代码"，但不免去"为每个音箱写那个插件"的工作——这和 VSCode 插件系统同理（装 Python 插件不改 VSCode，但 Python 插件得有人写）。

### 解耦的验证标准

加第二个音箱时，问自己：**改了 `app/main.py` 吗？改了 `app/integration/` 任何文件吗？**
- 改了 → 解耦失败，是设计漏洞
- 没改（只加了 `integrations/xxx/`）→ 解耦成功

凭证注入已通用化（按 manifest `secrets` 声明统一注入，宿主不认识具体插件名），所以"需要 HA 凭证的音箱"加进来零主代码改动。

## 配置

`config.json` 的 `integration` section：

| 键 | 默认值 | 说明 |
|----|--------|------|
| `enabled` | `false` | 是否启用插件平台（代码兜底默认关；`config.example.json` 里显式写了 `true`） |
| `plugin_dir` | `integrations` | 插件目录（相对项目根） |
| `api_version` | `1` | 契约版本（不匹配的插件被跳过） |
| `default_rpc_timeout` | `30` | RPC 调用超时（秒） |
| `max_restarts` | `3` | 崩溃重启上限（超过后熔断） |

## 小爱插件（`integrations/xiaoai/`）

### 工作原理

通过 HA 的 `xiaomi_home` 集成（非 xiaomi_miot）暴露的 notify 实体做 TTS：

- **播报**：`notify.send_message(entity_id=notify.<dev>_play_text_a_5_1, message=文字)`
- **打断**：`media_player.media_stop(entity_id=media_player.<dev>)`

### 关键发现（踩坑记录）

设计初期假设小爱用 `xiaomi_miot.intelligent_speaker` 服务，实测发现：
- HA 实际生效的是 `xiaomi_home` 集成（xiaomi_miot 文件存在但未注册服务）
- 小爱 media_player 的 `supported_features=17469` 不含 `PLAY_MEDIA`，无法接收 HA 标准 TTS 音频流
- 正确路径是 `xiaomi_home` 暴露的 notify 实体（play_text / execute_text_directive）

### 软件串行锁

Aether 自己的多次 speak 通过 `asyncio.Lock` + `asyncio.Queue` 排队，保证不并发占用小爱。
**外部程序**（米家 app、HA 自动化等）对小爱的控制不在此锁范围——Aether 不霸道。

## 韧性机制

| 机制 | 说明 |
|------|------|
| 指数退避重试 | 插件**启动失败**时 1s→2s→4s…（封顶 30s）退避重试（借鉴 K8s CrashLoopBackOff）。注意：进程启动**成功之后**崩溃没有检测/重启机制，靠外部手动重启 |
| 启动熔断 | 超过 `max_restarts` 次启动尝试失败后放弃启动该插件（日志记「已熔断」；无 disabled 标记，插件列表里仍显示 `alive=false`） |
| 单点失败隔离 | 单个插件启动失败不阻塞其他插件 |
| 优雅关闭 | 停止时三级流程：发 `shutdown` RPC 通知（3s 超时）→ `terminate()` → 2s 仍不退再 `kill()` |
| 广播容错 | 单个 sink 广播失败仅记日志，不阻塞主聊天流程 |

## API

- `GET /api/integrations` — 列出所有插件及运行状态
- `POST /api/integrations/broadcast` — 手动触发广播（测试用，body: `{"text": "...", "msg_id": "..."}`）

## 已知限制（Phase 1）

- 仅支持方向 1（Aether → 插件）单向 RPC；Phase 3 补方向 2（插件反向调 Aether）
- 无心跳熔断（进程卡死但未退出的检测）；Phase 5 补
- 插件级配置表单未做（manifest `config_schema` 的动态表单 UI）；小爱广播的开关按钮已通过 UI 贡献机制实现（见下节）


## 前端 UI 贡献机制

插件不仅贡献后端能力，还能贡献前端 UI——**且 UI 不硬编码在主代码里，由 manifest 声明、Aether 通用渲染器渲染**。没装插件 → 前端无该 UI → 主代码八竿子打不着。

### 声明方式

manifest.json 加 `ui_contributions`：

```json
"ui_contributions": [{
  "slot": "chat_input_toolbar",
  "type": "toggle_button",
  "props": {"icon_on": "🔊", "icon_off": "🔇", "title_on": "...", "title_off": "..."},
  "state_key": "broadcast_enabled",
  "action": "toggle_broadcast"
}]
```

| 字段 | 说明 |
|------|------|
| `slot` | UI 槽位（如 `chat_input_toolbar`），前端在对应位置放 `<IntegrationSlot>` 占位 |
| `type` | 预定义类型：`toggle_button` / `icon_button` / `status_badge`（不写 Vue 代码） |
| `props` | UI 展示参数（icon/title 等） |
| `state_key` | 状态读取 key（`GET /api/integrations/state/{key}`） |
| `action` | 点击触发动作（`POST /api/integrations/action/{name}`） |

### 关键设计：state/action 归属框架，不属插件

广播开关（`broadcast_enabled`）是 `SinkManager` 的通用状态——**这是插件系统框架的能力，不属于小爱插件**。小爱插件只声明"我要在 UI 放个按钮控制这个通用开关"，连开关逻辑都不持有。

Aether 维护 `STATE_HANDLERS` / `ACTION_HANDLERS` 注册表，**插件只能用已注册的 state_key/action**——安全边界（插件不能任意触发未注册动作）。

### 通用组件

- `IntegrationSlot.vue`：读 `/api/integrations/ui_contributions`，按 `slot` 过滤，按 `type` 渲染对应通用组件。无贡献时渲染空。
- `ToggleButtonContribution.vue`：读 `state_key` 显示开/关态，点击 POST `action`，切换状态。用 Aether 现有 CSS 变量，不认得小爱。
- `ChatView.vue` input-row 只放 `<IntegrationSlot slot="chat_input_toolbar" />` 占位——**无小爱/音箱/broadcast 字眼**。

### 解耦验证

删 `integrations/xiaoai/` 后：
- `list_ui_contributions()` 返回空 → 前端 `<IntegrationSlot>` 渲染空 → `/chat` 无喇叭按钮 ✅
- ChatView.vue 无"小爱/音箱/broadcast"字眼 ✅
- state/action 注册表无 `xiaoai` 字样，全是框架通用能力 ✅


## 插件管理页面

在 `/chat` 输入 `/plugin` 跳转到插件管理页面，支持：

| 操作 | 说明 |
|------|------|
| 查看列表 | 所有插件 + 状态（运行中/未启动/已禁用）+ 能力徽标 |
| 启用/禁用 | 热加载：禁用立即停进程，启用立即热启动（不重启 Aether）。状态持久化到 config |
| 导出 | 打包 `integrations/{id}/` 为 zip 下载（分享给别人用） |
| 上传 | 拖拽 zip 上传，校验 manifest + entry + id 合法 + 冲突，原子解压到 `integrations/` |
| 删除 | 删 `integrations/{id}/` 文件夹（运行中的先停进程） |

### 上传安全

子进程插件 = 任意代码执行。安全靠**认证**（谁能上传）而非**内容审计**（上传什么）：
- 上传需登录（api_token_guard）
- manifest.id 正则校验（防路径穿越 `../`）
- entry 文件必须存在于 zip 内
- 同名插件已存在 → 拒绝
- 解压失败回滚（删临时目录）

⚠️ 不做代码内容审计——子进程插件本质信任代码，审计 Python 查恶意成本高且不可靠。

### 管理路由

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/integrations` | GET | 列表 + 状态（含 enabled） |
| `/api/integrations/{id}/toggle-enabled` | POST | 启用↔禁用 |
| `/api/integrations/{id}/export` | GET | 打包 zip 下载 |
| `/api/integrations/upload` | POST | 上传 zip 校验+解压 |
| `/api/integrations/{id}` | DELETE | 删除插件 |

## 后续 Phase

- **Phase 2**：全局打断（W3）+ 小爱直通模式（W2）+ 插件配置表单 UI
- **Phase 3**：双向 RPC（反向调用 + 权限白名单）
- **Phase 4**：飞书机器人（W4）
- **Phase 5**：心跳熔断（进程卡死检测）+ 崩溃自动重启 + 依赖图拓扑启动
