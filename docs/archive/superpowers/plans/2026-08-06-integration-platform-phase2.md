# Aether 集成平台 Phase 2 实现计划：全局打断 + 小爱直通模式（W2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aether 对话支持全局打断（AI 生成时发送按钮变身停止按钮，点击停 AI + 停小爱播报）+ 小爱直通模式（切"小爱"模式打字，文字原样转小爱原生执行，不进 LLM）。

**Architecture:** WS 聊天循环从阻塞式 `await dispatch_stream` 改为 task 式（`current_task` 局部变量），支持收 interrupt 消息和发新消息时 cancel 旧 task。Dispatcher `_run_turn` 加 `CancelledError` 处理（emit Finish + interrupt_all + 吞掉不 re-raise）。小爱直通走全链路 inbound_router（rpc 常量 → SDK ABC → plugin_base 路由 → IntegrationLayer.route_inbound → XiaoAiRouter 调 notify.send_message 到 execute_text_directive 实体）。模式选择器走插件 manifest ui_contribution（mode_option 类型），框架通用渲染，主程序零硬编码。

**Tech Stack:** Python 3.11 / FastAPI / asyncio / pytest / Vue 3 Composition API / stdio JSON-RPC 2.0

---

## Global Constraints

- Python 异步，所有 I/O 用 `async/await`，禁止阻塞调用。
- 测试框架 pytest，异步测试用 `asyncio.new_event_loop().run_until_complete(go())` 模式（非 asyncio_mode=auto，与 Phase 1 测试一致）。
- 测试导入用绝对路径 `from app.xxx import ...`（`tests/conftest.py` 已注入项目根到 `sys.path`）。
- 容器内开发：测试在 Docker 容器内跑（`docker exec aether-dev pytest ...`），PYTHONPATH=/aether。
- **完全解耦原则**：主程序不硬编码"小爱"/"xiaoai"。模式选择器走插件 manifest ui_contribution。删 `integrations/xiaoai/` → 模式选择器只有默认 Aether 按钮 + 主程序不崩 + 业务逻辑 grep 零硬编码。
- **打断归属**：全局打断是 Aether 框架独立需求（WS 改 task 式 + CancelledError 处理 + 发送按钮变身），与插件系统无关。打断小爱复用 Phase 1 的 `xiaoai/plugin.py:interrupt()`（已实现），插件不新增打断代码。
- 代码注释与日志保持中文（贴合现有风格）。
- 每个 Task 完成后立即运行测试，绿了再提交。
- 所有容器内命令前缀：`docker exec aether-dev`（假设容器名 aether-dev，按需调整）。

---

## File Structure

### 新增文件

```
app/integration/sdk/router_base.py                       ← InboundRouter ABC（通用 SDK）
frontend/src/components/integration/ModeOptionContribution.vue  ← 模式按钮通用渲染（通用 SDK）
```

### 修改文件

```
# 框架（全局打断）
app/routes/ws_routes.py                     ← 循环改 task 式 + interrupt + 通用 mode 路由
app/agents/dispatcher.py                    ← _run_turn 加 CancelledError 处理
app/schema/chat_schema.py                   ← Dialog.Finish 加 message 字段
frontend/src/views/ChatView.vue             ← 发送按钮变身 + 框架默认 Aether 按钮 + IntegrationSlot(mode_selector) + Finish(false) 处理

# 插件 SDK（通用）
app/integration/rpc_protocol.py             ← 加 METHOD_ROUTE 常量
app/integration/integration_layer.py        ← 加 route_inbound 方法
app/integration/sdk/plugin_base.py          ← handle() 加 router.handle 分支 + self.routers
app/routes/integration_routes.py            ← STATE_HANDLERS 加 current_mode，ACTION_HANDLERS 加 set_mode
app/integration/config_helper.py            ← 加 get_current_mode / set_current_mode
frontend/src/components/integration/IntegrationSlot.vue  ← TYPE_COMPONENTS 加 mode_option

# 小爱插件（本次新增功能）
integrations/xiaoai/manifest.json           ← 加 inbound_router capability + mode_option ui_contribution
integrations/xiaoai/plugin.py               ← 加 XiaoAiRouter + 挂到 plugin.routers
```

### 测试文件

```
tests/test_rpc_protocol_route.py            ← METHOD_ROUTE 常量
tests/test_inbound_router_base.py           ← InboundRouter ABC + plugin_base router.handle 路由
tests/test_integration_layer_route.py       ← IntegrationLayer.route_inbound（通用，不硬编码插件）
tests/test_dispatcher_cancel.py             ← Dispatcher CancelledError 处理（emit Finish + interrupt_all）
tests/test_ws_interrupt.py                  ← WS 循环打断行为（task 式 + interrupt + 自动打断 + mode 路由）
tests/test_mode_state_routes.py             ← current_mode state + set_mode action
tests/integrations/test_xiaoai_router.py    ← XiaoAiRouter 直通逻辑（不 spawn，mock HA caller）
```

### 职责边界

| 文件 | 唯一职责 |
|------|---------|
| `sdk/router_base.py` | InboundRouter ABC 定义，无 I/O |
| `sdk/plugin_base.py` | handle() 按 method 路由到 sinks/routers，不实现具体逻辑 |
| `rpc_protocol.py` | RPC 方法名常量 + 消息构造纯函数 |
| `integration_layer.py:route_inbound` | 找声明 inbound_router 的插件，RPC 调 router.handle |
| `dispatcher.py` | _run_turn 加 CancelledError 捕获，emit Finish + interrupt_all |
| `ws_routes.py` | WS 循环 task 化，interrupt/新消息 cancel 旧 task，mode 路由 |
| `config_helper.py` | current_mode 读写（持久化到 config） |
| `xiaoai/plugin.py:XiaoAiRouter` | 调 notify.send_message 到 execute_text_directive 实体 |

---

## Task 1: RPC 协议加 METHOD_ROUTE 常量

**Files:**
- Modify: `app/integration/rpc_protocol.py:9-14`
- Test: `tests/test_rpc_protocol_route.py`

**Interfaces:**
- Produces: `METHOD_ROUTE = "router.handle"` 常量，供 plugin_base / integration_layer 引用

- [ ] **Step 1: Write the failing test**

```python
"""METHOD_ROUTE 常量测试。"""

from app.integration.rpc_protocol import METHOD_ROUTE, METHOD_SPEAK, build_request


def test_method_route_constant():
    assert METHOD_ROUTE == "router.handle"


def test_build_request_with_route():
    req = build_request(msg_id=1, method=METHOD_ROUTE, params={"text": "hi"})
    assert req["method"] == "router.handle"
    assert req["params"] == {"text": "hi"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec aether-dev pytest tests/test_rpc_protocol_route.py -v`
Expected: FAIL with `ImportError: cannot import name 'METHOD_ROUTE'`

- [ ] **Step 3: Write minimal implementation**

在 `app/integration/rpc_protocol.py` 的方法名常量区（METHOD_INTERRUPT 之后）加一行：

```python
METHOD_ROUTE = "router.handle"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec aether-dev pytest tests/test_rpc_protocol_route.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/integration/rpc_protocol.py tests/test_rpc_protocol_route.py
git commit -m "feat(integration): 加 METHOD_ROUTE 常量（router.handle）"
```

---

## Task 2: InboundRouter ABC + plugin_base 路由分发

**Files:**
- Create: `app/integration/sdk/router_base.py`
- Modify: `app/integration/sdk/plugin_base.py:5,16-18,24-39`
- Test: `tests/test_inbound_router_base.py`

**Interfaces:**
- Consumes: `METHOD_ROUTE` from Task 1
- Produces: `InboundRouter` ABC（`async route(self, text: str) -> dict`）；`IntegrationPlugin.routers: list[InboundRouter]` 属性；`IntegrationPlugin.handle()` 对 `METHOD_ROUTE` 调 `self.routers[0].route(text)`

- [ ] **Step 1: Write the failing test**

