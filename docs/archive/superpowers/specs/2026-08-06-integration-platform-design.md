# Aether 集成平台（小爱 + 飞书 + 插件系统）设计文档

**日期**: 2026-08-06
**状态**: 待评审
**作者**: brainstorming 协作产出

---

## 1. 背景与动机

### 1.1 用户诉求

三个原初诉求：

1. **小爱作为 Aether 的音频输出**：用户说"打开床头灯"，Aether 文字回复"床头灯已打开"的同时，小爱同步播报。
2. **Aether 切换模式打字控小爱**：用户在 Aether `/chat` 里切到"小爱模式"，直接打字（如"播放周杰伦的歌"），文字原样转给小爱原生执行，不经过 Aether LLM。
3. **飞书机器人**：Aether 接入飞书，用户可在飞书 @机器人 或私聊，消息转给 Aether LLM，回复发回飞书。

### 1.2 为什么选工业级插件系统（方案 C）

本设计不采用"最小改动硬接"（方案 A）或"轻量抽象"（方案 B），而选择**完整的插件/集成系统（方案 C）**。理由：

- **真实工程价值的优先级**：这是一个用于面试展示的项目，需要体现工业级架构设计能力。插件系统的每个坑点（隔离、生命周期、IPC、权限）都是面试官会追问的点。
- **"不硬编码 + 支持所有音频"的诉求**：本质上要求抽象层，方案 A 做不到，方案 B 的抽象过轻。
- **成本可控**：V1 只有 2 个集成（小爱、飞书），插件机制代码约 700 行，换"扔文件夹即可扩展"的能力，ROI 可接受。

### 1.3 关键约束

- **零硬编码**：小爱播报通过 HA 现成的 `xiaomi_miot.intelligent_speaker` 服务调用，不直连小米协议。
- **复用现有传输**：IPC 协议复用 MCP 的 stdio JSON-RPC，不重新发明轮子。
- **贴合现有代码风格**：plain class + `AppContainer` DI + config section per feature。

---

## 2. 决策记录（brainstorming 已确认）

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 架构方向 | **方案 C：完整插件系统** | 工业级标准答案，体现架构深度 |
| 隔离模型 | **子进程隔离（契约模型）** | 进程级崩溃隔离，复用 MCP stdio 传输 |
| 能力契约 | **多能力 + 优先级** | 完整覆盖输出/入站/命令能力，优先级做容错仲裁 |
| 分发方式 | **本地目录扫描** | `integrations/` 目录约定，无需市场 |
| 通信方向 | **双向（插件 ↔ Aether）** | 插件可反向调 Aether API（如查设备状态） |
| 小爱入站模式 | **直通小爱原生**（`execute=true`） | 用户显式选模式，文字原样转小爱，不进 LLM |
| 输出广播范围 | **全部回复都念** | 无需意图分类，`final_content` 全量广播 |
| 多音箱支持 | **V1 单音箱（小爱）** | `OutputSink` 抽象保留扩展性，V1 只实现一个 |
| 飞书形态 | **聊天机器人（文字双向）** | Phase 2 再考虑主动推送 |
| 打断语义 | **全局横切能力** | 同时打断 AI 思考 + 小爱播报，非绑定小爱 |
| 播报并发 | **Aether 软件锁串行排队** | 一把 asyncio.Lock + 队列，不碰硬件层 |
| 外部打断 | **不管，Aether 不霸道** | 只约束 Aether 自己的调用，外部程序自由用小爱 |

---

## 3. 架构总览

### 3.1 全景图

