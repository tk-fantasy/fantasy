# Aether 集成平台 Phase 2 设计：全局打断（W3）+ 小爱直通模式（W2）

**日期**: 2026-08-06
**状态**: 待评审
**前置**: Phase 1 已完成（插件骨架 + 小爱播报 + 热加载），见 `2026-08-06-integration-platform-design.md`

---

## 1. 背景与目标

Phase 1 完成了"Aether 回复 → 小爱播报"的 MVP。Phase 2 交付两个用户在最初头脑风暴里确认的核心需求：

- **W3 全局打断**：用户原话"对话没有打断功能这是全局的"。同时打断 AI 思考 + 小爱播报。
- **W2 小爱直通模式**：用户在 ChatView 切"小爱模式"打字（如"播放周杰伦的歌"），文字原样转小爱原生执行（execute=true），不进 LLM。

### 1.1 验收标准

| 验收项 | 行为 |
|--------|------|
| 点打断 | AI 立即停止 + 小爱立即停止播报 + 前端不卡死 |
| AI 思考时发新消息 | 自动打断旧的 AI + 停小爱，开始新对话 |
| AI 结束但小爱还在念 | 点打断 → 只停小爱（无 task 可 cancel） |
| 切小爱模式打字 | 小爱原生执行（播放音乐/讲笑话），不进 LLM，不消耗 token |
| 两用户并发 | AI 思考各自独立；小爱共享设备队列排队 |

---

## 2. 决策记录

| 决策项 | 选择 | 理由 |
|--------|------|------|
| WS 循环改造 | **方案 B：单活跃 task** | 单用户单对话场景，不需要 request_id 精确匹配。`current_task` 局部变量，简单 |
| 新消息冲突 | **自动打断旧的** | 类 ChatGPT 体验，最自然 |
| request_id 跟踪 | **不需要** | 一个 WS 连接 = 一个对话，同时只有一个活跃 task。客户端零改动 |
| 打断按钮可见性 | **始终可见** | 无法精确知道小爱何时念完（HA 不回调），始终可见最可靠。idle 时点击为 no-op |
| CancelledError 处理 | **吞掉不 re-raise** | emit Finish + interrupt_all 后正常返回，避免 WS handler 崩 |
| 直通模式 session | **不进 history** | 完全绕过 LLM，不消耗 token，不污染对话历史 |
| 模式选择器 | **框架自带 UI（非插件贡献）** | 模式切换是框架能力，直接写在 ChatView 里 |
| 多用户小爱打断 | **全局（defer Phase 4）** | 共享家庭设备，全局打断符合直觉。与 spec §14 Q3 一致 |

---

## 3. 架构：WS 循环改造（核心变更）

### 3.1 当前问题

`app/routes/ws_routes.py:48` 直接 `await dispatch_stream(event, ...)`，receive 循环在 AI 思考期间卡死，收不到任何新消息（包括 interrupt）。这是打断必须解决的核心。

### 3.2 改造后结构

```python
async def ws_chat(websocket, container, user_id):
    current_task: asyncio.Task | None = None
    await websocket.accept()
    while True:
        payload = await websocket.receive_json()

        if payload.get("type") == "pong":
            continue

        if payload.get("type") == "interrupt":
            current_task = await _cancel_current(current_task, container)
            continue

        if payload.get("type") == "chat":
            # 自动打断旧的（用户已确认）
            current_task = await _cancel_current(current_task, container)

            mode = payload.get("mode", "aether")
            rid = payload.get("request_id") or new_request_id()
            set_request_id(rid)

            if mode == "xiaoai_direct":
                current_task = asyncio.create_task(
                    _handle_direct(websocket, container, payload, rid, user_id)
                )
            else:
                event = Event.build_event(
                    Nlp.Request(query=payload.get("query", "")),
                    request_id=rid,
                    session_id=payload.get("session_id"),
                )
                current_task = asyncio.create_task(
                    _run_dispatch(container, event, websocket, user_id, rid)
                )
            set_request_id("-")
```

### 3.3 辅助函数