```python
"""InboundRouter ABC + plugin_base router.handle 路由测试。"""

import asyncio

from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.router_base import InboundRouter
from app.integration.rpc_protocol import METHOD_ROUTE, METHOD_SPEAK


class FakeRouter(InboundRouter):
    """测试用 router，记录收到的 text。"""
    def __init__(self):
        self.received: list[str] = []

    async def route(self, text: str) -> dict:
        self.received.append(text)
        return {"ok": True, "executed": text}


class FakePlugin(IntegrationPlugin):
    def setup(self, manifest_dict):
        self.manifest = manifest_dict
        self.routers = [FakeRouter()]


def test_inbound_router_is_abstract():
    """InboundRouter 不能直接实例化（抽象基类）。"""
    import pytest
    with pytest.raises(TypeError):
        InboundRouter()


def test_plugin_handles_router_handle():
    """plugin.handle(METHOD_ROUTE) 调用 router.route。"""
    plugin = FakePlugin()
    plugin.setup({})

    async def go():
        result = await plugin.handle(METHOD_ROUTE, {"text": "播放音乐"})
        assert result == {"ok": True, "executed": "播放音乐"}
        assert plugin.routers[0].received == ["播放音乐"]

    asyncio.new_event_loop().run_until_complete(go())


def test_plugin_router_handle_no_router_registered():
    """没注册 router 时返回 error。"""
    plugin = IntegrationPlugin()  # 没设 routers

    async def go():
        result = await plugin.handle(METHOD_ROUTE, {"text": "hi"})
        assert "error" in result

    asyncio.new_event_loop().run_until_complete(go())


def test_plugin_unknown_method_still_errors():
    """未知方法仍返回 error。"""
    plugin = IntegrationPlugin()

    async def go():
        result = await plugin.handle("bogus.method", {})
        assert "error" in result

    asyncio.new_event_loop().run_until_complete(go())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec aether-dev pytest tests/test_inbound_router_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.integration.sdk.router_base'`

- [ ] **Step 3: Create router_base.py**

```python
"""InboundRouter 抽象基类 —— 插件入站路由能力实现此接口。"""

from abc import ABC, abstractmethod


class InboundRouter(ABC):
    """入站路由能力契约。

    用户在 ChatView 切模式后，文字经 InboundRouter 路由到插件处理。
    典型：小爱直通模式——文字原样转小爱原生执行（execute=true），不进 LLM。
    """

    @abstractmethod
    async def route(self, text: str) -> dict:
        """处理入站文字。返回执行结果 dict（至少含 ok 或错误信息）。"""
        ...
```

- [ ] **Step 4: Modify plugin_base.py**

修改 `app/integration/sdk/plugin_base.py`，加 router 导入 + `self.routers` 初始化 + `handle()` 加 `METHOD_ROUTE` 分支：

```python
"""IntegrationPlugin 基类 —— 插件进程内继承。"""

from typing import Any

from ..rpc_protocol import METHOD_INTERRUPT, METHOD_ROUTE, METHOD_SPEAK
from .sink_base import OutputSink


class IntegrationPlugin:
    """插件基类。

    子类在 setup() 里根据 manifest 构建 sinks（output_sink）和
    routers（inbound_router）。
    handle() 按 JSON-RPC method 路由到对应能力。
    """

    def __init__(self) -> None:
        self.manifest: dict[str, Any] = {}
        self.sinks: list[OutputSink] = []
        self.routers: list[Any] = []  # list[InboundRouter]，用 Any 避免循环导入

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        """子类实现：解析 manifest_dict，构建 sinks/routers 等。"""
        self.manifest = manifest_dict

    async def handle(self, method: str, params: dict[str, Any]) -> dict:
        """按 method 分发到对应能力。未知方法返回 error。"""
        if method == METHOD_SPEAK:
            if not self.sinks:
                return {"error": "no sink registered"}
            sink = self.sinks[0]
            return await sink.speak(
                text=params.get("text", ""),
                msg_id=params.get("msg_id", ""),
            )
        if method == METHOD_INTERRUPT:
            if not self.sinks:
                return {"error": "no sink registered"}
            sink = self.sinks[0]
            return await sink.interrupt()
        if method == METHOD_ROUTE:
            if not self.routers:
                return {"error": "no router registered"}
            router = self.routers[0]
            return await router.route(text=params.get("text", ""))
        return {"error": f"unknown method: {method}"}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker exec aether-dev pytest tests/test_inbound_router_base.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add app/integration/sdk/router_base.py app/integration/sdk/plugin_base.py tests/test_inbound_router_base.py
git commit -m "feat(integration): InboundRouter ABC + plugin_base router.handle 路由"
```

---

## Task 3: IntegrationLayer.route_inbound（通用入站路由）

**Files:**
- Modify: `app/integration/integration_layer.py`（加 route_inbound 方法）
- Test: `tests/test_integration_layer_route.py`

**Interfaces:**
- Consumes: `METHOD_ROUTE` from Task 1，`CapabilityType.INBOUND_ROUTER`（已有），`Manifest.has_capability`（已有）
- Produces: `IntegrationLayer.route_inbound(text: str, mode: str) -> dict`——找声明 inbound_router 的存活插件，RPC 调 router.handle。无插件返回 `{ok: False, error: "no inbound router available"}`

- [ ] **Step 1: Write the failing test**

```python
"""IntegrationLayer.route_inbound 测试（通用，不硬编码插件）。"""

import asyncio

from app.integration.integration_layer import IntegrationLayer

INTEGRATIONS_TESTS_DIR = "tests/integrations"


def test_route_inbound_no_plugins_returns_error():
    """无 inbound_router 插件时返回 error。"""
    layer = IntegrationLayer(plugin_dir="nonexistent_dir")

    async def go():
        result = await layer.route_inbound("播放音乐", "some_mode")
        assert result["ok"] is False
        assert "no inbound router" in result["error"]

    asyncio.new_event_loop().run_until_complete(go())
```