```
┌─────────────────────────────────────────────────────────────────┐
│                        Aether 宿主进程                            │
│                                                                  │
│  ┌──────────────┐   ┌──────────────────────┐  ┌───────────────┐ │
│  │ Dispatcher    │   │ IntegrationLayer     │  │ InterruptCtrl  │ │
│  │ (现有)        │   │   (新增核心)          │  │   (W3 横切)    │ │
│  │               │   │                      │  │               │ │
│  │ final_content │──▶│ SinkManager          │◀─│ cancel_all()  │ │
│  │   产出后      │   │  .broadcast(text)    │  │ - AI task.cancel│
│  │               │   │                      │  │ - sink.interrupt│
│  │ dispatch_stream│  │ InboundRouter        │  └───────────────┘ │
│  │   可被 cancel │   │  .route(text, mode)  │                    │
│  └──────┬───────┘   │                      │                    │
│         │           │ CapabilityRegistry   │                    │
│         │           │  - 优先级排序          │                    │
│         │           │  - 资源冲突检测        │                    │
│         │           └──────────┬───────────┘                    │
│         │                      │ stdio JSON-RPC（复用 MCP 传输） │
│         │                      ▼                                  │
│  ┌──────▼───────┐   ┌──────────────────────┐                    │
│  │ AppContainer  │   │ PluginSupervisor      │                   │
│  │  (DI, 现有)   │   │  - 进程生命周期        │                   │
│  │  ha_service   │   │  - 崩溃恢复（退避）    │                   │
│  │  dispatcher   │   │  - 心跳/熔断           │                   │
│  └──────────────┘   │ PluginConnection      │                   │
│         ▲           │  - 双向 RPC             │                   │
│         │ 反向 RPC  │  - pending futures map  │                   │
│         │ (权限白名单)└──────────┬───────────┘                   │
│         └────────────────────────┘                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                    ▼
        ┌──────────────────┐    ┌──────────────────┐
        │ integrations/    │    │ integrations/    │
        │   xiaoai/        │    │   feishu/        │
        │   ├ manifest.json│    │   ├ manifest.json│
        │   ├ plugin.py    │    │   ├ plugin.py    │
        │   └ requirements │    │   └ requirements │
        └──────────────────┘    └──────────────────┘
        (独立子进程)             (独立子进程)
```

### 3.2 工作项映射

| 工作项 | 本质 | 落地位置 |
|--------|------|---------|
| W1 小爱输出广播 | `output_sink` 能力 | `Dispatcher` 钩子 → `SinkManager.broadcast` |
| W2 小爱直通模式 | `inbound_router` 能力 | `InboundRouter.route` + ChatView 模式切换 |
| W3 全局打断 | 横切控制器 | `InterruptController` + WS interrupt 消息 |
| W4 飞书机器人 | `output_sink` + `inbound_router` | `feishu_routes.py` webhook → `dispatcher.dispatch` |

---

## 4. 核心模块设计

### 4.1 插件清单规范（manifest.json）

插件的身份与契约声明：

```json
{
  "id": "xiaoai",
  "name": "小爱音箱",
  "version": "1.0.0",
  "aether_api_version": "1",
  "author": "Aether",
  "description": "小爱 TTS 广播 + 直通模式",
  "entry": "plugin.py",
  "depends_on": [],
  "capabilities": [
    {
      "type": "output_sink",
      "id": "xiaoai_pro",
      "priority": 100,
      "config_schema": {
        "entity_id": {
          "type": "string", "required": true,
          "label": "小爱实体ID",
          "placeholder": "media_player.xiaoai_pro"
        },
        "execute_mode": {
          "type": "enum",
          "options": ["speak", "execute"],
          "default": "speak",
          "label": "默认模式"
        }
      },
      "queue_policy": {
        "default": "queue"
      }
    },
    {
      "type": "inbound_router",
      "id": "xiaoai_direct",
      "priority": 50,
      "config_schema": {}
    }
  ],
  "permissions": [
    "ha.call_service",
    "ha.get_state"
  ],
  "resources": {
    "max_memory_mb": 128,
    "restart_on_crash": true,
    "max_restarts": 3
  }
}
```

### 4.2 能力契约（Capability Contract）

#### 4.2.1 能力类型

| 能力类型 | 职责 | RPC 方法 | 仲裁策略 |
|---------|------|---------|---------|
| `output_sink` | 接收 Aether 回复并输出 | `sink.speak`, `sink.interrupt` | 全广播（fan-out） |
| `inbound_router` | 接收用户输入并处理 | `router.handle` | 用户显式选模式（V1） |

#### 4.2.2 priority 仲裁规则（已收敛）

经 brainstorming 两轮收敛，priority 的作用被收敛到最小：

- **output_sink**：`SinkManager.broadcast` 并发 `asyncio.gather` 到所有 enabled sink。priority 仅用于：
  1. UI 排序
  2. 单个 sink 失败时的 fallback 序
- **inbound_router（V1）**：用户在 ChatView 显式选模式（如"小爱模式"），路由由用户选择决定，**不靠 priority 仲裁**。priority 仅 UI 排序用。

**不区分消息类型**（删去 device_confirm/alert/chat 分类——避免凭空复杂度）。

### 4.3 播报串行锁（两层解耦）

**关键设计：Aether 软件锁 vs 硬件层完全分离。**

