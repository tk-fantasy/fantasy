# Aether 集成平台 Phase 2 设计：全局打断 + 小爱直通模式（W2）

**日期**: 2026-08-06
**状态**: 待评审
**前置**: Phase 1 已完成（插件骨架 + 小爱播报 + 热加载），见 `2026-08-06-integration-platform-design.md`

---

## 1. 背景与目标

本次工作实际包含**两件归属不同的事**，协同交付：

### 1A. 全局打断（框架级，独立需求）

Aether 的对话一直缺失打断能力——AI 思考时 WS 循环阻塞，用户既不能停 AI、也发不了新消息。这是 Aether 本身的对话能力缺陷，**与插件系统/小爱无关**。本就该补，与"小爱插件"是两码事。

框架做全局打断时会调 `interrupt_all()`：有 output_sink 插件就顺带停播报，没有插件也不影响（纯 cancel AI 思考）。

### 1B. 小爱直通模式（W2，插件级）

用户在 ChatView 切"小爱"模式打字（如"播放周杰伦的歌"），文字原样转小爱原生执行（execute=true），不进 LLM。这是小爱插件的新功能。

> **关于"打断小爱"**：Phase 1 的 `xiaoai/plugin.py` 已实现 `interrupt()`（清队列 + `media_player.media_stop`）。框架全局打断调 `interrupt_all()` 时会自动命中它——**插件不新增打断代码**，只是被框架搭便车调用。因此"打断物理小爱"算插件能力（Phase 1 已交付），但触发它的是框架的全局打断。

### 1.1 验收标准

| 验收项 | 归属 | 行为 |
|--------|------|------|
| 点打断（AI 生成中） | 框架+插件协同 | AI 生成中发送按钮变身停止按钮，点击立即停 AI（框架）+ 停小爱播报（插件 interrupt，Phase 1 已有）+ 前端不卡死 |
| 点打断（AI 结束但小爱在念） | 框架+插件协同 | broadcasting 阶段发送按钮保持停止态，点击停小爱（无 task 可 cancel，只调 interrupt_all）|
| AI 思考时发新消息 | 框架 | 自动打断旧的 AI + 停小爱，开始新对话 |
| 小爱念完不点打断 | 框架 | 估算超时后自动清 broadcasting，发送按钮恢复发送态 |
| 无插件时点打断 | 框架 | 纯 cancel AI，interrupt_all 无 sink 为 no-op，正常工作 |
| 切小爱模式打字 | 插件(W2) | 小爱原生执行（播放音乐/讲笑话），不进 LLM，不消耗 token |
| 两用户并发 | 框架 | AI 思考各自独立；小爱共享设备队列排队 |

---

## 2. 决策记录