```python
async def _cancel_current(task, container) -> asyncio.Task | None:
    """取消当前活跃 task + 中断所有 sink 播报。返回 None（已清理）。"""
    if task and not task.done():
        task.cancel()
        try:
            await task  # 等 CancelledError 传播完毕
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # task 内部异常已自己处理
    # 停小爱（即使 task 已结束，小爱可能还在念）
    sink_manager = container.integration_layer.sink_manager if container.integration_layer else None
    if sink_manager:
        await sink_manager.interrupt_all()
    return None


async def _run_dispatch(container, event, websocket, user_id, rid):
    """包装 dispatch_stream，确保异常不逃逸到 WS 循环。"""
    try:
        await container.dispatcher.dispatch_stream(event, websocket.send_json, user_id=user_id)
    except asyncio.CancelledError:
        # Dispatcher 内部已处理（emit Finish + interrupt），这里兜底
        pass
    except Exception:
        pass  # Dispatcher 内部已有异常处理


async def _handle_direct(websocket, container, payload, rid, user_id):
    """直通小爱模式：文字原样转小爱原生执行。"""
    # 见 §5
```

### 3.4 并发模型

- 每个 WebSocket 连接 = 一个 `ws_chat` 协程 = 一个 `current_task` 局部变量
- 两个用户 = 两个独立 WS 连接 = 两个独立 `current_task`，互不影响
- AI 思考完全独立（各自 LLM 调用）
- 小爱是共享物理设备：多用户播报进同一队列（插件软件锁串行），interrupt_all 全局停（共享家庭设备符合直觉）

---

## 4. W3 全局打断

### 4.1 后端：Dispatcher CancelledError 处理

`app/agents/dispatcher.py` 的 `_run_turn` 方法（约 601-609 行 `run_agent_streaming` 调用处）当前无 CancelledError 处理——cancel 后异常逃逸，客户端挂起收不到 Finish。

**改动**：在 `run_agent_streaming` 调用外包 `try/except asyncio.CancelledError`：

```python
# _run_turn 内，主 agent 循环处
try:
    async for stream_event in run_agent_streaming(...):
        ...
except asyncio.CancelledError:
    # 被打断：通知前端 + 停小爱
    await emit(Dialog.Finish(success=False))  # 让前端知道结束
    if self._sink_manager:
        await self._sink_manager.interrupt_all()
    logger.info("Turn %s 被用户打断", request_id)
    return  # 吞掉 CancelledError，正常返回，不 re-raise
```

**同样模式应用于重试循环**（614-637 行）和验证器循环（641-661 行）——这三处 `run_agent_streaming` 调用都可能被 cancel。

**为什么不 re-raise**：re-raise 会让 `dispatch_stream` → WS handler 的 task 抛 CancelledError，虽然 WS handler 的 `_run_dispatch` 包装也会吞掉，但 Dispatcher 内部自己处理更干净（能 emit Finish 让前端不卡）。

### 4.2 后端：WS route interrupt 消息

收到 `{type:"interrupt"}` → 调 `_cancel_current(current_task, container)`。

### 4.3 后端：SinkManager.interrupt_all()

已实现（`app/integration/sink_manager.py:56-68`），并发 fan-out `sink.interrupt` RPC 到所有 output_sink 插件。不受 `broadcast_enabled` 限制（打断总是生效）。**无需改动。**

### 4.4 前端：打断按钮

- **位置**：输入框工具栏，与 IntegrationSlot 同行
- **可见性**：始终可见（stop 图标）。理由：无法精确知道小爱何时念完（HA 不回调 TTS 完成事件），始终可见确保用户随时能停。idle 时点击为 no-op（interrupt_all 对无活跃播报是 no-op）
- **点击行为**：
  1. 发 WS `{type:"interrupt"}`
  2. 前端立即 `finalizeStreaming()`（清空流式状态，隐藏 thinking 指示器）
- **样式**：thinking 期间红色活跃态，idle 时灰色

### 4.5 开放问题 2 回答（打断边界）

> "AI 思考已结束但小爱还在念，此时打断算什么？"

**回答**：打断按钮始终可见。此时点击 → 没有 task 可 cancel（AI 已结束）→ 只调 `interrupt_all` 停小爱。不需要区分"打断 AI"和"打断小爱"——一个按钮，一个动作，覆盖所有场景。

---

## 5. W2 小爱直通模式

### 5.1 全链路新增