```
┌─────────────────────────────────────────────────────┐
│  Aether 软件锁（asyncio.Lock，进程内逻辑锁）          │
│  作用：Aether 自己的消息排队，不并发占用小爱          │
│  范围：只管 Aether 自己，管不了外部                   │
└─────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────┐
│  小爱硬件（物理设备，谁都能调）                       │
│  外部程序（米家 app/定时场景/其他）随时可控制小爱    │
│  Aether 不拦、不知道、不管                            │
└─────────────────────────────────────────────────────┘
```

**规则：**
- Aether 内部多消息 → **串行排队**（一把锁 + 一个队列）
- Aether 主动打断 → **允许**（清队列 + 停小爱 + 释放锁）
- 外部程序打断小爱 → **不管**（Aether 不霸道，小爱完全开放给外部）

```python
class XiaoAiSink(OutputSink):
    def __init__(self, config, ha_caller):
        self._seq_lock = asyncio.Lock()   # 软件串行锁
        self._queue: asyncio.Queue = asyncio.Queue()
        self._ha_caller = ha_caller

    async def speak(self, text: str, msg_id: str):
        await self._queue.put((text, msg_id))
        async with self._seq_lock:
            while not self._queue.empty():
                msg, mid = await self._queue.get()
                await self._do_call_xiaoai(msg)

    async def interrupt(self):
        while not self._queue.empty():
            self._queue.get_nowait()
        await self._stop_xiaoai()

    async def _do_call_xiaoai(self, text: str):
        # 调 HA: xiaomi_miot.intelligent_speaker
        await self._ha_caller.call_service(
            domain="xiaomi_miot",
            service="intelligent_speaker",
            entity_id=self._entity_id,
            data={"text": text, "execute": False, "silent": False}
        )

    async def _stop_xiaoai(self):
        await self._ha_caller.call_service(
            domain="media_player", service="media_stop",
            entity_id=self._entity_id, data={}
        )
```

> **注**：上面是概念示意。在插件系统架构下，`XiaoAiSink` 的实际实现位于子进程插件内（见 §6）。Aether 宿主侧只持有 `PluginConnection`，通过 RPC 调用插件的 `sink.speak` / `sink.interrupt`。锁和队列位于插件进程内。

### 4.4 全局打断（InterruptController）

打断是横切能力，同时作用：
1. **AI 思考**：`Dispatcher.dispatch_stream` 的当前 turn task 被 `task.cancel()`
2. **所有 output_sink**：每个启用的 sink 收到 `interrupt` 调用

```python
class InterruptController:
    def __init__(self):
        self._active_turns: dict[str, asyncio.Task] = {}  # request_id → task

    def register_turn(self, request_id: str, task: asyncio.Task):
        self._active_turns[request_id] = task

    def unregister_turn(self, request_id: str):
        self._active_turns.pop(request_id, None)

    async def interrupt(self, request_id: str, sink_manager):
        task = self._active_turns.get(request_id)
        if task and not task.done():
            task.cancel()  # 打断 AI 思考
        await sink_manager.interrupt_all()  # 打断所有 sink 播报
```

**前端触发**：ChatView 发 WS 消息 `{type: "interrupt", request_id}` → WS route → `InterruptController.interrupt`。

---

## 5. 插件运行时（核心机制）

### 5.1 状态机

```
                    ┌──────────────────────────────────────┐
                    ▼                                       │
  发现 ──▶ 校验 ──▶ 待启动 ──▶ 启动中 ──▶ 运行中 ──┐       │
                                          │          │       │
                                          │ 心跳超时  │ 崩溃  │
                                          ▼          ▼       │
                                        不健康 ──▶ 重启中 ──┘
                                          │          │
                                          │ 修复     │ 超过 max_restarts
                                          └──────┐   ▼
                                                 ▼  禁用
                                              运行中   (需人工介入)
```

| 状态 | 含义 |
|------|------|
| 待启动 | 清单校验通过，未 spawn |
| 运行中 | 进程活 + 心跳正常 + 握手成功 |
| 不健康 | 进程活但心跳超时（卡死征兆） |
| 禁用 | 重启次数耗尽，等用户处理 |

### 5.2 坑点与对策

#### ⚠️ 坑点 1：API 版本演进
清单声明 `aether_api_version`，Aether 维护 `SUPPORTED_API_VERSIONS`，加载时校验不匹配则拒绝启动 + 告警。**借鉴 VS Code `engines.vscode`。**

#### ⚠️ 坑点 2：重启风暴（Restart Storm）
插件启动时依赖项故障 → 崩溃 → 立即重启 → 又崩溃 → 一秒崩几十次，日志爆炸 CPU 飙满。

