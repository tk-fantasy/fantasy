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
│                    (stdio JSON-RPC)        (崩溃退避重启) │
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

## 配置

`config.json` 的 `integration` section：

| 键 | 默认值 | 说明 |
|----|--------|------|
| `enabled` | `true` | 是否启用插件平台 |
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
| 指数退避重启 | 插件崩溃后 1s→2s→4s 退避重启，避免重启风暴（借鉴 K8s CrashLoopBackOff） |
| 熔断 | 超过 `max_restarts` 后停止重试，插件标记 disabled |
| 单点失败隔离 | 单个插件启动失败不阻塞其他插件 |
| 广播容错 | 单个 sink 广播失败仅记日志，不阻塞主聊天流程 |

## API

- `GET /api/integrations` — 列出所有插件及运行状态
- `POST /api/integrations/broadcast` — 手动触发广播（测试用，body: `{"text": "...", "msg_id": "..."}`）

## 已知限制（Phase 1）

- 仅支持方向 1（Aether → 插件）单向 RPC；Phase 3 补方向 2（插件反向调 Aether）
- 无心跳熔断（进程卡死但未退出的检测）；Phase 5 补
- 无优雅关闭三级流程（RPC→TERM→KILL）的完整实现；Phase 5 补
- 配置 UI 未做；Phase 2 补前端

## 后续 Phase

- **Phase 2**：全局打断（W3）+ 小爱直通模式（W2）+ ChatView UI
- **Phase 3**：双向 RPC（反向调用 + 权限白名单）
- **Phase 4**：飞书机器人（W4）
- **Phase 5**：心跳熔断 + 优雅关闭 + 依赖图拓扑启动