| 决策项 | 选择 | 理由 |
|--------|------|------|
| **工作归属** | **全局打断=框架独立需求；W2 直通=插件功能** | 全局打断是 Aether 对话能力缺陷，与插件无关；打断小爱复用 Phase 1 sink.interrupt，插件不新增打断代码 |
| WS 循环改造 | **方案 B：单活跃 task** | 单用户单对话场景，不需要 request_id 精确匹配。`current_task` 局部变量，简单 |
| 新消息冲突 | **自动打断旧的** | 类 ChatGPT 体验，最自然 |
| request_id 跟踪 | **不需要** | 一个 WS 连接 = 一个对话，同时只有一个活跃 task。客户端零改动 |
| CancelledError 处理 | **吞掉不 re-raise** | emit Finish + interrupt_all 后正常返回，避免 WS handler 崩 |
| 直通模式 session | **不进 history** | 完全绕过 LLM，不消耗 token，不污染对话历史 |
| 模式选择器 | **插件 UI 贡献（mode_option）** | 与 Phase 1 广播开关同机制：manifest 声明 `ui_contribution`，框架通用渲染。没小爱插件 → 只有默认 Aether 按钮，零硬编码 |
| 打断触发 | **发送按钮变身** | AI 生成时发送按钮变身停止按钮，不额外加按钮。类 ChatGPT 交互，一个位置两种语义 |
| AI 结束后打断小爱 | **方案 C：broadcasting status** | HA 不暴露播报状态无法精确检测。Dispatcher broadcast 前 emit UI.Status(broadcasting)，发送按钮保持停止态，超时估算清除。复用现有 statusPhase 机制，框架通用不硬编码 |
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

            if mode == "aether":
                # 默认模式：走 LLM
                event = Event.build_event(
                    Nlp.Request(query=payload.get("query", "")),
                    request_id=rid,
                    session_id=payload.get("session_id"),
                )
                current_task = asyncio.create_task(
                    _run_dispatch(container, event, websocket, user_id, rid)
                )
            else:
                # 任意非默认模式：通用路由到 inbound_router 插件（不硬编码模式名）
                current_task = asyncio.create_task(
                    _handle_direct(websocket, container, payload, rid, user_id)
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
    """直通模式：文字路由到 inbound_router 插件（通用，不硬编码任何插件）。"""
    try:
        text = payload.get("query", "")
        mode = payload.get("mode", "")
        # 通用路由：找声明了 inbound_router 的插件
        result = await container.integration_layer.route_inbound(text, mode)
        if result.get("ok"):
            await websocket.send_json(
                _build_instruction(Dialog.Finish(success=True, message="已转交处理"))
            )
        else:
            await websocket.send_json(
                _build_instruction(Dialog.Finish(success=False, message=result.get("error", "直通失败")))
            )
    except Exception as exc:
        await websocket.send_json(
            _build_instruction(Dialog.Finish(success=False, message="直通执行失败"))
        )
```

### 3.4 并发模型

- 每个 WebSocket 连接 = 一个 `ws_chat` 协程 = 一个 `current_task` 局部变量
- 两个用户 = 两个独立 WS 连接 = 两个独立 `current_task`，互不影响
- AI 思考完全独立（各自 LLM 调用）
- 小爱是共享物理设备：多用户播报进同一队列（插件软件锁串行），interrupt_all 全局停（共享家庭设备符合直觉）

---

## 4. 全局打断（框架级，独立需求）

> **归属说明**：本节全是 Aether 框架自身改动（`ws_routes.py` / `dispatcher.py` / ChatView），补全对话缺失的打断能力，与插件系统无关。无插件时全局打断照常工作（纯 cancel AI，`interrupt_all` 无 sink 为 no-op）。"打断小爱"是副作用——框架调 `interrupt_all` 命中 Phase 1 已有的 `xiaoai/plugin.py:interrupt()`，插件不新增打断代码。

### 4.1 后端：Dispatcher CancelledError 处理

`app/agents/dispatcher.py` 的 `_run_turn` 方法（约 601-609 行 `run_agent_streaming` 调用处）当前无 CancelledError 处理——cancel 后异常逃逸，客户端挂起收不到 Finish。

**改动**：在 `run_agent_streaming` 调用外包 `try/except asyncio.CancelledError`：

```python
# _run_turn 内，主 agent 循环处
try:
    async for stream_event in run_agent_streaming(...):
        ...
except asyncio.CancelledError:
    # 被打断：通知前端 + 停所有 sink（有插件就停，没有也无所谓）
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

### 4.3 后端：SinkManager.interrupt_all()（框架，复用 Phase 1 插件 interrupt）

已实现（`app/integration/sink_manager.py:56-68`），并发 fan-out `sink.interrupt` RPC 到所有 output_sink 插件。不受 `broadcast_enabled` 限制（打断总是生效）。**框架侧无需改动。** 命中的 `xiaoai/plugin.py:interrupt()`（Phase 1 已实现：清队列 + `media_player.media_stop`）也无需改动——这就是"打断物理小爱算插件"的部分，但插件代码 Phase 1 已交付，本次只是被框架搭便车调用。

### 4.4 前端：发送按钮变身打断按钮

- **不额外加按钮**：复用现有发送按钮，AI 生成时**变身**为打断按钮
- **状态切换**（两个阶段都让按钮保持停止态）：
  - AI 空闲 + 无播报 → 发送按钮（▶/纸飞机图标），点击发消息
  - AI 生成中（`statusPhase` 非空，含 thinking/executing/retrying） → 停止按钮（■ 红色），点击发 `{type:"interrupt"}`
  - AI 结束但 sink 在播报（`statusPhase="broadcasting"`） → 保持停止按钮，点击发 `{type:"interrupt"}`
- **实现**：ChatView 用 `statusPhase`（现有状态）判断——非空时就是停止态
  ```javascript
  // 发送按钮的动态行为：statusPhase 非空 = 有事在进行 = 停止态
  function handleSendButton() {
    if (statusPhase.value) {
      ws.send(JSON.stringify({ type: 'interrupt' }))
      finalizeStreaming()
    } else {
      sendMessage()
    }
  }
  ```
- **样式**：生成中/broadcasting 时按钮变红色 + ■ 图标
- **归属**：框架自带 UI（打断是 Aether 对话能力，非插件贡献）

### 4.4a broadcasting 阶段（解决"AI 结束但小爱还在念"）

**问题**：HA 不暴露小爱 TTS 播报状态（实测空闲/播报 state 均为 `on`），无法精确检测"正在念"。AI 生成结束后 `statusPhase` 清空，发送按钮恢复发送态，但小爱可能还在念——此时用户无入口打断。

**方案 C：框架 broadcasting status**

Dispatcher 在 `broadcast()` **之前** emit `UI.Status(phase="broadcasting")`，让发送按钮保持停止态。播报完毕（估算超时）或被 interrupt 后清除。

**为什么框架做**：`statusPhase` 是框架已有的 UI 状态机制（thinking/executing/retrying/finalizing）。broadcasting 只是加一个新 phase 值，复用同一套机制。框架通用，不硬编码小爱（无 sink 插件时 broadcast 是 no-op，broadcasting 也不会 emit）。

**超时估算**：broadcasting 是异步的（speak 排队串行），无法等待完成。用超时估算清除：
- emit `UI.Status(phase="broadcasting")` 后，启动一个定时任务
- 超时时间 = 文本字数 / 4（中文约 4 字/秒）+ 5 秒缓冲
- 超时后 emit `UI.Status(phase="")`（清空）
- 若用户点 interrupt，Dispatcher 的 `_handle_cancelled` 调 `interrupt_all`，WS route 的 `_cancel_current` 后续也会清 statusPhase

**代码位置**：Dispatcher `_run_turn`（约 727-731 行 broadcast hook 处）：

```python
# ── 集成广播钩子 ──
if state.final_content and self._sink_manager is not None:
    try:
        # emit broadcasting status（让发送按钮保持停止态）
        await emit(Instruction.build_instruction(
            UI.Status(phase="broadcasting"), request_id, session_id,
        ))
        await self._sink_manager.broadcast(state.final_content, request_id)
        # 估算超时后清除 broadcasting（中文约 4 字/秒 + 5 秒缓冲）
        est_seconds = max(len(state.final_content) / 4, 3) + 5
        asyncio.create_task(self._clear_broadcasting_after(
            emit, request_id, session_id, est_seconds))
    except Exception as exc:
        logger.warning("集成广播失败（不影响主流程）: %s", exc)
```

```python
async def _clear_broadcasting_after(self, emit, request_id, session_id, delay):
    """延迟清除 broadcasting status（估算播报完毕）。"""
    await asyncio.sleep(delay)
    try:
        await emit(Instruction.build_instruction(
            UI.Status(phase=""), request_id, session_id,
        ))
    except Exception:
        pass  # 连接已断等，忽略
```

> **注**：若 user 在超时前发了新消息，`_cancel_current` 会 cancel 旧 task，超时 task 也会被 cancel（create_task 跟随 task 生命周期）。interrupt 时 `_handle_cancelled` 调 `interrupt_all` 停小爱，但 broadcasting status 的清除由前端的 `finalizeStreaming()` 处理（interrupt 消息处理后前端清 statusPhase）。

### 4.5 开放问题 2 回答（打断边界）

> "AI 思考已结束但小爱还在念，此时打断算什么？"

**回答**：已覆盖。Dispatcher 在 broadcast 前 emit `UI.Status(phase="broadcasting")`，发送按钮保持停止态。用户点 ■ → 发 interrupt → WS route cancel（无 task 可 cancel，跳过）+ `interrupt_all` 停小爱 + 前端 `finalizeStreaming` 清 statusPhase。一个按钮，覆盖全部场景。

**超时保底**：若用户不点打断，估算超时后自动清 broadcasting（文本字数/4 + 5 秒缓冲），发送按钮恢复发送态。不精确但够用——HA 不暴露播报状态，精确检测无解。

---

## 5. W2 小爱直通模式

### 5.0 完全解耦原则

与 Phase 1 广播开关同机制：主程序不出现"小爱"字眼，不硬编码模式名。模式选择器由插件 manifest 声明 `ui_contribution`（`mode_option` 类型），框架通用渲染。没小爱插件 → 模式选择器只有默认 Aether → 永远走 LLM，零硬编码。

### 5.1 全链路新增

| 层 | 文件 | 改动 |
|----|------|------|
| RPC 协议 | `app/integration/rpc_protocol.py` | 加常量 `METHOD_ROUTE = "router.handle"` |
| SDK 基类 | `app/integration/sdk/router_base.py` | 新增 `InboundRouter` ABC：`async route(self, text: str) -> dict` |
| SDK 路由 | `app/integration/sdk/plugin_base.py` | `handle()` 加 `router.handle` 分支 → 调 `self.router.route(text)` |
| 门面 | `app/integration/integration_layer.py` | 加 `route_inbound(text, mode) -> dict`：找声明 `inbound_router` 的插件，RPC 调 `router.handle` |
| schema | `app/integration/schema.py` | `INBOUND_ROUTER` 枚举已有，无需改 |
| State/Action 注册表 | `app/routes/integration_routes.py` | STATE_HANDLERS 加 `current_mode`（默认 "aether"），ACTION_HANDLERS 加 `set_mode` |
| 小爱 manifest | `integrations/xiaoai/manifest.json` | 加 `inbound_router` capability + `ui_contribution`（mode_option） |
| 小爱插件 | `integrations/xiaoai/plugin.py` | 实现 `XiaoAiRouter.route(text)`：调 `notify.send_message` 到 `execute_text_directive` 实体 |
| 前端通用渲染 | `frontend/src/components/integration/ModeOptionContribution.vue` | 新增：渲染模式按钮，点击设 current_mode |
| 前端注册 | `frontend/src/components/integration/IntegrationSlot.vue` | TYPE_COMPONENTS 加 `mode_option` 映射 |

### 5.2 直通数据流

```
用户切"小爱"模式（插件贡献的 mode_option 按钮），输入"播放周杰伦的歌"
  → WS {type:"chat", mode:"xiaoai_direct", query:"播放周杰伦的歌", session_id:...}
  → WS route: mode != "aether" → current_task = create_task(_handle_direct(...))
  → _handle_direct:
      ├─ integration_layer.route_inbound("播放周杰伦的歌", "xiaoai_direct")
      │   → 找到声明 inbound_router 的插件（xiaoai）
      │   → RPC router.handle {text:"播放周杰伦的歌", mode:"xiaoai_direct"}
      │   → XiaoAiRouter.route("播放周杰伦的歌")
      │       → HA notify.send_message(entity_id=...execute_text_directive_a_5_5, message="播放周杰伦的歌")
      │       → 小爱原生执行（播放音乐）
      │       → return {ok:true, executed:"播放周杰伦的歌"}
      └─ emit Dialog.Finish(success=true, message="已转交处理")
  → 前端渲染助手消息"已转交处理"
```

**关键**：
- 直通模式完全绕过 LLM，不进 session history，不消耗 token
- WS route 只判断 `mode != "aether"`，不硬编码 "xiaoai_direct"——任何插件声明的 mode 都走 route_inbound
- "已转交处理"是通用文案，不写"小爱"

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

加 `inbound_router` capability + 模式选择器 UI 贡献：

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
],
"ui_contributions": [
  {
    "slot": "chat_input_toolbar",
    "type": "toggle_button",
    ... (Phase 1 广播开关，不变)
  },
  {
    "slot": "chat_mode_selector",
    "type": "mode_option",
    "props": {
      "label": "小爱",
      "icon": "🎵",
      "mode": "xiaoai_direct"
    },
    "state_key": "current_mode",
    "action": "set_mode"
  }
]
```

**mode_option 机制**：
- `props.mode`：该按钮代表的模式值（由插件声明，框架不硬编码）
- `props.label/icon`：按钮显示文字/图标（"小爱"字眼在 manifest 里，不在前端代码里）
- `state_key: "current_mode"`：框架全局状态，记录当前选中模式
- `action: "set_mode"`：点击时调此 action，参数 `{mode: props.mode}`

没小爱插件 → `chat_mode_selector` slot 无贡献 → 模式选择器只有框架默认的 Aether 按钮 → 零硬编码。

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

### 6.1 模式选择器（框架默认 + 插件贡献）

- **框架默认按钮**：ChatView 里写一个 "Aether" 按钮（框架默认模式，总是显示），点击设 `current_mode = "aether"`
- **插件贡献按钮**：`<IntegrationSlot slot="chat_mode_selector" />` 通用占位，渲染插件声明的 mode_option（小爱插件贡献"小爱"按钮）。没插件时此占位为空
- `current_mode` 通过 `GET /api/integrations/state/current_mode` 读取（默认 "aether"）
- 选中的按钮高亮，发送消息附带 `mode: current_mode`

```javascript
const chatMode = ref('aether')  // 框架默认，从 /api/integrations/state/current_mode 读