**解法：指数退避**

```python
class PluginSupervisor:
    async def _supervise(self, plugin_id: str):
        restart_count = 0
        backoff = 1.0
        while restart_count < self._max_restarts(plugin_id):
            proc = await self._spawn(plugin_id)
            exit_code = await proc.wait()
            if exit_code == 0:
                return  # 正常退出
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            restart_count += 1
        await self._disable(plugin_id, reason="restart_limit_exceeded")
```

**借鉴 Kubernetes CrashLoopBackOff。**

#### ⚠️ 坑点 3：僵尸进程
子进程退出后父进程不 `wait()` → 进程表项残留 → PID 耗尽。

**解法**：`asyncio.create_subprocess_exec`（事件循环自动 reap）+ 监控层 `poll()` 兜底。

#### ⚠️ 坑点 4：优雅关闭
三级递进：RPC `shutdown`（5s）→ `SIGTERM`（2s）→ `SIGKILL`。**借鉴 systemd 关闭模型。**

#### ⚠️ 坑点 5：启动顺序
拓扑排序解析 `depends_on`，检测循环依赖 → fail fast。

#### ⚠️ 坑点 6：死锁检测（心跳）
进程活但不响应（死锁/死循环）。心跳超时 → 标记不健康 → 强杀重启。

### 5.3 优雅关闭流程

```python
async def graceful_shutdown(self, plugin_id: str):
    proc = self._procs[plugin_id]
    try:
        await asyncio.wait_for(
            self._rpc_call(plugin_id, "shutdown"), timeout=5.0
        )
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"Plugin {plugin_id} force-killed")
```

---

## 6. IPC 协议（双向 JSON-RPC over stdio）

### 6.1 两个方向

```
方向 1：Aether → 插件（请求/响应）
  场景：sink.speak, sink.interrupt, router.handle, health.check
  
方向 2：插件 → Aether（反向调用，权限白名单守护）
  场景：ha.call_service, dispatcher.dispatch, ha.get_state
```

### 6.2 协议分层

```
┌──────────────────────────────────────┐
│  应用层：Aether Integration API      │  语义接口
├──────────────────────────────────────┤
│  契约层：JSON-RPC 2.0 方法定义       │  schema 契约 + 参数校验
├──────────────────────────────────────┤
│  传输层：stdio JSON-RPC（复用 MCP）  │  字节流管道
└──────────────────────────────────────┘
```

### 6.3 方向 1：Aether → 插件

```jsonc
// 播报
{"jsonrpc":"2.0","id":1,"method":"sink.speak","params":{"text":"床头灯已打开","msg_id":"abc"}}

// 中断播报
{"jsonrpc":"2.0","id":2,"method":"sink.interrupt","params":{}}

// 路由入站消息
{"jsonrpc":"2.0","id":3,"method":"router.handle","params":{"text":"播放周杰伦的歌","user_id":"u1","mode":"direct"}}

// 健康检查
{"jsonrpc":"2.0","id":4,"method":"health.check","params":{}}
```

### 6.4 方向 2：插件 → Aether（权限白名单守护）

```jsonc
// 调 HA 服务
{"method":"aether.ha.call_service",
 "params":{"domain":"xiaomi_miot","service":"intelligent_speaker",
           "entity_id":"media_player.xiaoai_pro","data":{"text":"hi"}}}

// 让 LLM 处理文本（飞书消息进 LLM）
{"method":"aether.dispatcher.dispatch",
 "params":{"query":"用户在飞书发的消息","session_id":"feishu_user_123"}}

// 读 HA 实体状态
{"method":"aether.ha.get_state","params":{"entity_id":"light.bed"}}
```

**权限校验：**

```python
class ReverseRPCHandler:
    def __init__(self, container: AppContainer):
        self._container = container
        self._permissions: dict[str, set[str]] = {}  # plugin_id → 允许的 method

    async def handle(self, plugin_id: str, request: dict):
        method = request["method"]
        if method not in self._permissions.get(plugin_id, set()):
            return {"error": "permission_denied", "method": method}
        handler = self._routes[method]
        return await handler(request["params"])
```

### 6.5 ⚠️ 坑点 7：请求-响应配对（交错到达）

双向管道上请求和响应交错：

```
T1   Aether 发 speak(id=1)               插件收到 id=1
T2                                       开始念（耗时 3s）
T3   插件发 ha.get_state(id=1001)         Aether 收到 id=1001
T4   Aether 回 result(id=1001)
T5                                       插件收到 id=1001
T6                                       念完回 result(id=1)
T7   Aether 收到 id=1
```

