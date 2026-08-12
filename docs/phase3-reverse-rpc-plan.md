# 实现任务：全局状态 reset 路径 + 反向 RPC（插件平台 Phase 3）

> 项目：`D:\Aether`（FastAPI 后端 + Vue 前端的智能家居 AI 助手）
> 插件平台现状：子进程插件架构已完成 Phase 1/2/4，Phase 3（反向 RPC，插件→宿主）未做。本任务实现 Phase 3 + 顺手补一个全局状态隐患。

## 背景与关键代码事实（已探索确认，无需重新调研）

1. **插件通信现状**：宿主↔插件走 stdio JSON-RPC（`app/integration/rpc_protocol.py`），**只有单向**（宿主→插件）。插件要操作设备只能靠宿主把 HA token 注入子进程环境变量，插件自己 HTTP 直连 HA（`integrations/xiaoai/plugin.py` 的 `HAHttpCaller`）——这是要消灭的"临时方案"。

2. **死锁根因**：`app/integration/sdk/stdio_runtime.py:38-75` 插件主循环严格串行（`read → await plugin.handle → write`）。若 handle 内部反向调宿主并 await 响应，**没人读 stdin → 必然死锁**。必须重构成"后台并发 reader + 反向 future map"。

3. **id 空间约定（零冲突）**：宿主→插件请求用偶数 id（`plugin_process.py:144` 已是偶数，从 2 开始）；插件→宿主反向请求用奇数 id（从 1 开始）。双方各自维护 `_pending` future map，奇偶天然不撞。

4. **注入链**：`IntegrationLayer.__init__`（`integration_layer.py:23-31`）→ 构造 `PluginSupervisor`（`plugin_supervisor.py:22`）→ `_start_with_retries`（`plugin_supervisor.py:47`）构造 `PluginProcess`。host handler 注册表要沿这条链注入。

5. **`initialize_services()`（`app/bootstrap.py:30`）几乎无副作用**：纯对象构造，不连 DB、不起线程、不发请求。所以"挪进 lifespan"性价比低，本次不做。

6. **`ha_client.call_service` 签名**（`app/clients/ha_client.py:76-100`）与 xiaoai 的 `HAHttpCaller.call_service`（`plugin.py:35-53`）**完全一致**：`async def call_service(domain, service, entity_id=None, data=None) -> dict`。迁移时插件 3 个 call 点一行不用改。

7. **`_build_plugin_env`（`main.py`，搜该函数名）只被 xiaoai 一个插件用**（全仓唯一声明 secrets 的插件）。迁移后整个删，不拆。

8. **feishu 是宿主侧集成**（跑在主进程内，不走子进程 RPC），反向 RPC 改动**零影响**。

9. **`dispatcher` 全局**：`main.py:596` 定义为 `None`，lifespan 内（约 `:438`）赋值，shutdown（约 `:585-592`）只调 `close_all_agent_clients()`，**从不置回 None** → 进程内重启时持有僵尸对象。

---

## 第一部分：全局状态 reset 路径（零风险，先做）

改 `app/main.py` lifespan 的 shutdown 段末尾（`Database.close()` 之后、`logger.info("Application shutdown")` 之前）补：

```python
global dispatcher
dispatcher = None
# 同步把 _services 的热替换载体（langgraph_agent、langchain_tools）清回初始态。
```

测试：新增 `tests/test_main_globals_reset.py`，验证 lifespan shutdown 后 `app.main.dispatcher is None`。

---

## 第二部分：反向 RPC Phase 3（5 阶段递进，每阶段独立可验证）

### 阶段 1：协议层 `app/integration/rpc_protocol.py`

新增方向 2 方法常量：

```python
# 方向 2（插件 → Aether 反向调用）
METHOD_HOST_HA_CALL = "ha.call_service"
METHOD_HOST_HA_STATES = "ha.get_states"
METHOD_HOST_HA_DEVICES = "ha.get_devices_grouped"
METHOD_HOST_LLM_CHAT = "llm.chat"
METHOD_HOST_BROADCAST = "sink.broadcast"
```

更新文件头 docstring（"Phase 3 补方向 2" → "方向 2 已实现"），加 id 奇偶约定注释。
风险：极低，纯加常量。

### 阶段 2：宿主侧接收端

新增 `app/integration/host_registry.py`：

```python
class HostMethodRegistry:
    """宿主反向方法注册表：method → (handler, required_permission)。"""
    def register(self, method, handler, required_perm=None): ...
    async def dispatch(self, manifest, method, params) -> dict:
        # 未知方法 → RuntimeError；权限不足(required_perm not in manifest.permissions) → PermissionError；否则 await handler(params)
```

handler 签名：`async def handler(params: dict) -> dict`。

改 `app/integration/plugin_process.py`：

- `__init__`（:54）新增 `host_registry: HostMethodRegistry | None = None`，存 `self._host_registry`。
- `_read_stdout`（:163-177）加分流：
  - `if "method" in msg:` → 反向请求 → `asyncio.create_task(self._handle_reverse(msg))`（不 await，reader 立即回 readline）
  - `else:` → 正向响应，走原 id 配对，补 error 字段处理（`msg.get("error")` 存在则 `fut.set_exception`）
- 新增 `_handle_reverse(msg)`：`await self._host_registry.dispatch(self.manifest, method, params)` → 成功 `build_response(rid, result)` / 失败 `build_error(rid, -32000, ...)` → 写回 stdin（write + await drain）。
- `stop()`（:201-205）确认清理逻辑覆盖反向在途状态。
- stdin 并发安全：asyncio 单线程 + 同步 write 无 await 点，天然原子，无需锁。

改 `app/integration/plugin_supervisor.py`：