onMounted(async () => {
  try {
    const state = await apiGet('/api/integrations/state/current_mode')
    if (state?.value) chatMode.value = state.value
  } catch {}
})

function sendMessage() {
  ...
  ws.send(JSON.stringify({
    type: 'chat',
    query: text,
    session_id: sessionId.value,
    mode: chatMode.value,  // 'aether' 或插件声明的 mode 值
  }))
  ...
}
```

### 6.2 发送按钮变身打断按钮

- 不额外加按钮，复用现有发送按钮
- AI 生成中（`isStreaming`）→ 按钮变 ■ 红色，点击发 `{type:'interrupt'}` + `finalizeStreaming()`
- AI 空闲 → 按钮恢复 ▶，点击发消息
- 详见 §4.4

### 6.3 指令处理

`handleInstruction` 加对 `Dialog.Finish(success=false)` 的处理：
- 收到后 `finalizeStreaming()` 清空流式状态（目前 Finish 只处理 success=true 路径）
- 不显示错误（被打断不是错误）

### 6.4 直通响应显示

`_handle_direct` emit 的 `Dialog.Finish(success=true, message="已转交处理")` 走现有 Finish 处理路径，渲染为助手消息。（不写"小爱"——通用文案，任何 inbound_router 插件都适用）

### 6.5 ModeOptionContribution.vue（新增通用组件）

```vue
<!-- 渲染插件贡献的模式按钮，点击设 current_mode -->
<template>
  <button
    class="mode-option-btn"
    :class="{ active: isActive }"
    @click="selectMode"
    :title="contribution.props?.label"
  >
    <span v-if="contribution.props?.icon">{{ contribution.props.icon }}</span>
    {{ contribution.props?.label }}
  </button>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { apiGet, apiPost } from '../../utils/api'