**解法：pending futures map**

```python
class PluginConnection:
    def __init__(self):
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1

    async def call(self, method: str, params: dict) -> dict:
        req_id = self._next_id
        self._next_id += 2  # Aether 用奇数 id
        future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        await self._send({"jsonrpc":"2.0","id":req_id,"method":method,"params":params})
        return await future

    async def _read_loop(self):
        async for msg in self._read_stream():
            msg_id = msg.get("id")
            if "method" in msg:
                # 对方发来的请求
                result = await self._reverse_handler.handle(msg)
                await self._send({"jsonrpc":"2.0","id":msg_id,"result":result})
            elif msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if "error" in msg:
                    future.set_exception(RPCError(msg["error"]))
                else:
                    future.set_result(msg.get("result"))
```

**借鉴 HTTP/2 多路复用 / gRPC stream id。**

### 6.6 ⚠️ 坑点 8：重入死锁（Reentrant Deadlock）

经典双向 RPC 死锁：

```
插件调 aether.dispatch（要 Aether 跑 LLM）
  → Aether 跑 LLM,LLM 调小爱 speak
    → speak 是调插件!
      → 插件还在等 dispatch,不响应 speak
        → 死锁
```

**双保险解法：**
1. **插件端 read_loop 永不阻塞**——每个请求 spawn task 处理
2. **所有调用强制超时**

```python
# 插件端:读取循环不阻塞
async def _read_loop(self):
    async for msg in self._read_stream():
        if "method" in msg:
            asyncio.create_task(self._handle_request(msg))  # 不阻塞

# Aether 端:强制超时
async def call_plugin(self, plugin_id, method, params, timeout=30):
    return await asyncio.wait_for(
        self._conns[plugin_id].call(method, params),
        timeout=timeout
    )
```

**借鉴 actor 模型"消息不阻塞"思想。**

### 6.7 协议握手

```jsonc
// Aether → 插件（启动后立即发）
{"jsonrpc":"2.0","id":0,"method":"handshake","params":{
  "aether_api_version":"1",
  "capabilities_expected":["output_sink","inbound_router"],
  "permissions_granted":["ha.call_service","dispatcher.dispatch"]
}}

// 插件 → Aether
{"jsonrpc":"2.0","id":0,"result":{
  "plugin_id":"xiaoai","plugin_version":"1.0.0","ready":true
}}
```

握手成功才标记"运行中"。

---

## 7. 三个具体集成设计

### 7.1 小爱插件（W1 + W2）

**manifest 摘要：**
- `capabilities`: `output_sink`（priority 100）+ `inbound_router`（priority 50）
- `permissions`: `ha.call_service`, `ha.get_state`
- 直通模式：`execute=true` 转小爱原生

**output_sink 行为：**
- `sink.speak(text)` → Aether 软件锁串行 → 调 `xiaomi_miot.intelligent_speaker`（execute=false，TTS）
- `sink.interrupt()` → 清队列 + `media_player.media_stop`
- 锁/队列位于插件进程内

**inbound_router 行为（直通模式）：**
- 用户在 ChatView 切"小爱模式" → 前端发消息带 `mode="xiaoai_direct"`
- `InboundRouter` 路由到 xiaoai 插件的 `router.handle`
- 插件调 `ha.call_service(domain="xiaomi_miot", service="intelligent_speaker", data={text, execute:true})`
- 小爱原生执行（播放音乐/讲笑话等），不进 Aether LLM

**配置项：**
- `entity_id`（必填）：小爱 media_player 实体
- `execute_mode`（默认 speak）：output 默认模式

### 7.2 飞书插件（W4）

**manifest 摘要：**
- `capabilities`: `output_sink`（priority 90）+ `inbound_router`（priority 90）
- `permissions`: `dispatcher.dispatch`, `ha.get_state`
- `depends_on`: []

**入站（飞书 → Aether）：**
- 用户在飞书 @机器人/私聊 → 飞书发 webhook 到 Aether
- **关键**：飞书 webhook 直达 Aether（不经插件进程），因为 webhook 是 HTTP 入口，需要 Aether 有公网可达端点
- webhook route 解析飞书事件 → 调 `dispatcher.dispatch`（飞书 session_id 隔离）→ 收集 `Template.ToastStream` → 调飞书插件的 `sink.speak` 发消息