> **注**：`tests/integrations/echo` 只有 output_sink，没有 inbound_router，所以即使用真实目录也测了"无 router"路径。Task 7 会加一个有 inbound_router 的测试插件做 spawn 集成测试。

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec aether-dev pytest tests/test_integration_layer_route.py -v`
Expected: FAIL with `AttributeError: 'IntegrationLayer' object has no attribute 'route_inbound'`

- [ ] **Step 3: Write minimal implementation**

在 `app/integration/integration_layer.py` 加 `route_inbound` 方法（放在 `start_plugin` 之后）：

```python
    async def route_inbound(self, text: str, mode: str) -> dict:
        """将入站文字路由到声明 inbound_router 的插件（通用，不硬编码插件名）。

        找第一个声明了 inbound_router 且存活的插件，RPC 调 router.handle。
        无插件 / 全禁用 → 返回 {ok: False, error: ...}。
        V1 只有一个 inbound_router（小爱），直接调第一个匹配。
        """
        from .config_helper import get_disabled_plugins
        from .manifest_loader import load_manifests
        from .rpc_protocol import METHOD_ROUTE
        from .schema import CapabilityType

        disabled = get_disabled_plugins()
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version,
                                   disabled=disabled)
        for manifest in manifests:
            if manifest.has_capability(CapabilityType.INBOUND_ROUTER):
                proc = self._supervisor.get_process(manifest.id)
                if proc and proc.is_alive:
                    try:
                        return await proc.call(METHOD_ROUTE, {"text": text, "mode": mode})
                    except Exception as exc:
                        logger.warning("路由到插件 %s 失败: %s", manifest.id, exc)
                        return {"ok": False, "error": f"插件 {manifest.id} 路由失败"}
        return {"ok": False, "error": "no inbound router available"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec aether-dev pytest tests/test_integration_layer_route.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/integration/integration_layer.py tests/test_integration_layer_route.py
git commit -m "feat(integration): IntegrationLayer.route_inbound 通用入站路由"
```

---

## Task 4: Dialog.Finish 加 message 字段

**Files:**
- Modify: `app/schema/chat_schema.py:127-130`
- Test: `tests/test_dialog_finish_message.py`

**Interfaces:**
- Produces: `Dialog.Finish` 新增 `message: str = ""` 字段，供打断和直通回传文案

- [ ] **Step 1: Write the failing test**

```python
"""Dialog.Finish message 字段测试。"""

from app.schema.chat_schema import Dialog


def test_finish_has_message_field():
    """Finish 可带 message（默认空）。"""
    finish = Dialog.Finish(success=True)
    assert finish.message == ""

    finish_with_msg = Dialog.Finish(success=True, message="已转交处理")
    assert finish_with_msg.message == "已转交处理"


def test_finish_serializes_message():
    """Finish 序列化包含 message。"""
    finish = Dialog.Finish(success=False, message="被打断")
    dumped = finish.model_dump()
    assert dumped["message"] == "被打断"
    assert dumped["success"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec aether-dev pytest tests/test_dialog_finish_message.py -v`
Expected: FAIL with `AttributeError: ... object has no attribute 'message'`（或 TypeError 多了参数）

- [ ] **Step 3: Write minimal implementation**

修改 `app/schema/chat_schema.py` 的 `Dialog.Finish`：

```python
    class Finish(InstructionPayload):
        NAMESPACE: ClassVar[str] = "Dialog"
        NAME: ClassVar[str] = "Finish"
        success: bool
        message: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec aether-dev pytest tests/test_dialog_finish_message.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run full test suite to verify no regression**

Run: `docker exec aether-dev pytest tests/ -x -q 2>&1 | tail -5`
Expected: 全绿（Finish 现有调用不传 message，默认 "" 兼容）

- [ ] **Step 6: Commit**

```bash
git add app/schema/chat_schema.py tests/test_dialog_finish_message.py
git commit -m "feat(schema): Dialog.Finish 加 message 字段（打断/直通回传文案）"
```

---

## Task 5: Dispatcher CancelledError 处理 + broadcasting status

**Files:**
- Modify: `app/agents/dispatcher.py:600-661`（_run_turn 三处 run_agent_streaming 加 CancelledError 捕获）+ `727-731`（broadcast hook 加 broadcasting status emit + 超时清除）
- Test: `tests/test_dispatcher_cancel.py`

**Interfaces:**
- Consumes: `self._sink_manager`（已有，dispatcher.py:222），`Dialog.Finish.message`（Task 4），`UI.Status`（已有，chat_schema.py:142）
- Produces: Dispatcher _run_turn 捕获 CancelledError → emit `Dialog.Finish(success=False)` + `interrupt_all()` + 正常 return；broadcast hook 前 emit `UI.Status(phase="broadcasting")` + 超时估算清除

- [ ] **Step 1: Write the failing test**

```python
"""Dispatcher CancelledError 处理测试。

打断时 task.cancel() 触发 CancelledError，Dispatcher 应：
1. emit Dialog.Finish(success=False) 让前端不卡
2. 调 sink_manager.interrupt_all() 停播报
3. 吞掉 CancelledError 正常返回（不 re-raise）
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.agents.dispatcher import Dispatcher
from app.schema.chat_schema import Event, Nlp


def _make_dispatcher(sink_manager=None):
    """构造一个 Dispatcher，mock 掉依赖。"""
    session_store = MagicMock()
    session_store.get_or_create = AsyncMock(return_value=MagicMock(
        history_events=[], history_instructions=[], model_messages=[],
    ))
    session_store.store_session = AsyncMock()

    agent = AsyncMock()
    # run_agent_streaming mock 成一个会一直 yield 的 async generator（模拟长时间思考）
    async def _endless_stream(*a, **kw):
        try:
            while True:
                await asyncio.sleep(0.01)
                yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock()}}
        except asyncio.CancelledError:
            raise

    dispatcher = Dispatcher(
        session_store=session_store,
        agent_factory=MagicMock(return_value=agent),
        validator=MagicMock(should_retry=AsyncMock(return_value=False), _max_retries=0),
        sink_manager=sink_manager,
    )
    # 注入 mock 的 run_agent_streaming
    import app.agents.dispatcher as disp_mod
    disp_mod.run_agent_streaming = _endless_stream
    return dispatcher


def test_dispatcher_cancel_emits_finish_and_interrupts():
    """cancel task 后 Dispatcher emit Finish(success=False) + interrupt_all。"""
    sink_manager = MagicMock()
    sink_manager.interrupt_all = AsyncMock()

    dispatcher = _make_dispatcher(sink_manager=sink_manager)

    sent = []
    async def ws_send(msg):
        sent.append(msg)

    event = Event.build_event(Nlp.Request(query="test"), request_id="r1")

    async def go():
        task = asyncio.create_task(
            dispatcher.dispatch_stream(event, ws_send, user_id="u1")
        )
        await asyncio.sleep(0.05)  # 让它开始思考
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # 不应该逃逸——Dispatcher 应吞掉

    asyncio.new_event_loop().run_until_complete(go())

    # 验证 emit 了 Finish(success=False)
    finish_msgs = [m for m in sent
                   if m.get("header", {}).get("namespace") == "Dialog"
                   and m.get("header", {}).get("name") == "Finish"]
    assert len(finish_msgs) >= 1
    assert finish_msgs[-1]["payload"]["success"] is False

    # 验证调了 interrupt_all
    sink_manager.interrupt_all.assert_called()


def test_dispatcher_cancel_no_sink_manager_still_emits_finish():
    """无 sink_manager 时 cancel 仍 emit Finish（纯框架打断，无插件也工作）。"""
    dispatcher = _make_dispatcher(sink_manager=None)

    sent = []
    async def ws_send(msg):
        sent.append(msg)

    event = Event.build_event(Nlp.Request(query="test"), request_id="r2")

    async def go():
        task = asyncio.create_task(
            dispatcher.dispatch_stream(event, ws_send, user_id="u1")
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())

    finish_msgs = [m for m in sent
                   if m.get("header", {}).get("namespace") == "Dialog"
                   and m.get("header", {}).get("name") == "Finish"]
    assert len(finish_msgs) >= 1
    assert finish_msgs[-1]["payload"]["success"] is False


def test_dispatcher_broadcasts_emit_broadcasting_status():
    """broadcast 前 emit UI.Status(phase=broadcasting)，让发送按钮保持停止态。

    HA 不暴露小爱播报状态，用 broadcasting status 让前端知道"还在念"。
    """
    sink_manager = MagicMock()
    sink_manager.broadcast = AsyncMock()
    sink_manager.interrupt_all = AsyncMock()

    # 用一个快速完成的 stream（让 turn 走到 broadcast hook）
    async def _fast_stream(*a, **kw):
        yield {"event": "on_chat_model_stream",
               "data": {"chunk": MagicMock(content="测试回复内容")}}

    dispatcher = _make_dispatcher(sink_manager=sink_manager)
    import app.agents.dispatcher as disp_mod
    disp_mod.run_agent_streaming = _fast_stream

    sent = []
    async def ws_send(msg):
        sent.append(msg)

    event = Event.build_event(Nlp.Request(query="test"), request_id="r3")

    async def go():
        await dispatcher.dispatch_stream(event, ws_send, user_id="u1")

    asyncio.new_event_loop().run_until_complete(go())

    # 验证 emit 了 UI.Status(phase=broadcasting)
    status_msgs = [m for m in sent
                   if m.get("header", {}).get("namespace") == "UI"
                   and m.get("header", {}).get("name") == "Status"]
    phases = [m["payload"]["phase"] for m in status_msgs]
    assert "broadcasting" in phases  # broadcast 前 emit 了 broadcasting

    # 验证调了 broadcast
    sink_manager.broadcast.assert_called()
```

> **注**：`_make_dispatcher` 的 `_endless_stream` 会被 `test_dispatcher_broadcasts_emit_broadcasting_status` 覆盖为 `_fast_stream`（快速完成让 turn 走到 broadcast hook）。需确认 `_fast_stream` 的 chunk mock 能让 `state.final_content` 被设置——可能需要调 handler mock。若 handler 逻辑复杂，简化测试：直接 mock `state.final_content`。实现时按实际 handler 行为调整测试。

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec aether-dev pytest tests/test_dispatcher_cancel.py -v`
Expected: FAIL（CancelledError 逃逸，没 emit Finish，或没调 interrupt_all，或没 emit broadcasting）

- [ ] **Step 3: Write minimal implementation**

修改 `app/agents/dispatcher.py` 的 `_run_turn`。在三处 `run_agent_streaming` 调用的 `try/except Exception` 块中，**在 `except Exception` 之前加 `except asyncio.CancelledError`**。

需要先确认文件顶部已 `import asyncio`（dispatcher.py 用到 interactive_priority 等，确认有 asyncio）。

第一处（约 602-609 行，主轮）：
```python
        with interactive_priority.hold():
            try:
                async for stream_event in run_agent_streaming(agent, lc_messages, session):
                    await handler(stream_event)
            except asyncio.CancelledError:
                # 被用户打断：通知前端 + 停所有 sink（有插件就停，没有也无所谓）
                await emit(
                    Instruction.build_instruction(
                        Dialog.Finish(success=False), request_id, session_id,
                    )
                )
                if self._sink_manager is not None:
                    try:
                        await self._sink_manager.interrupt_all()
                    except Exception as exc:
                        logger.warning("打断时停 sink 失败（不影响）: %s", exc)
                logger.info("Turn %s 被用户打断", request_id)
                return  # 吞掉 CancelledError，正常返回
            except Exception as e:
                await self._emit_turn_error(e, state, emit, request_id, session_id, path)
```

第二处（约 628-637 行，失败重试轮）—— 在 `try` 块的 `except Exception` 前加同样的 `except asyncio.CancelledError`（emit Finish + interrupt_all + return）。

第三处（约 656-661 行，validator 重试轮）—— 同上。

> **DRY 注意**：三处 except 块逻辑相同。可抽一个 `_handle_cancelled(emit, request_id, session_id)` 辅助方法避免重复。但为保持改动最小，先内联；若审查时觉得重复，再抽取。

为避免重复，实际实现时在 Dispatcher 类里加一个辅助方法：

```python
    async def _handle_cancelled(self, emit, request_id: str, session_id: str) -> None:
        """处理被打断：emit Finish(success=False) + 停所有 sink。吞掉 CancelledError。"""
        await emit(
            Instruction.build_instruction(
                Dialog.Finish(success=False), request_id, session_id,
            )
        )
        if self._sink_manager is not None:
            try:
                await self._sink_manager.interrupt_all()
            except Exception as exc:
                logger.warning("打断时停 sink 失败（不影响）: %s", exc)
        logger.info("Turn %s 被用户打断", request_id)
```

三处 except 都调 `await self._handle_cancelled(emit, request_id, session_id); return`。

**再修改 broadcast hook（约 727-731 行）**——broadcast 前 emit broadcasting status + 超时清除：

```python
        # ── 集成广播钩子：把最终回复同步到 output_sink（如小爱）──
        # 失败不阻塞主流程，仅记录警告（用户已通过 WS 收到文字回复）。
        if state.final_content and self._sink_manager is not None:
            try:
                # emit broadcasting status：让前端发送按钮保持停止态（可打断小爱）
                # HA 不暴露播报状态，用 broadcasting phase 让用户知道"还在念"
                await emit(
                    Instruction.build_instruction(
                        UI.Status(phase="broadcasting"), request_id, session_id,
                    )
                )
                await self._sink_manager.broadcast(state.final_content, request_id)
                # 估算超时后清除 broadcasting（中文约 4 字/秒 + 5 秒缓冲）
                # 超时后发送按钮恢复发送态（无精确检测，估算够用）
                est_seconds = max(len(state.final_content) / 4, 3) + 5
                asyncio.create_task(self._clear_broadcasting_after(
                    emit, request_id, session_id, est_seconds))
            except Exception as exc:
                logger.warning("集成广播失败（不影响主流程）: %s", exc)
```

加辅助方法（Dispatcher 类内，`_handle_cancelled` 之后）：

```python
    async def _clear_broadcasting_after(self, emit, request_id: str,
                                        session_id: str, delay: float) -> None:
        """延迟清除 broadcasting status（估算播报完毕后发送按钮恢复发送态）。

        HA 不暴露小爱播报状态，用超时估算。不精确但够用——
        用户也可在超时前点 ■ 打断（interrupt 会清前端 statusPhase）。
        """
        try:
            await asyncio.sleep(delay)
            await emit(
                Instruction.build_instruction(
                    UI.Status(phase=""), request_id, session_id,
                )
            )
        except Exception:
            pass  # 连接已断 / task 被 cancel，忽略
```

> **import 确认**：`UI` 已在 dispatcher.py 顶部 import（现有代码 emit UI.Status thinking/retrying）。`asyncio` 需确认已 import。

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec aether-dev pytest tests/test_dispatcher_cancel.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run full test suite to verify no regression**

Run: `docker exec aether-dev pytest tests/ -x -q 2>&1 | tail -5`
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add app/agents/dispatcher.py tests/test_dispatcher_cancel.py
git commit -m "feat(dispatcher): CancelledError 处理 + broadcasting status（打断 + 播报态）"
```

---

## Task 6: WS 循环改 task 式 + interrupt + mode 路由

**Files:**
- Modify: `app/routes/ws_routes.py:30-53`（chat_ws 的 while 循环）
- Test: `tests/test_ws_interrupt.py`

**Interfaces:**
- Consumes: `container.dispatcher.dispatch_stream`（已有），`container.integration_layer.route_inbound`（Task 3），`container.integration_layer.sink_manager.interrupt_all`（已有）
- Produces: WS chat 循环 task 式化——`current_task` 局部变量，收 `{type:"interrupt"}` cancel + interrupt_all，收 `{type:"chat"}` 自动打断旧的再 spawn 新的（mode=aether 走 dispatch，mode!=aether 走 route_inbound）

- [ ] **Step 1: Write the failing test**

```python
"""WS 循环打断行为测试。

验证：
1. 收到 interrupt 消息 → cancel current_task + interrupt_all
2. 发新消息时自动打断旧的
3. mode=aether 走 dispatch_stream
4. mode!=aether 走 route_inbound（不硬编码模式名）
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_container(dispatch_stream_fn=None, route_inbound_fn=None,
                    interrupt_all_fn=None, integration_enabled=True):
    """构造 mock container。"""
    container = MagicMock()
    container.dispatcher = MagicMock()
    if dispatch_stream_fn:
        container.dispatcher.dispatch_stream = dispatch_stream_fn
    else:
        container.dispatcher.dispatch_stream = AsyncMock()

    if integration_enabled:
        container.integration_layer = MagicMock()
        container.integration_layer.route_inbound = route_inbound_fn or AsyncMock(
            return_value={"ok": True, "executed": "text"})
        container.integration_layer.sink_manager = MagicMock()
        container.integration_layer.sink_manager.interrupt_all = interrupt_all_fn or AsyncMock()
    else:
        container.integration_layer = None

    return container


class FakeWebSocket:
    """模拟 WebSocket，queue 驱动 receive_json。"""
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def accept(self):
        pass

    async def receive_json(self):
        if not self._messages:
            # 阻塞直到被 cancel
            await asyncio.sleep(100)
        return self._messages.pop(0)

    async def send_json(self, data):
        self.sent.append(data)


def test_interrupt_cancels_task_and_interrupts_sinks():
    """收到 interrupt → cancel current_task + interrupt_all。"""
    dispatch_started = asyncio.Event()

    async def slow_dispatch(event, ws_send, user_id=""):
        dispatch_started.set()
        await asyncio.sleep(100)  # 模拟长时间思考

    interrupt_calls = []
    async def interrupt_all():
        interrupt_calls.append(True)

    container = _make_container(
        dispatch_stream_fn=slow_dispatch,
        interrupt_all_fn=interrupt_all,
    )

    ws = FakeWebSocket([
        {"type": "chat", "query": "hello", "session_id": "s1"},
        {"type": "interrupt"},
    ])

    from app.routes.ws_routes import _chat_loop

    async def go():
        task = asyncio.create_task(_chat_loop(ws, container, "u1"))
        await asyncio.wait_for(dispatch_started.wait(), timeout=2.0)
        await asyncio.sleep(0.1)  # 让 interrupt 处理
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())

    assert len(interrupt_calls) >= 1  # 调了 interrupt_all


def test_new_message_auto_interrupts_old():
    """发新消息 → 自动 cancel 旧 task。"""
    first_dispatch = asyncio.Event()

    async def slow_dispatch(event, ws_send, user_id=""):
        first_dispatch.set()
        await asyncio.sleep(100)

    second_calls = []
    async def fast_dispatch(event, ws_send, user_id=""):
        second_calls.append(event.payload.get("query"))

    # 第一次慢，第二次快——用 side_effect 切换
    call_count = [0]
    async def dispatch(event, ws_send, user_id=""):
        call_count[0] += 1
        if call_count[0] == 1:
            first_dispatch.set()
            await asyncio.sleep(100)
        else:
            second_calls.append(event.payload.get("query"))

    container = _make_container(dispatch_stream_fn=dispatch)

    ws = FakeWebSocket([
        {"type": "chat", "query": "first", "session_id": "s1"},
        {"type": "chat", "query": "second", "session_id": "s1"},
    ])

    from app.routes.ws_routes import _chat_loop

    async def go():
        task = asyncio.create_task(_chat_loop(ws, container, "u1"))
        await asyncio.wait_for(first_dispatch.wait(), timeout=2.0)
        await asyncio.sleep(0.2)  # 让第二条消息处理
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())

    assert len(second_calls) == 1  # 第二条消息的 dispatch 被调了
    assert second_calls[0] == "second"


def test_non_aether_mode_routes_to_inbound():
    """mode != aether → 走 route_inbound（不硬编码模式名）。"""
    route_calls = []
    async def route_inbound(text, mode):
        route_calls.append((text, mode))
        return {"ok": True, "executed": text}

    container = _make_container(route_inbound_fn=route_inbound)

    ws = FakeWebSocket([
        {"type": "chat", "mode": "xiaoai_direct", "query": "播放音乐",
         "session_id": "s1"},
    ])

    from app.routes.ws_routes import _chat_loop

    async def go():
        task = asyncio.create_task(_chat_loop(ws, container, "u1"))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())

    assert len(route_calls) == 1
    assert route_calls[0] == ("播放音乐", "xiaoai_direct")


def test_interrupt_with_no_integration_layer_still_works():
    """无集成平台时 interrupt 也不崩（纯框架打断）。"""
    container = _make_container(integration_enabled=False)

    async def slow_dispatch(event, ws_send, user_id=""):
        await asyncio.sleep(100)

    container.dispatcher.dispatch_stream = slow_dispatch

    ws = FakeWebSocket([
        {"type": "chat", "query": "hello", "session_id": "s1"},
        {"type": "interrupt"},
    ])

    from app.routes.ws_routes import _chat_loop

    async def go():
        task = asyncio.create_task(_chat_loop(ws, container, "u1"))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())
    # 不崩就算通过
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec aether-dev pytest tests/test_ws_interrupt.py -v`
Expected: FAIL with `ImportError: cannot import name '_chat_loop'`

- [ ] **Step 3: Write minimal implementation**

重构 `app/routes/ws_routes.py`。把 chat_ws 的循环体抽成独立的 `_chat_loop` 函数（便于测试，不用起真实 WebSocket）：

```python
"""WebSocket 路由 — 聊天和文档助手 WebSocket 端点。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..container import get_container
from ..core.tracing import new_request_id, set_request_id
from ..schema.chat_schema import Event, Nlp, Dialog, Instruction

logger = logging.getLogger(__name__)

router = APIRouter()


async def _cancel_current(task: asyncio.Task | None, container) -> None:
    """取消当前活跃 task + 中断所有 sink 播报。"""
    if task is not None and not task.done():
        task.cancel()
        try:
            await task  # 等 CancelledError 传播完毕（Dispatcher 内部已处理）
        except (asyncio.CancelledError, Exception):
            pass  # task 内部异常已自己处理
    # 停所有 sink（即使 task 已结束，小爱可能还在念）
    layer = getattr(container, "integration_layer", None)
    if layer is not None and layer.sink_manager is not None:
        await layer.sink_manager.interrupt_all()


async def _run_dispatch(container, event, ws_send, user_id: str) -> None:
    """包装 dispatch_stream，确保异常不逃逸到 WS 循环。"""
    try:
        await container.dispatcher.dispatch_stream(event, ws_send, user_id=user_id)
    except asyncio.CancelledError:
        pass  # Dispatcher 内部已 emit Finish + interrupt
    except Exception:
        pass  # Dispatcher 内部已有异常处理


async def _handle_direct(websocket, container, payload, rid: str, user_id: str) -> None:
    """直通模式：文字路由到 inbound_router 插件（通用，不硬编码任何插件）。"""
    from ..core.tracing import set_request_id
    set_request_id(rid)
    try:
        text = payload.get("query", "")
        mode = payload.get("mode", "")
        layer = getattr(container, "integration_layer", None)
        if layer is None:
            await websocket.send_json(
                Instruction.build_instruction(
                    Dialog.Finish(success=False, message="直通失败"),
                    rid, payload.get("session_id", ""),
                ).model_dump()
            )
            return
        result = await layer.route_inbound(text, mode)
        if result.get("ok"):
            await websocket.send_json(
                Instruction.build_instruction(
                    Dialog.Finish(success=True, message="已转交处理"),
                    rid, payload.get("session_id", ""),
                ).model_dump()
            )
        else:
            await websocket.send_json(
                Instruction.build_instruction(
                    Dialog.Finish(success=False,
                                  message=result.get("error", "直通失败")),
                    rid, payload.get("session_id", ""),
                ).model_dump()
            )
    except Exception:
        await websocket.send_json(
            Instruction.build_instruction(
                Dialog.Finish(success=False, message="直通执行失败"),
                rid, payload.get("session_id", ""),
            ).model_dump()
        )
    finally:
        set_request_id("-")


async def _chat_loop(websocket, container, user_id: str) -> None:
    """聊天 WS 主循环（task 式，支持打断 + mode 路由）。

    current_task 是局部变量：一个连接同时只有一个活跃 task。
    收 interrupt / 新消息时 cancel 旧的 + interrupt_all。
    """
    current_task: asyncio.Task | None = None
    while True:
        payload = await websocket.receive_json()

        if payload.get("type") == "pong":
            continue

        if payload.get("type") == "interrupt":
            await _cancel_current(current_task, container)
            current_task = None
            continue

        if payload.get("type") == "chat":
            # 自动打断旧的（类 ChatGPT 体验）
            await _cancel_current(current_task, container)

            mode = payload.get("mode", "aether")
            rid = payload.get("request_id") or new_request_id()

            if mode == "aether":
                set_request_id(rid)
                event = Event.build_event(
                    Nlp.Request(query=payload.get("query", "")),
                    request_id=rid,
                    session_id=payload.get("session_id"),
                )
                current_task = asyncio.create_task(
                    _run_dispatch(container, event, websocket.send_json, user_id)
                )
                set_request_id("-")
            else:
                # 任意非默认模式：通用路由到 inbound_router（不硬编码模式名）
                current_task = asyncio.create_task(
                    _handle_direct(websocket, container, payload, rid, user_id)
                )


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    """WebSocket 聊天端点。"""
    from ..main import _ws_verify_token, _ws_heartbeat
    user_id = await _ws_verify_token(websocket)
    if user_id is None:
        return
    container = get_container()
    await websocket.accept()

    heartbeat_task = asyncio.create_task(_ws_heartbeat(websocket))
    try:
        await _chat_loop(websocket, container, user_id)
    except WebSocketDisconnect:
        logger.info("Chat websocket disconnected")
    finally:
        heartbeat_task.cancel()
```

> **注**：原循环里 `logger.info("Received websocket chat event", ...)` 日志移到 `_run_dispatch` / `_handle_direct` 里（或省略，保持简洁）。

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec aether-dev pytest tests/test_ws_interrupt.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run full test suite to verify no regression**

Run: `docker exec aether-dev pytest tests/ -x -q 2>&1 | tail -5`
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add app/routes/ws_routes.py tests/test_ws_interrupt.py
git commit -m "feat(ws): 循环改 task 式 + interrupt + 通用 mode 路由（全局打断基础）"
```

---

## Task 7: 小爱插件 XiaoAiRouter 直通模式

**Files:**
- Modify: `integrations/xiaoai/manifest.json`（加 inbound_router capability + mode_option ui_contribution）
- Modify: `integrations/xiaoai/plugin.py`（加 XiaoAiRouter 类 + 挂到 plugin.routers）
- Test: `tests/integrations/test_xiaoai_router.py`

**Interfaces:**
- Consumes: `InboundRouter` ABC（Task 2），`HAHttpCaller`（已有，plugin.py:24-52），`EXECUTE_DIRECTIVE_SUFFIX`（已有死代码，plugin.py:68）
- Produces: `XiaoAiRouter.route(text)` 调 `notify.send_message` 到 `execute_text_directive` 实体；`XiaoAiPlugin.routers = [XiaoAiRouter(...)]`

- [ ] **Step 1: Write the failing test**

```python
"""XiaoAiRouter 直通逻辑测试（不 spawn，mock HA caller）。"""

import asyncio
from unittest.mock import AsyncMock

from integrations.xiaoai.plugin import XiaoAiRouter


def test_execute_entity_derivation():
    """从 media_player entity 推导 execute_text_directive notify 实体。"""
    router = XiaoAiRouter(
        ha_caller=None,
        media_player_entity="media_player.xiaomi_cn_2166464483_lx06",
    )
    entity = router._execute_entity()
    assert entity == "notify.xiaomi_cn_2166464483_lx06_execute_text_directive_a_5_5"


def test_route_calls_ha_notify_send_message():
    """route(text) 调 HA notify.send_message 到 execute_text_directive 实体。"""
    ha_caller = AsyncMock()
    router = XiaoAiRouter(
        ha_caller=ha_caller,
        media_player_entity="media_player.xiaomi_cn_2166464483_lx06",
    )

    async def go():
        result = await router.route("播放周杰伦的歌")
        return result

    result = asyncio.new_event_loop().run_until_complete(go())

    assert result["ok"] is True
    assert result["executed"] == "播放周杰伦的歌"
    ha_caller.call_service.assert_called_once_with(
        domain="notify",
        service="send_message",
        data={
            "entity_id": "notify.xiaomi_cn_2166464483_lx06_execute_text_directive_a_5_5",
            "message": "播放周杰伦的歌",
        },
    )


def test_route_returns_ok_even_with_different_text():
    """不同文字都能路由。"""
    ha_caller = AsyncMock()
    router = XiaoAiRouter(
        ha_caller=ha_caller,
        media_player_entity="media_player.xiaomi_cn_123_lx06",
    )

    async def go():
        return await router.route("讲个笑话")

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True
    assert "笑话" in result["executed"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec aether-dev pytest tests/integrations/test_xiaoai_router.py -v`
Expected: FAIL with `ImportError: cannot import name 'XiaoAiRouter'`

- [ ] **Step 3: Add XiaoAiRouter to plugin.py**

在 `integrations/xiaoai/plugin.py` 的 `XiaoAiSink` 类之后、`XiaoAiPlugin` 类之前，加 `XiaoAiRouter` 类：

```python
from app.integration.sdk.router_base import InboundRouter


class XiaoAiRouter(InboundRouter):
    """小爱直通路由：文字原样转小爱原生执行（execute=true 语义）。

    调 notify.send_message 到 execute_text_directive notify 实体，
    小爱原生执行（播放音乐/讲笑话等），不进 LLM。
    """

    EXECUTE_DIRECTIVE_SUFFIX = "execute_text_directive_a_5_5"

    def __init__(self, ha_caller, media_player_entity: str) -> None:
        self._ha = ha_caller
        self._media_player = media_player_entity

    def _execute_entity(self) -> str:
        """从 media_player entity_id 推导 execute_text_directive notify 实体 id。

        media_player.xiaomi_cn_2166464483_lx06
        → notify.xiaomi_cn_2166464483_lx06_execute_text_directive_a_5_5
        """
        dev = self._media_player.split(".", 1)[-1]
        return f"notify.{dev}_{self.EXECUTE_DIRECTIVE_SUFFIX}"

    async def route(self, text: str) -> dict:
        entity = self._execute_entity()
        await self._ha.call_service(
            domain="notify",
            service="send_message",
            data={"entity_id": entity, "message": text},
        )
        return {"ok": True, "executed": text}
```

然后修改 `XiaoAiPlugin.setup`，在 `self.sinks = [...]` 之后加 `self.routers = [...]`：

```python
        self.sinks = [XiaoAiSink(self.ha_caller, entity_id, execute_mode)]
        self.routers = [XiaoAiRouter(self.ha_caller, entity_id)]
```

- [ ] **Step 4: Update manifest.json**

修改 `integrations/xiaoai/manifest.json`，加 `inbound_router` capability 和 `mode_option` ui_contribution：

```json
{
    "id": "xiaoai",
    "name": "小爱音箱",
    "version": "1.0.0",
    "aether_api_version": "1",
    "author": "Aether",
    "description": "小爱 TTS 广播 + 直通模式（Phase 2）",
    "entry": "plugin.py",
    "capabilities": [
        {
            "type": "output_sink",
            "id": "xiaoai_pro",
            "priority": 100,
            "config_schema": {
                "entity_id": {
                    "type": "string",
                    "required": true,
                    "label": "小爱实体ID",
                    "default": "media_player.xiaomi_cn_2166464483_lx06"
                },
                "execute_mode": {
                    "type": "enum",
                    "options": ["speak", "execute"],
                    "default": "speak",
                    "label": "默认模式"
                }
            }
        },
        {
            "type": "inbound_router",
            "id": "xiaoai_direct",
            "priority": 50,
            "config_schema": {}
        }
    ],
    "permissions": [],
    "secrets": ["ha_url", "ha_token"],
    "ui_contributions": [
        {
            "slot": "chat_input_toolbar",
            "type": "toggle_button",
            "props": {
                "icon_on": "🔊",
                "icon_off": "🔇",
                "title_on": "小爱广播已开启（点击关闭）",
                "title_off": "小爱广播已关闭（点击开启）"
            },
            "state_key": "broadcast_enabled",
            "action": "toggle_broadcast"
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
    ],
    "resources": {
        "max_memory_mb": 128,
        "restart_on_crash": true,
        "max_restarts": 3
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker exec aether-dev pytest tests/integrations/test_xiaoai_router.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Verify manifest validates + plugin loads**

Run: `docker exec aether-dev python -c "from app.integration.schema import Manifest; import json; m = Manifest.model_validate(json.load(open('integrations/xiaoai/manifest.json'))); print('OK', m.capabilities, m.ui_contributions)"`
Expected: 打印 OK + 两个 capability + 两个 ui_contribution

- [ ] **Step 7: Commit**

```bash
git add integrations/xiaoai/manifest.json integrations/xiaoai/plugin.py tests/integrations/test_xiaoai_router.py
git commit -m "feat(xiaoai): XiaoAiRouter 直通模式 + manifest inbound_router/mode_option"
```

---

## Task 8: current_mode state + set_mode action（config 持久化 + 路由）

**Files:**
- Modify: `app/integration/config_helper.py`（加 get_current_mode / set_current_mode）
- Modify: `app/routes/integration_routes.py:109-125`（STATE_HANDLERS 加 current_mode，ACTION_HANDLERS 加 set_mode）
- Test: `tests/test_mode_state_routes.py`

**Interfaces:**
- Consumes: `get_config` / `update_config_section`（已有）
- Produces: `get_current_mode() -> str`（默认 "aether"），`set_current_mode(mode: str)`；`STATE_HANDLERS["current_mode"]`，`ACTION_HANDLERS["set_mode"]`

- [ ] **Step 1: Write the failing test**

```python
"""current_mode state + set_mode action 路由测试。"""

import asyncio
from unittest.mock import MagicMock, patch

from app.routes.integration_routes import (
    get_state, invoke_action, STATE_HANDLERS, ACTION_HANDLERS,
)


def _make_container_with_layer(layer):
    container = MagicMock()
    container.integration_layer = layer
    return container


def test_current_mode_in_state_handlers():
    """current_mode 注册在 STATE_HANDLERS。"""
    assert "current_mode" in STATE_HANDLERS


def test_set_mode_in_action_handlers():
    """set_mode 注册在 ACTION_HANDLERS。"""
    assert "set_mode" in ACTION_HANDLERS


def test_get_current_mode_returns_aether_by_default():
    """默认 current_mode = aether。"""
    with patch("app.integration.config_helper.get_config", return_value="aether"):
        layer = MagicMock()
        from app.integration.config_helper import get_current_mode
        # STATE_HANDLERS["current_mode"] 应读 get_current_mode
        handler = STATE_HANDLERS["current_mode"]
        # handler 签名是 lambda layer: value，但它读 config 不读 layer
        # 所以这里直接测 get_current_mode
        assert get_current_mode() == "aether"


def test_set_mode_persists():
    """set_mode 持久化到 config。"""
    with patch("app.integration.config_helper.update_config_section") as mock_update:
        from app.integration.config_helper import set_current_mode
        set_current_mode("xiaoai_direct")
        mock_update.assert_called_once_with(
            "integration", {"current_mode": "xiaoai_direct"})


def test_invoke_action_set_mode():
    """POST /action/set_mode 调用 handler 返回新 mode。"""
    with patch("app.integration.config_helper.set_current_mode") as mock_set, \
         patch("app.integration.config_helper.get_config", return_value="xiaoai_direct"):
        layer = MagicMock()
        container = _make_container_with_layer(layer)

        async def go():
            return await invoke_action("set_mode", container)

        # invoke_action 需要接受 mode 参数——当前签名是 (action, container)
        # set_mode handler 需要读 body 里的 mode。这里测 handler 本身。
        handler = ACTION_HANDLERS["set_mode"]
        assert handler is not None

    # handler 是 async fn(layer, mode) -> {current_mode: mode}
    # 实际路由层会从 request body 取 mode 传进来
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec aether-dev pytest tests/test_mode_state_routes.py -v`
Expected: FAIL（current_mode 不在 STATE_HANDLERS，set_mode 不在 ACTION_HANDLERS）

- [ ] **Step 3: Add config_helper functions**

在 `app/integration/config_helper.py` 末尾加：

```python
def get_current_mode() -> str:
    """读取当前聊天模式（默认 "aether"）。"""
    return str(get_config("integration.current_mode", "aether"))


def set_current_mode(mode: str) -> None:
    """持久化当前聊天模式到 config.json。"""
    update_config_section("integration", {"current_mode": str(mode)})
```

- [ ] **Step 4: Modify integration_routes.py**

修改 `app/routes/integration_routes.py` 的 STATE_HANDLERS 和 ACTION_HANDLERS。

STATE_HANDLERS 加 `current_mode`（约 109-111 行）：

```python
STATE_HANDLERS = {
    "broadcast_enabled": lambda layer: layer.sink_manager.broadcast_enabled,
    "current_mode": lambda layer: _get_current_mode_safe(),
}
```

ACTION_HANDLERS 加 `set_mode`（约 116-125 行）：

```python
async def _toggle_broadcast(layer):
    """切换全局广播开关（框架能力，非小爱专属）。"""
    new_state = not layer.sink_manager.broadcast_enabled
    layer.set_broadcast_enabled(new_state)
    return {"broadcast_enabled": new_state}


async def _set_mode(layer, mode: str = "aether"):
    """设置当前聊天模式（框架能力，非小爱专属）。"""
    from .integration.config_helper import set_current_mode
    set_current_mode(mode)
    return {"current_mode": mode}


ACTION_HANDLERS = {
    "toggle_broadcast": _toggle_broadcast,
    "set_mode": _set_mode,
}
```

加辅助函数（模块级，STATE_HANDLERS 引用）：

```python
def _get_current_mode_safe() -> str:
    """安全读取 current_mode（集成平台未启用时也能读）。"""
    try:
        from ..integration.config_helper import get_current_mode
        return get_current_mode()
    except Exception:
        return "aether"
```

同时修改 `invoke_action` 路由，让它支持从 request body 传 mode 参数（约 140-150 行）：

```python
@router.post("/integrations/action/{action}")
async def invoke_action(action: str, container=Depends(get_container),
                        body: dict | None = None):
    """通用动作触发路由。按 action 路由到框架能力。

    set_mode 等 action 可从 body 传参数（如 {"mode": "xiaoai_direct"}）。
    """
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        return {"success": False, "message": f"未知 action: {action}"}
    # set_mode 需要额外参数
    if action == "set_mode":
        mode = (body or {}).get("mode", "aether")
        result = await handler(layer, mode)
    else:
        result = await handler(layer)
    return {"success": True, "data": result}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker exec aether-dev pytest tests/test_mode_state_routes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/integration/config_helper.py app/routes/integration_routes.py tests/test_mode_state_routes.py
git commit -m "feat(integration): current_mode state + set_mode action（模式持久化）"
```

---

## Task 9: 前端 ModeOptionContribution.vue + IntegrationSlot 注册

**Files:**
- Create: `frontend/src/components/integration/ModeOptionContribution.vue`
- Modify: `frontend/src/components/integration/IntegrationSlot.vue:24-28`
- Test: 手动验证（前端组件，无自动化测试框架配置）

**Interfaces:**
- Consumes: `apiGet` / `apiPost` from `../../utils/api`（相对路径，与 ToggleButtonContribution 一致）
- Produces: mode_option 类型组件，点击 POST `/api/integrations/action/set_mode` `{mode}` + 派发 `mode-changed` 事件

- [ ] **Step 1: Create ModeOptionContribution.vue**

```vue
<template>
  <button
    class="mode-option-btn"
    :class="{ active: isActive }"
    @click="selectMode"
    :title="contribution.props?.label"
  >
    <span v-if="contribution.props?.icon" class="mode-icon">{{ contribution.props.icon }}</span>
    <span class="mode-label">{{ contribution.props?.label }}</span>
  </button>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { apiGet, apiPost } from '../../utils/api'

const props = defineProps({ contribution: Object })
const isActive = ref(false)

async function refreshState() {
  try {
    const resp = await apiGet('/api/integrations/state/current_mode')
    const current = resp?.value || 'aether'
    isActive.value = current === props.contribution.props?.mode
  } catch {
    isActive.value = false
  }
}

async function selectMode() {
  const mode = props.contribution.props?.mode
  try {
    await apiPost('/api/integrations/action/set_mode', { mode })
    isActive.value = true
    window.dispatchEvent(new CustomEvent('mode-changed', { detail: { mode } }))
  } catch {
    // 忽略，保持当前状态
  }
}

onMounted(refreshState)
</script>

<style scoped>
.mode-option-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  background: transparent;
  color: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}
.mode-option-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.9);
}
.mode-option-btn.active {
  background: rgba(88, 166, 255, 0.15);
  border-color: rgba(88, 166, 255, 0.4);
  color: #58a6ff;
}
.mode-icon {
  font-size: 14px;
}
</style>
```

- [ ] **Step 2: Register mode_option in IntegrationSlot.vue**

修改 `frontend/src/components/integration/IntegrationSlot.vue` 的 TYPE_COMPONENTS（约 24-28 行）：

```javascript
import { ref, onMounted, shallowRef } from 'vue'
import { apiGet } from '../../utils/api'
import ToggleButtonContribution from './ToggleButtonContribution.vue'
import ModeOptionContribution from './ModeOptionContribution.vue'

const props = defineProps({
  slot: { type: String, required: true },
})

const contributions = ref([])

// type → 通用组件映射（预定义类型，插件不能贡献任意组件）
const TYPE_COMPONENTS = {
  toggle_button: ToggleButtonContribution,
  mode_option: ModeOptionContribution,
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/integration/ModeOptionContribution.vue frontend/src/components/integration/IntegrationSlot.vue
git commit -m "feat(frontend): ModeOptionContribution 组件 + IntegrationSlot 注册 mode_option"
```

---

## Task 10: ChatView 发送按钮变身 + 模式选择器 + Finish(false) 处理

**Files:**
- Modify: `frontend/src/views/ChatView.vue`
  - 发送按钮变身（约 961 行 `<button @click="sendMessage" class="send-btn">发送</button>`）
  - 框架默认 Aether 按钮 + IntegrationSlot(chat_mode_selector)（input-row 内）
  - chatMode 状态 + mode-changed 监听 + sendMessage 带 mode
  - Finish(false) 处理（约 274 行 Dialog.Finish case）
- Test: 手动验证

**Interfaces:**
- Consumes: `IntegrationSlot`（已有 import），`apiGet`（已有 import 模式）
- Produces: 发送按钮 isStreaming 时变停止按钮；模式选择器（Aether 默认 + 插件贡献）；sendMessage 带 mode 字段

- [ ] **Step 1: Add chatMode state + mode-changed listener**

在 ChatView.vue 的 `<script setup>` 区域（statusPhase 附近，约 116 行后）加：

```javascript
const chatMode = ref('aether')  // 'aether' 或插件声明的 mode 值

// 框架默认模式按钮点击
function selectAetherMode() {
  chatMode.value = 'aether'
  // 同步到后端（让其他标签页/刷新后一致）
  apiPost('/api/integrations/action/set_mode', { mode: 'aether' }).catch(() => {})
}

// 初始化读取 current_mode
onMounted(async () => {
  try {
    const resp = await apiGet('/api/integrations/state/current_mode')
    if (resp?.value) chatMode.value = resp.value
  } catch {}
})

// 监听插件贡献的模式按钮切换
window.addEventListener('mode-changed', (e) => {
  chatMode.value = e.detail.mode
})
```

> **注**：需确认 `apiGet` / `apiPost` 已 import。ChatView 已 import IntegrationSlot，检查 api 工具是否已引入。若未引入，加 `import { apiGet, apiPost } from '../utils/api'`。

- [ ] **Step 2: Modify sendMessage to include mode**

修改 `sendMessage` 函数（约 431-435 行的 ws.send）：

```javascript
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: 'chat',
      query: text,
      session_id: sessionId.value,
      mode: chatMode.value,
    }))
  }
```

- [ ] **Step 3: Add send button transform (变身停止按钮)**

修改 input-row 的发送按钮（约 961 行）：

```html
        <IntegrationSlot slot="chat_input_toolbar" />
        <button
          @click="statusPhase ? handleInterrupt() : sendMessage()"
          :class="['send-btn', { 'stop-btn': statusPhase }]"
        >
          {{ statusPhase ? '■' : '发送' }}
        </button>
```

加打断处理函数（sendMessage 附近）：

```javascript
function handleInterrupt() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'interrupt' }))
  }
  finalizeStreaming()
}
```

> **注**：用 `statusPhase` 判断是否在生成中（thinking/executing/retrying/finalizing/broadcasting 都算）。`statusPhase` 为空字符串时是空闲态 → 发送按钮。broadcasting 阶段（Task 5 emit 的 `UI.Status(phase="broadcasting")`）也会让按钮保持停止态——此时点击打断小爱（无 AI task 可 cancel，WS route 只调 interrupt_all）。

- [ ] **Step 4: Add mode selector (framework Aether button + plugin contributions)**

在 input-row 内，IntegrationSlot(chat_input_toolbar) 之前加模式选择器：

```html
      <div class="input-row">
        <div class="mode-selector">
          <button
            class="mode-option-btn"
            :class="{ active: chatMode === 'aether' }"
            @click="selectAetherMode"
          >Aether</button>
          <IntegrationSlot slot="chat_mode_selector" />
        </div>
        <!-- ... existing toolbar buttons ... -->
```

加对应样式（.input-row 样式附近，约 1474 行后）：

```css
.mode-selector {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.mode-selector .mode-option-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  background: transparent;
  color: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}
.mode-selector .mode-option-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.9);
}
.mode-selector .mode-option-btn.active {
  background: rgba(88, 166, 255, 0.15);
  border-color: rgba(88, 166, 255, 0.4);
  color: #58a6ff;
}
.send-btn.stop-btn {
  background: rgba(255, 86, 86, 0.15);
  border-color: rgba(255, 86, 86, 0.4);
  color: #ff5656;
}
```

- [ ] **Step 5: Handle Dialog.Finish(success=false)**

修改 `handleInstruction` 的 `Dialog.Finish` case（约 274-277 行）。当前已调 `finalizeStreaming()`，确认它清空了 statusPhase。检查是否需要额外处理（被打断不显示错误）：

```javascript
    case 'Dialog.Finish':
      statusPhase.value = ''
      finalizeStreaming()
      break
```

> **注**：现有 Finish 处理已够用（清 statusPhase + finalizeStreaming）。success=false 不显示错误（被打断不是错误）。确认 finalizeStreaming 里没有"显示错误"逻辑。若 message 非空（直通回传"已转交处理"），可显示为助手消息。检查 handleInstruction 里是否有渲染 message 的逻辑——若有则保留，若没有则在 Finish case 加：

```javascript
    case 'Dialog.Finish':
      statusPhase.value = ''
      if (inst.payload?.message) {
        // 直通回传文案 / 其他带消息的 Finish
        finalizeStreaming(inst.payload.message)
      } else {
        finalizeStreaming()
      }
      break
```

> 检查 `finalizeStreaming` 签名——若不接受参数，改为在 Finish case 里直接 push 消息。

- [ ] **Step 6: Verify frontend builds**

Run: `cd frontend && npm run build 2>&1 | tail -10`
Expected: 构建成功无报错

- [ ] **Step 7: Commit**

```bash
git add frontend/src/views/ChatView.vue
git commit -m "feat(frontend): 发送按钮变身停止按钮 + 模式选择器 + Finish(false) 处理"
```

---

## Task 11: E2E 验证 + 解耦验证

**Files:**
- Manual verification + 现有测试全量回归

- [ ] **Step 1: Run full test suite**

Run: `docker exec aether-dev pytest tests/ -v 2>&1 | tail -20`
Expected: 全绿（Phase 1 + Phase 2 所有测试）

- [ ] **Step 2: Rebuild container + verify integration layer starts**

Run: `docker exec aether-dev python -c "import asyncio; from app.container import get_container; c = get_container(); print('integration_layer:', c.integration_layer)"` 
Expected: integration_layer 非 None

- [ ] **Step 3: Verify xiaoai plugin loads with both capabilities**

Run: `docker exec aether-dev python -c "
import asyncio, json
from app.integration.integration_layer import IntegrationLayer
layer = IntegrationLayer(plugin_dir='integrations')
async def go():
    await layer.start()
    plugins = layer.list_plugins()
    print(plugins)
    ui = layer.list_ui_contributions()
    print('ui:', ui)
    await layer.stop()
asyncio.new_event_loop().run_until_complete(go())
"`
Expected: xiaoai 插件 alive=True，capabilities 含 output_sink + inbound_router，ui_contributions 含 toggle_button + mode_option

- [ ] **Step 4: Verify route_inbound works end-to-end (with HA)**

在 ChatView 切"小爱"模式，输入"你好" → 小爱应原生响应。

- [ ] **Step 5: Verify interrupt works end-to-end**

在 ChatView Aether 模式问一个长问题，AI 生成中点击■停止按钮 → AI 立即停 + 小爱停。

- [ ] **Step 6: Verify decoupling — delete xiaoai plugin**

临时移走小爱插件验证零硬编码：

Run:
```bash
docker exec aether-dev mv integrations/xiaoai /tmp/xiaoai_backup
docker exec aether-dev python -c "
import asyncio
from app.integration.integration_layer import IntegrationLayer
layer = IntegrationLayer(plugin_dir='integrations')
async def go():
    await layer.start()
    ui = layer.list_ui_contributions()
    print('ui_contributions:', ui)  # 应为空——无 mode_option
    result = await layer.route_inbound('test', 'xiaoai_direct')
    print('route result:', result)  # 应 no inbound router available
    await layer.stop()
asyncio.new_event_loop().run_until_complete(go())
"
docker exec aether-dev mv /tmp/xiaoai_backup integrations/xiaoai
```
Expected: ui_contributions 为空，route_inbound 返回 `{ok: False, error: "no inbound router available"}`

- [ ] **Step 7: Grep verify zero hardcoding**

Run: `docker exec aether-dev grep -rn "xiaoai\|小爱" app/routes/ app/agents/ app/integration/ --include="*.py" | grep -v "manifest_loader\|config_helper\|test_\|__pycache__"`
Expected: 主程序业务逻辑无 xiaoai/小爱 硬编码（只在 manifest 目录扫描的泛化逻辑里出现）

- [ ] **Step 8: Final commit**

```bash
git add -A
git commit -m "test: Phase 2 E2E 验证 + 解耦验证通过"
```

---

## Self-Review

**1. Spec coverage:**
- §3 WS 循环改造 → Task 6 ✅
- §4.1 Dispatcher CancelledError → Task 5 ✅
- §4.4 发送按钮变身 → Task 10 ✅
- §4.3 interrupt_all（已实现，复用）→ Task 5/6 调用 ✅
- §5.0-5.5 inbound_router 全链路 → Task 1-3, 7 ✅
- §5.4 manifest mode_option → Task 7 ✅
- §6.1 模式选择器 → Task 9-10 ✅
- §6.2 发送按钮变身 → Task 10 ✅
- §6.3 Finish(false) 处理 → Task 10 ✅
- §6.4 直通响应显示 → Task 6（emit Finish with message）+ Task 10（渲染）✅
- §6.5-6.7 ModeOptionContribution + IntegrationSlot + mode-changed → Task 9-10 ✅
- current_mode state/action → Task 8 ✅

**2. Placeholder scan:** 无 TBD/TODO，所有 step 有完整代码 ✅

**3. Type consistency:**
- `METHOD_ROUTE = "router.handle"` — Task 1 定义，Task 2/3 引用 ✅
- `InboundRouter.route(text) -> dict` — Task 2 定义，Task 7 实现 ✅
- `IntegrationPlugin.routers` — Task 2 定义，Task 7 挂载 ✅
- `IntegrationLayer.route_inbound(text, mode) -> dict` — Task 3 定义，Task 6 调用 ✅
- `Dialog.Finish(success, message)` — Task 4 定义，Task 5/6 emit ✅
- `get_current_mode() / set_current_mode(mode)` — Task 8 定义，Task 9/10 引用 ✅