const props = defineProps({ contribution: Object })
const isActive = ref(false)
const currentMode = ref('aether')

onMounted(async () => {
  await refreshState()
})

async function refreshState() {
  try {
    const state = await apiGet('/api/integrations/state/current_mode')
    currentMode.value = state?.value || 'aether'
    isActive.value = currentMode.value === props.contribution.props?.mode
  } catch {}
}

async function selectMode() {
  const mode = props.contribution.props?.mode
  await apiPost(`/api/integrations/action/set_mode`, { mode })
  currentMode.value = mode
  isActive.value = true
  // 通知 ChatView 更新（通过全局事件或 pinia store）
  window.dispatchEvent(new CustomEvent('mode-changed', { detail: { mode } }))
}
</script>
```

### 6.6 IntegrationSlot.vue 注册 mode_option

```javascript
const TYPE_COMPONENTS = {
  toggle_button: ToggleButtonContribution,
  mode_option: ModeOptionContribution,  // 新增
}
```

### 6.7 ChatView 监听模式变化

ChatView 监听 `mode-changed` 事件更新 `chatMode`（插件贡献的按钮点击后同步）：

```javascript
onMounted(() => {
  window.addEventListener('mode-changed', (e) => {
    chatMode.value = e.detail.mode
  })
})
```

---

## 7. 代码结构

### 7.1 归属划分

| 归属 | 内容 | 文件 |
|------|------|------|
| **框架（全局打断，独立需求）** | WS 改 task 式 + AI cancel + CancelledError 处理 + 发送按钮变身 + interrupt_all 调度 | `ws_routes.py`, `dispatcher.py`, `ChatView.vue`(发送按钮变身) |
| **插件 SDK（通用，非小爱）** | InboundRouter ABC + router.handle 路由 + METHOD_ROUTE 常量 + route_inbound + mode_option 渲染 + mode state/action | `sdk/router_base.py`, `sdk/plugin_base.py`, `rpc_protocol.py`, `integration_layer.py`, `integration_routes.py`, `IntegrationSlot.vue`, `ModeOptionContribution.vue` |
| **小爱插件（本次新增功能）** | XiaoAiRouter 直通 + manifest 加 inbound_router/mode_option | `integrations/xiaoai/plugin.py`, `integrations/xiaoai/manifest.json` |

> **注意**：小爱插件的 `interrupt()` 是 Phase 1 已交付代码，本次不改动。框架全局打断通过 `interrupt_all` 搭便车调用它。

### 7.2 新增文件

```
app/integration/sdk/router_base.py                       ← InboundRouter ABC（通用 SDK）
frontend/src/components/integration/ModeOptionContribution.vue  ← 模式按钮通用渲染（通用 SDK）
```

### 7.3 修改文件

```
# 框架（全局打断）
app/routes/ws_routes.py                     ← 循环改 task 式 + interrupt + 通用 mode 路由
app/agents/dispatcher.py                    ← _run_turn 加 CancelledError 处理
frontend/src/views/ChatView.vue             ← 发送按钮变身打断按钮 + 框架默认 Aether 按钮 + IntegrationSlot(mode_selector) + Finish(false) 处理