> **架构决策**：飞书 webhook 接收在 Aether 宿主侧（`feishu_routes.py`），因为 webhook 需要公网端点和签名校验，不适合放子进程。子进程只负责"发消息到飞书"（output_sink 能力）。

**出站（Aether → 飞书）：**
- `sink.speak(text)` → 调飞书发消息 API
- session 路由：回复发到原会话（私聊/群）

**配置项：**
- `app_id`, `app_secret_env`（飞书应用凭证）
- `verification_token`（webhook 校验）
- `bot_name`（@识别）

### 7.3 ChatView 模式切换 + 打断 UI（W2 + W3）

**模式切换：**
- ChatView 输入框旁加"模式选择器"（下拉/按钮组）
- 默认 `aether`（走 LLM），可选 `xiaoai_direct`（直通小爱）
- 选定后，每条消息发送时附带 `mode` 字段
- WS route 根据 `mode` 路由：`aether` → Dispatcher，`xiaoai_direct` → InboundRouter

**打断按钮：**
- AI 思考/小爱播报期间显示"打断"按钮
- 点击 → 发 `{type:"interrupt", request_id}`
- 后端 `InterruptController.interrupt` → AI task.cancel + sink.interrupt_all
- 打断是横切的，作用于当前活跃的所有输出

---

## 8. 数据流（端到端）

### 8.1 W1：打开床头灯 → 小爱播报

```
用户语音/文字"打开床头灯"
  → /ws/chat (mode=aether)
  → Dispatcher.dispatch_stream
  → LLM ReAct: call_service(light.turn_on)
  → HA 执行,设备亮
  → final_content = "床头灯已打开"
  → SinkManager.broadcast("床头灯已打开")
     ├─ XiaoAiSink.speak  → RPC → 插件 → 软件锁 → HA intelligent_speaker → 小爱念
     └─ (未来其他 sink)
  → InterruptController 注册本次 turn
```

### 8.2 W2：小爱模式直通播放音乐

```
用户在 ChatView 切"小爱模式",输入"播放周杰伦的歌"
  → /ws/chat (mode=xiaoai_direct)
  → InboundRouter.route("播放周杰伦的歌", mode=xiaoai_direct)
  → RPC → xiaoai 插件 router.handle
  → 插件调 ha.call_service(xiaomi_miot.intelligent_speaker, execute=true, text="播放周杰伦的歌")
  → 小爱原生执行播放音乐
  → 插件回 {ok:true}
  → 前端显示"已转交小爱处理"
```

### 8.3 W3：全局打断

```
用户点击"打断"按钮
  → /ws/chat {type:interrupt, request_id:xxx}
  → InterruptController.interrupt(xxx)
     ├─ AI task.cancel()  → Dispatcher 收到 CancelledError,停止流
     └─ SinkManager.interrupt_all()
         └─ 每个启用的 sink 收到 sink.interrupt RPC
             └─ XiaoAiSink: 清队列 + media_player.media_stop
```

### 8.4 W4：飞书聊天

```
用户在飞书私聊 @机器人 "今天天气怎么样"
  → 飞书 webhook → Aether /api/feishu/webhook
  → 校验签名 + 解析事件
  → dispatcher.dispatch(query="今天天气怎么样", session_id="feishu_xxx")
  → LLM 跑完,final_content="今天晴,28度"
  → SinkManager.broadcast(final_content, target_session="feishu_xxx")
  → FeishuSink.speak → 调飞书发消息 API → 用户在飞书收到回复
```

---

## 9. 代码结构

### 9.1 宿主侧新增（Aether）

```
app/
├── integration/                          ← 新增核心目录
│   ├── __init__.py
│   ├── container.py                      ← IntegrationLayer（SinkManager + InboundRouter + CapabilityRegistry）
│   ├── sink_manager.py                   ← 广播 + fan-out
│   ├── inbound_router.py                 ← 入站路由（用户显式选模式）
│   ├── capability_registry.py            ← 能力注册 + 优先级排序
│   ├── plugin_supervisor.py              ← 进程生命周期 + 退避重启 + 熔断
│   ├── plugin_connection.py              ← 双向 JSON-RPC + pending futures
│   ├── reverse_rpc_handler.py            ← 插件→Aether 反向调用 + 权限校验
│   ├── manifest_loader.py                ← 目录扫描 + 校验 + 拓扑排序
│   ├── interrupt_controller.py           ← 全局打断（W3 横切）
│   └── schema/
│       ├── manifest_schema.py            ← Pydantic manifest 校验
│       └── rpc_methods.py                ← RPC 方法定义
│
├── routes/
│   ├── integration_routes.py             ← /api/integrations CRUD（启用/禁用/配置）
│   └── feishu_routes.py                  ← /api/feishu/webhook（W4 入口）
│
├── schema/
│   └── chat_schema.py                    ← 新增 Interrupt instruction + mode 字段
│
└── 修改点:
    ├── agents/dispatcher.py              ← final_content 产出后调 SinkManager.broadcast
    ├── container.py                      ← AppContainer 加 integration_layer 字段
    ├── bootstrap.py                      ← initialize_services 启动 IntegrationLayer
    ├── routes/ws_routes.py               ← 处理 interrupt 消息 + mode 路由
    └── main.py                           ← 注册 integration_routes + feishu_routes
```