- `__init__`（:22）新增 `host_registry` 参数，存 `self._host_registry`。
- `_start_with_retries`（:47）构造 `PluginProcess` 时传 `host_registry=self._host_registry`。

### 阶段 3：插件侧并发 runtime 重构（死锁关键，最大改动）

重构 `app/integration/sdk/stdio_runtime.py`：

拆掉串行 while 循环，改成：

- **后台 reader task**：常驻 `loop.run_in_executor(None, sys.stdin.buffer.readline)` 读 stdin（必须保留 `run_in_executor`，Windows Proactor 对匿名管道的 async 读不生效，见 :33-35 注释）。
- 收正向请求（有 method + 偶数 id）→ `asyncio.create_task(_handle_request(msg))` 并发处理
- 收反向响应（有 result/error + id ∈ `_pending_reverse`）→ resolve future（error 时 `set_exception`）
- **writer task**：从 `asyncio.Queue` 串行写 stdout（避免并发写交错），所有输出（正向响应 + 反向请求）入队。
- `_handle_request(msg)`：`await plugin.handle(method, params)` → 入队响应。
- 新增 `_pending_reverse: dict[int, asyncio.Future]` 和 `host_call(method, params)`：分配奇数 id → 注册 future → 入队 request → `await asyncio.wait_for(future, timeout)`。

改 `app/integration/sdk/plugin_base.py`：

- `IntegrationPlugin` 新增 `self.host: HostProxy`（runtime 在 setup 后注入）。
- 新增 `HostProxy` 类：持有 `host_call` 引用，提供 `host.ha.call_service(...)`、`host.ha.get_states()`、`host.ha.get_devices_grouped()`、`host.llm.chat(...)`、`host.broadcast(...)`，方法名映射到阶段 1 的 METHOD 常量。用子代理对象（`host.ha`、`host.llm`）组织。

风险：高。缓解：阶段 3 完成后必须先跑"嵌套调用不死锁"专项测试（插件 handle 内反向调 host，验证正常返回）。

### 阶段 4：暴露宿主能力 + 接线

改 `app/integration/integration_layer.py`：

- `__init__`（:23-31）新增 `host_deps: dict | None = None`（含 ha_client, ha_service, llm_chat_client；sink_manager 已是内部属性）。
- 构造 `HostMethodRegistry` 并注册 5 个 handler：

| method | handler | required_perm |
|---|---|---|
| `ha.call_service` | `ha_client.call_service(domain, service, entity_id, data)` | `"ha"` |
| `ha.get_states` | `ha_service.get_states_snapshot()` | `"ha"` |
| `ha.get_devices_grouped` | `ha_service.get_all_devices_grouped()` | `"ha"` |
| `llm.chat` | `llm_chat_client.chat(messages, timeout)` | `"llm"` |
| `sink.broadcast` | `self.sink_manager.broadcast(text, msg_id)` | `"broadcast"` |

- 透传 `host_registry` 给 `PluginSupervisor`。

改 `app/main.py` lifespan 构造 `IntegrationLayer` 处（搜 `IntegrationLayer(`），加 `host_deps={"ha_client": ha_client, "ha_service": ha_service, "llm_chat_client": llm_chat_client}`。

### 阶段 5：xiaoai 迁移 + 清理

改 `integrations/xiaoai/plugin.py`：

- 删 `HAHttpCaller` 类（:25-53）。
- `setup`（:165-166）env 读取两行删掉，改为 `self.ha_caller = self.host.ha`。
- 3 个 call 点（:94-98、:110-115、:143-147）的 `self._ha.call_service(...)` 一行不改（HostProxy 签名一致）。

改 `integrations/xiaoai/manifest.json`：

- `"permissions": ["ha"]`（原 `[]`）。
- `"secrets": []`（原 `["ha_url", "ha_token"]`）。

改 `app/main.py`：

- 删 `_build_plugin_env` 函数（搜该名）。
- 删构造 `IntegrationLayer` 时的 `env_per_plugin=_build_plugin_env(...)`。
- 更新相关注释。

改 `app/integration/schema.py`：确认 `Manifest.permissions` 字段存在，文档列出支持的权限值（ha/llm/broadcast）。

---

## 测试计划

新增：

- `tests/test_host_registry.py`：注册 / 未知方法拒绝 / 权限拒绝（manifest 无 `"ha"` 调 `ha.call_service` 抛 `PermissionError`）/ 正常 dispatch。
- `tests/test_reverse_rpc_host.py`：`_read_stdout` 分流——模拟插件发反向 request，验证宿主 dispatch + 写回 response；error 传播；reader 不阻塞。
- `tests/test_reverse_rpc_plugin_runtime.py`（死锁专项）：插件 handle 内反向调 host 不死锁；并发正向请求；反向超时；Windows `run_in_executor` 兼容。
- 扩展 `tests/integrations/echo/` 加反向 RPC 能力，e2e 测插件→宿主→mock ha_client 全链路。

回归：

- `tests/integrations/test_xiaoai_plugin.py`：AsyncMock 桩换成 in-process HostProxy 桩，3 个 call 点行为不变。
- `tests/test_integration_e2e.py`：现有 broadcast/interrupt/crash 用例验证不受影响。

---

## 执行顺序与提交建议

1. 第一部分（reset）→ 独立 commit
2. 阶段 1+2（协议常量 + 宿主接收 + registry）→ 一个 commit
3. 阶段 3（插件 runtime + HostProxy）→ 单独 commit（死锁关键，跑死锁专项测试）
4. 阶段 4+5（接线 + xiaoai 迁移 + 清理）→ 一个 commit，跑全套回归

每阶段配测试，阶段 3 是最高风险点。