# 插件 SDK（通用）
app/integration/rpc_protocol.py             ← 加 METHOD_ROUTE 常量
app/integration/integration_layer.py        ← 加 route_inbound 方法
app/integration/sdk/plugin_base.py          ← handle() 加 router.handle 分支
app/routes/integration_routes.py            ← STATE_HANDLERS 加 current_mode，ACTION_HANDLERS 加 set_mode
frontend/src/components/integration/IntegrationSlot.vue  ← TYPE_COMPONENTS 加 mode_option

# 小爱插件（本次新增功能）
integrations/xiaoai/manifest.json           ← 加 inbound_router capability + mode_option ui_contribution
integrations/xiaoai/plugin.py               ← 加 XiaoAiRouter + 挂到 plugin
```

### 7.3 测试文件

### 7.4 测试文件

```
tests/test_ws_interrupt.py                  ← WS 循环打断行为（task 式 + interrupt 消息 + 自动打断 + 发新消息打断旧的）
tests/test_dispatcher_cancel.py             ← Dispatcher CancelledError 处理（emit Finish + interrupt_all）
tests/test_inbound_router.py                ← InboundRouter ABC + plugin_base router.handle 路由
tests/test_integration_layer_route.py       ← IntegrationLayer.route_inbound（通用，不硬编码插件）
tests/integrations/test_xiaoai_router.py    ← XiaoAiRouter 直通逻辑（不 spawn，mock HA caller）
tests/test_mode_state_routes.py             ← current_mode state + set_mode action 路由
```

### 7.5 解耦验证标准

- **删 `integrations/xiaoai/` 目录** → WS route 仍正常（mode != aether 时 route_inbound 返回 no router，前端显示"直通失败"，但主程序不崩）
- **删 `integrations/xiaoai/` 目录** → ChatView 模式选择器只有框架默认 Aether 按钮（`chat_mode_selector` slot 无贡献）
- **主程序 grep "小爱"/"xiaoai"** → 只在 manifest_loader 扫描目录和 config 的 disabled_plugins 里出现，业务逻辑零硬编码

---

## 8. 边界与错误处理

| 场景 | 行为 |
|------|------|
| 无任何插件时点打断 | 纯 cancel AI task，`interrupt_all` 无 sink 为 no-op，全局打断照常工作 |
| 直通模式但无 inbound_router 插件（未启用/未加载） | `route_inbound` 返回 `{ok:false, error:"no inbound router available"}`，前端显示"直通失败" |
| 直通模式插件 HA 调用失败 | 插件 `route()` 抛异常 → RPC 返回 error → WS emit `Dialog.Finish(success=false, message="直通执行失败")` |
| 打断时无活跃 task | `_cancel_current` 检查 `task.done()`，跳过 cancel，仍调 `interrupt_all`（no-op if 无播报）。此场景发生在"发新消息触发自动打断"时旧 task 刚结束 |
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
| 极致解耦 | 模式选择器走插件 UI 贡献（mode_option），主程序零硬编码，删插件零痕迹 |