### 9.2 插件 SDK（共享给插件开发者）

```
app/integration/sdk/                      ← 打包给插件用
├── __init__.py
├── plugin_base.py                        ← IntegrationPlugin 基类
├── sink_base.py                          ← OutputSink 抽象
├── router_base.py                        ← InboundRouter 抽象
├── rpc_client.py                         ← 反向调用 Aether 的客户端
├── stdio_server.py                       ← stdio JSON-RPC server（复用 MCP）
└── types.py                              ← 共享类型
```

### 9.3 集成插件（独立目录）

```
integrations/                             ← 根目录约定（同 ha_config 同级）
├── xiaoai/
│   ├── manifest.json
│   ├── plugin.py                         ← XiaoAiPlugin（OutputSink + InboundRouter）
│   ├── ha_bridge.py                      ← 封装 ha.call_service 反向调用
│   ├── queue_lock.py                     ← 软件锁 + 队列
│   └── requirements.txt                  ← 可选,插件独有依赖
│
└── feishu/
    ├── manifest.json
    ├── plugin.py                         ← FeishuPlugin（OutputSink）
    ├── feishu_client.py                  ← 飞书 API 客户端
    └── requirements.txt
```

### 9.4 前端新增

```
frontend/src/
├── views/
│   ├── IntegrationsView.vue              ← 新视图:插件列表 + 启用/禁用 + 配置
│   └── ChatView.vue                      ← 修改:模式选择器 + 打断按钮
├── components/
│   ├── PluginConfigPanel.vue             ← 动态表单（根据 manifest config_schema 渲染）
│   └── InterruptButton.vue               ← 打断按钮组件
├── router/index.js                       ← 加 /integrations 路由
└── utils/api.js                          ← 加 integration API 封装
```

---

## 10. 配置

### 10.1 config.json 新增 section

```json
{
  "integration": {
    "enabled": true,
    "plugin_dir": "integrations",
    "api_version": "1",
    "startup_timeout": 10,
    "health_check_interval": 30,
    "default_rpc_timeout": 30
  }
}
```

插件级配置不写在 config.json，而是：
- **静态默认值**：在插件 manifest 的 `config_schema.default`
- **用户覆盖值**：存 DB `integration_config` 表（plugin_id + key + value + user_id）
- **secrets**：存 `.env`（key 由 manifest 的 `secret: true` 标记）

### 10.2 数据库表

```sql
-- 插件用户配置（覆盖 manifest 默认值）
CREATE TABLE integration_config (
    plugin_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    config_key  TEXT NOT NULL,
    config_value TEXT,
    PRIMARY KEY (plugin_id, user_id, config_key)
);

-- 插件运行状态（用于崩溃恢复后还原）
CREATE TABLE integration_runtime (
    plugin_id   TEXT PRIMARY KEY,
    enabled     BOOLEAN DEFAULT FALSE,
    state       TEXT,        -- running/disabled/error
    last_error  TEXT,
    updated_at  TIMESTAMP
);
```

---

## 11. 实施阶段（分 Phase 交付）

### Phase 1：插件系统骨架 + 小爱 W1（MVP 可演示）

**目标**：Aether 回复 → 小爱播报。

**交付**：
- manifest_loader + schema 校验
- plugin_supervisor（spawn + 退避重启，暂不心跳熔断）
- plugin_connection（方向 1 单向 RPC，暂不双向）
- sink_manager.broadcast
- Dispatcher 钩子（final_content → broadcast）
- XiaoAiSink 插件（调 intelligent_speaker）
- 基础配置 UI

**不做**：双向 RPC、inbound_router、打断、飞书。

**验收**：跟 Aether 对话，小爱同步念出回复；插件进程 kill 后自动重启。

### Phase 2：全局打断 W3 + 小爱 W2