| 层 | 文件 | 改动 |
|----|------|------|
| RPC 协议 | `app/integration/rpc_protocol.py` | 加常量 `METHOD_ROUTE = "router.handle"` |
| SDK 基类 | `app/integration/sdk/router_base.py` | 新增 `InboundRouter` ABC：`async route(self, text: str) -> dict` |
| SDK 路由 | `app/integration/sdk/plugin_base.py` | `handle()` 加 `router.handle` 分支 → 调 `self.router.route(text)` |
| 门面 | `app/integration/integration_layer.py` | 加 `route_inbound(text, mode) -> dict`：找声明 `inbound_router` 的插件，RPC 调 `router.handle` |
| schema | `app/integration/schema.py` | `INBOUND_ROUTER` 枚举已有，无需改 |
| 小爱 manifest | `integrations/xiaoai/manifest.json` | 加 `inbound_router` capability |
| 小爱插件 | `integrations/xiaoai/plugin.py` | 实现 `XiaoAiRouter.route(text)`：调 `notify.send_message` 到 `execute_text_directive` 实体 |

### 5.2 直通数据流

```
用户切"小爱模式"，输入"播放周杰伦的歌"
  → WS {type:"chat", mode:"xiaoai_direct", query:"播放周杰伦的歌", session_id:...}
  → WS route: mode=xiaoai_direct
  → current_task = create_task(_handle_direct(...))
  → _handle_direct:
      ├─ emit 用户气泡（query 原文）
      ├─ integration_layer.route_inbound("播放周杰伦的歌", "xiaoai_direct")
      │   → 找到 xiaoai 插件（声明了 inbound_router）
      │   → RPC router.handle {text:"播放周杰伦的歌"}
      │   → XiaoAiRouter.route("播放周杰伦的歌")
      │       → HA notify.send_message(entity_id=...execute_text_directive_a_5_5, message="播放周杰伦的歌")
      │       → 小爱原生执行（播放音乐）
      │       → return {ok:true, executed:"播放周杰伦的歌"}
      └─ emit Dialog.Finish(success=true, message="已转交小爱处理")
  → 前端渲染助手消息"已转交小爱处理"
```

**关键**：直通模式完全绕过 LLM，不进 session history，不消耗 token。

### 5.3 XiaoAiRouter 实现

```python
class XiaoAiRouter(InboundRouter):
    """小爱直通路由：文字原样转小爱原生执行。"""

    EXECUTE_DIRECTIVE_SUFFIX = "execute_text_directive_a_5_5"

    def __init__(self, media_player: str, ha_caller: HAHttpCaller):
        self._media_player = media_player
        self._ha = ha_caller

    def _execute_entity(self) -> str:
        """从 media_player entity 推导 execute_text_directive notify 实体 id。"""
        # media_player.xiaomi_cn_xxx_lx06 → notify.xiaomi_cn_xxx_lx06_execute_text_directive_a_5_5
        suffix = self._media_player.replace("media_player.", "")
        return f"notify.{suffix}_{self.EXECUTE_DIRECTIVE_SUFFIX}"

    async def route(self, text: str) -> dict:
        entity = self._execute_entity()
        await self._ha.call_service(
            domain="notify", service="send_message",
            data={"entity_id": entity, "message": text},
        )
        return {"ok": True, "executed": text}
```

### 5.4 manifest.json 改动

```json
"capabilities": [
  {
    "type": "output_sink",
    "id": "xiaoai_pro",
    "priority": 100,
    "config_schema": { ... }
  },
  {
    "type": "inbound_router",
    "id": "xiaoai_direct",
    "priority": 50,
    "config_schema": {}
  }
]
```

### 5.5 IntegrationLayer.route_inbound

```python
async def route_inbound(self, text: str, mode: str) -> dict:
    """将入站文字路由到声明 inbound_router 的插件。"""
    for manifest in self._get_enabled_manifests():
        if manifest.has_capability(CapabilityType.INBOUND_ROUTER):
            proc = self._supervisor.get_process(manifest.id)
            if proc and proc.is_alive:
                return await proc.call(METHOD_ROUTE, {"text": text, "mode": mode})
    return {"ok": False, "error": "no inbound router available"}
```

V1 只有一个 inbound_router 插件（小爱），直接调第一个匹配的。未来多 router 时用 mode 字段路由。

---

## 6. 前端改动（ChatView.vue）

### 6.1 模式选择器

- 输入框旁加按钮组 `[Aether | 小爱]`（两个 toggle 按钮，默认 Aether）
- 这是框架自带 UI，不走 IntegrationSlot 插件贡献机制（模式切换是框架能力）
- 选"小爱"时按钮高亮，发送消息附带 `mode: "xiaoai_direct"`
- 选"Aether"时 `mode: "aether"`（或不发 mode，后端默认 aether）