**目标**：能打断 + 能直通小爱。

**交付**：
- interrupt_controller（AI task.cancel + sink.interrupt）
- ChatView 打断按钮 + WS interrupt 消息
- inbound_router + 模式路由
- xiaoai 直通模式（execute=true）
- ChatView 模式选择器
- 软件锁 + 队列

**验收**：点打断立即停 AI + 停小爱；切小爱模式打字，小爱原生执行。

### Phase 3：双向 RPC + 反向调用

**目标**：插件能反向调 Aether。

**交付**：
- plugin_connection 双向化（pending futures map）
- reverse_rpc_handler + 权限白名单
- SDK 完善（rpc_client 反向调用封装）
- 坑点 8 重入死锁防护（read_loop 不阻塞 + 超时）

**验收**：插件代码里能调 `aether.ha.get_state` 拿到设备状态。

### Phase 4：飞书机器人 W4

**目标**：飞书 ↔ Aether 双向聊天。

**交付**：
- feishu_routes.py（webhook 接收 + 签名校验）
- FeishuPlugin（output_sink 发消息）
- session 隔离（feishu_user_xxx）
- dispatcher.dispatch 复用（非 WS 路径）
- IntegrationsView 配置面板

**验收**：飞书 @机器人，Aether 回复发回飞书。

### Phase 5（可选）：高级机制

- 心跳熔断（坑点 6 完整版）
- 优雅关闭三级流程（坑点 4 完整版）
- 依赖图拓扑排序启动
- 插件市场（远程下载 + 签名）

---

## 12. 风险与权衡

### 12.1 复用 MCP 传输的风险

**现状**：Aether 的 `MCPClientManager` 实现了方向 1（Aether → MCP server），但未实现方向 2（MCP server → Aether 反向调用）。

**风险**：补全方向 2 需深入 MCP 协议，可能发现现有 client 代码不兼容双向。

**缓解**：Phase 3 才做双向，Phase 1-2 只用方向 1，风险后置。若 Phase 3 发现 MCP 复用成本过高，可退化为自研轻量 JSON-RPC（不影响 Phase 1-2 已交付内容）。

### 12.2 子进程性能开销

每次 sink.speak 都跨进程 RPC，相比进程内直调慢（~1-5ms IPC 延迟）。

**权衡**：小爱播报不是高频操作（秒级粒度），IPC 开销可忽略。换来崩溃隔离，值。

### 12.3 动态 UI 表单的复杂度

`PluginConfigPanel.vue` 需根据 manifest 的 `config_schema` 动态渲染表单（string/enum/secret/嵌套），是前端最复杂的部分。

**缓解**：V1 只支持简单类型（string/enum/boolean/secret），嵌套结构留后。

---

## 13. 面试谈资总结（项目核心价值）

本设计可对外讲述的工程深度：

| 主题 | 体现点 |
|------|--------|
| 架构决策 | 三方案对比（最小改动/轻量抽象/插件系统），知情选 C |
| 进程隔离 | 子进程 vs 进程内，崩溃隔离边界 |
| 生命周期管理 | 状态机 + 指数退避 + 熔断（借鉴 K8s CrashLoopBackOff） |
| IPC 协议 | JSON-RPC 双向 + pending futures map（借鉴 HTTP/2 多路复用） |
| 并发控制 | 重入死锁防护（借鉴 actor 模型） |
| 安全边界 | 权限白名单 + 零信任反向调用 |
| 优雅降级 | 三级关闭（RPC→TERM→KILL，借鉴 systemd） |
| 可扩展性 | manifest 自描述 + 目录扫描 + 动态 UI |
| 去除过度设计 | priority 从排他仲裁收敛为 UI 排序 + 容错 |
| 版本演进 | API 版本契约（借鉴 VS Code engines） |

---

## 14. 开放问题（待实现时定）

1. **小爱播报内容过滤**：LLM 回复可能含 Markdown/表格，小爱念出来会怪。是否需要"纯文本化"预处理？（建议 Phase 1 先不过滤，观察体验）
2. **打断的边界**：AI 思考已结束但小爱还在念，此时打断算"打断 AI"还是只"打断小爱"？（当前设计：都打断，简化）
3. **多用户隔离**：飞书不同用户的 session 是否共享 HA 设备控制权？（建议：默认共享，Phase 4 再考虑隔离）
4. **插件日志聚合**：子进程日志怎么收集到 Aether 主日志？（建议：stderr → Aether 日志聚合，标 plugin_id 前缀）