```javascript
const chatMode = ref('aether')  // 'aether' | 'xiaoai_direct'

function sendMessage() {
  ...
  ws.send(JSON.stringify({
    type: 'chat',
    query: text,
    session_id: sessionId.value,
    mode: chatMode.value,
  }))
  ...
}
```

### 6.2 打断按钮

- 输入框工具栏，stop 图标，始终可见
- 点击：`ws.send(JSON.stringify({type:'interrupt'}))` + `finalizeStreaming()`

### 6.3 指令处理

`handleInstruction` 加对 `Dialog.Finish(success=false)` 的处理：
- 收到后 `finalizeStreaming()` 清空流式状态（目前 Finish 只处理 success=true 路径）
- 不显示错误（被打断不是错误）

### 6.4 直通响应显示

`_handle_direct` emit 的 `Dialog.Finish(success=true, message="已转交小爱处理")` 走现有 Finish 处理路径，渲染为助手消息。

---

## 7. 代码结构

### 7.1 新增文件

```
app/integration/sdk/router_base.py          ← InboundRouter ABC
```

### 7.2 修改文件

```
app/routes/ws_routes.py                     ← 循环改 task 式 + interrupt + mode 路由
app/agents/dispatcher.py                    ← _run_turn 加 CancelledError 处理
app/integration/rpc_protocol.py             ← 加 METHOD_ROUTE 常量
app/integration/integration_layer.py        ← 加 route_inbound 方法
app/integration/sdk/plugin_base.py          ← handle() 加 router.handle 分支
integrations/xiaoai/manifest.json           ← 加 inbound_router capability
integrations/xiaoai/plugin.py               ← 加 XiaoAiRouter + 挂到 plugin
frontend/src/views/ChatView.vue             ← 模式选择器 + 打断按钮 + Finish(false) 处理
```

### 7.3 测试文件

```
tests/test_ws_interrupt.py                  ← WS 循环打断行为
tests/test_dispatcher_cancel.py             ← Dispatcher CancelledError 处理
tests/test_inbound_router.py                ← route_inbound + InboundRouter ABC
tests/integrations/test_xiaoai_router.py    ← XiaoAiRouter 直通逻辑（不 spawn）
tests/test_integration_layer_route.py       ← IntegrationLayer.route_inbound
```

---

## 8. 边界与错误处理

| 场景 | 行为 |
|------|------|
| 直通模式但小爱插件未启用/未加载 | `route_inbound` 返回 `{ok:false, error:"no inbound router"}`，前端显示"小爱未启用" |
| 直通模式小爱 HA 调用失败 | 插件 `route()` 抛异常 → RPC 返回 error → WS emit `Dialog.Finish(success=false, message="小爱执行失败")` |
| 打断时无活跃 task | `_cancel_current` 检查 `task.done()`，跳过 cancel，仍调 `interrupt_all`（no-op if 无播报） |
| 打断时 task 已结束但小爱在念 | 跳过 cancel，调 `interrupt_all` 停小爱 |
| Dispatch task 内部异常（非 cancel） | `_run_dispatch` 包装吞掉异常，Dispatcher 内部已有 Finish(success=false) emit |
| 两用户同时 broadcast | 小爱插件软件锁串行排队，不冲突 |

---

## 9. 不在 Phase 2 做的

- 多用户小爱打断隔离（defer Phase 4，spec §14 Q3）
- 小爱播报内容过滤 Markdown→纯文本（spec §14 Q1，先观察体验）
- 心跳熔断 / 优雅关闭三级流程 / 依赖图拓扑（Phase 5）
- 双向 RPC 反向调用（Phase 3）
- 飞书机器人（Phase 4）

---

## 10. 面试谈资

| 主题 | 体现点 |
|------|--------|
| 协作取消（Cooperative Cancellation） | asyncio task.cancel + CancelledError 优雅处理，借鉴结构化并发 |
| 横切关注点 | 打断作为横切能力，同时作用于 AI task + 硬件 sink，非绑定单一组件 |
| WS 长连接状态管理 | 从阻塞式循环到 task 式，支持并发消息处理 + 即时打断 |
| 能力契约扩展 | inbound_router 能力从 schema 到 SDK 到插件全链路落地 |
| 解耦边界 | 直通模式完全绕过 LLM，模式切换是框架能力非插件贡献 |
