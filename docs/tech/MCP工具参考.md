# MCP 工具参考

Aether 用 MCP (Model Context Protocol) 统一管理所有 AI 工具。LLM 在 ReAct 循环里调用的工具，全部走 MCP 层。

---

## 1. 工具注册系统

### MCPClientManager (`app/mcp/mcp_client_manager.py`)

```python
class MCPClientManager:
    self._tools: dict[str, MCPTool] = {}       # 全名 "{client_id}___{tool_name}" → MCPTool
    self._external_servers: list[ExternalMCPServer] = []

    def register_tool(self, tool: MCPTool) -> None
    def list_tools(self) -> list[MCPTool]
    def get_tool(self, name: str) -> MCPTool | None
    async def connect_external_server(self, name, cmd, args) -> list[MCPTool]
    async def disconnect_server(self, name) -> bool
    def list_external_servers(self) -> list[dict]
```

### MCPTool 数据类

```python
@dataclass
class MCPTool:
    client_id: str                              # 工具所属客户端
    tool_name: str                              # 工具名
    description: str                            # 供 LLM 理解的描述
    parameters: dict                            # JSON Schema 参数定义
    handler: Callable[[dict, session], Awaitable[dict]]
```

### 命名规范

完整工具名：`{client_id}___{tool_name}`（三下划线）。

| client_id | 用途 |
|-----------|------|
| `local` | 内置本地工具（describe_state / fetch_webpage / http_request / web_search / vision_chat / verify_condition / verify_action / scheduled_task_*） |
| `ha_devices` | Home Assistant 设备工具（get_entities / get_device_manual / call_service） |
| 自定义 | 外部 MCP server 的 `name` |

### ToolExecutor (`app/mcp/tool_executor.py`)

- `execute_tool_by_name(tool_name, parameters, session)`：取工具 → 轻量 JSON Schema 校验（required + type + enum）→ 调 handler → 包装成 `{success, tool_name, result}` 或 `{success:False, error}`。
- `resolve_tool_name(tool_name)`：接受短名或全名。先查全名命中则直接返回；短名则遍历 `list_tools()` 找 `client_id___短名`。调用方传哪种都行。

---

## 2. 内置工具清单（13 个）

### 基础本地工具（4 个，`register_local_tools` 注册）

| # | 工具 | 参数 | 作用 |
|---|------|------|------|
| 1 | `describe_state` | 无 | 查询摄像头画面状态和最近一次工具调用结果 |
| 2 | `fetch_webpage` | `url`(必填), `max_chars`, `format` | 抓网页正文，SSRF 防护（拦截内网 IP），HTML→markdown，5MB 上限，默认 4000 字 |
| 3 | `http_request` | `url`(必填), `method`, `headers`, `params`, `json_body` | 通用 HTTP 请求调外部 API |
| 4 | `web_search` | `query`(必填), `max_results` | 上网搜索，走 Exa MCP（`https://mcp.exa.ai/mcp`，工具名 `web_search_exa`）。默认 5 条，上限 10 |

> `web_search` 不再走 SearXNG。Exa Key 在 `config.json` 的 `web_search.exa.api_key`，留空匿名调用（限速）。详见配置参考。

### 依赖注入工具（9 个，`register_all_tools(deps)` 在 `app/tools.py` 注册）

| # | 工具 | client_id | 参数 | 作用 |
|---|------|-----------|------|------|
| 5 | `vision_chat` | `local` | `question`, `camera_id`(可选) | 拍指定摄像头当前帧，用 VL 模型回答问题。返回 `{answer, question, has_frame, model}`。camera_id 不传取当前 AI 预览路 |
| 6 | `get_entities` | `ha_devices` | 无 | 拉所有 HA 设备。返回 `{devices, entities, count, services}`：`devices` 按物理设备聚合（name/area/entity_ids，供介绍设备）；`entities` 为扁平实体列表（含 `_controls` 动态控件、`note` 用户备注，供 call_service） |
| 7 | `get_device_manual` | `ha_devices` | `entity_ids`(必填，逗号分隔) | 按需拉单台/多台设备的详细操作手册（domain/service/param 明细 + 用户自定义备注）。控制不熟悉或有怪癖的设备前主动调用 |
| 8 | `call_service` | `ha_devices` | `domain`(必填), `service`(必填), `entity_id`(必填), `data` | 调 HA 服务控制设备。校验 `entity_id` 真实存在（防 LLM 编造）+ query 语义匹配校验，返回 `{success, result, new_state}` |
| 9 | `verify_condition` | `local` | `condition`(必填), `condition_type`(auto/time/weather/vision/device) | 验证条件是否成立。`auto` 按关键词路由。返回 `condition_met:null` + 数据，由 LLM 判断 |
| 10 | `verify_action` | `local` | `entity_id`(必填), `service`, `data`, `expected_state`, `action_description` | 只读验证设备状态是否真的变了，不执行控制 |
| 11 | `scheduled_task_create` | `local` | `name`(必填), `schedule{kind,at/every_seconds/expr}`, `payload{kind,tool_name+tool_input / message / reminder}` | 创建定时任务，返回 `{success, task_id, summary}` |
| 12 | `scheduled_task_list` | `local` | 无 | 列出所有定时任务 |
| 13 | `scheduled_task_delete` | `local` | `task_id`(必填) | 删除定时任务 |

### verify_condition 路由（`condition_type=auto`）

```
"时间/几点/白天/晚上/早上/下午/hour/time/钟" → time
"天气/下雨/温度/晴/weather"                   → weather
"画面/看到/摄像头/有人/检测/camera"            → vision
"设备/实体/entity/device/状态"                → device
其他                                          → time
```

> `condition_met` 始终为 `null`，由 LLM 根据返回数据自己判断。这样让 LLM 在 ReAct 循环里灵活决策。

### call_service 容错

- `data` 是 JSON 字符串时自动解析
- `entity_id` 不含 `domain.` 前缀时自动补全；支持逗号分隔的批量 entity_id
- 入参 trim 空格（兼容本地模型在参数首尾带空格）
- **entity_id 真实性校验**：不在 HA 实体列表里直接拒绝（防 LLM 编造）；HA 对不存在的 entity_id 静默返回 200，不校验会被当成「成功」谎报
- **query 语义校验**：命中用户指令的设备但目标 entity_id 不在命中范围 → 拒绝（防语义近邻顶替，如「打开加湿器」却操作带除湿模式的空调）
- **控件范围探测保底**（`control_probe.py`）：带单个数值参数的滑块调用收到 400（越界）时，自动按候选刻度（0-1 → 0-100 → 0-255）探测真实范围，命中后缓存并按正确范围归一化用户原值重发。规范表 `entity_controls._DOMAIN_SPEC_RANGES` 已覆盖 HA 规范固定范围的属性（如 media_player.volume_level 恒 0-1），探测仅作保底
- 执行后自动查设备最新状态填 `new_state`

### 设备备注注入

用户在 `/halist` 写的**实体备注**（scope `entity_note`，如「继电器 ON=关门」）经三处同时注入 AI 认知：

1. 后台目录缓存 `_refresh_ha_catalog`（每 60 秒刷新周期重读，改动最多 60 秒生效）
2. 规则生成 `rule_service`（构建自动化时作为最高优先级提示）
3. `get_entities` 返回的 `entities[].note` 字段 + `get_device_manual` 的 `manuals` 文本

备注为空时不输出任何内容（零回归）。详见《03-设备控制/设备面板操作指南》备注一节。

---

## 3. LangChain 工具转换 (`app/mcp/langchain_tools.py`)

`convert_all_tools(manager, full_name=False)` 把所有 MCPTool 转成 LangChain `StructuredTool`：

- **默认用短名**（`full_name=False`），所以 LLM 看到的工具名是 `call_service` 而不是 `ha_devices___call_service`。
- JSON Schema → Pydantic `args_schema`（string→str, integer→int, number→float, boolean→bool, object→dict, array→list；非 required 字段为 `Optional`）。
- 包装 handler：从 `RunnableConfig["configurable"]["session"]` 取 session，调原 handler，结果 `json.dumps` 成字符串（LangChain ToolMessage 要求）。

---

## 4. 外部 MCP 支持

### 协议

stdio + JSON-RPC 2.0：

```
Aether Backend ──stdin/stdout──→ External MCP Process (npx / uvx / python -m ...)
```

### 连接流程

1. 启动子进程（`cmd + args`）
2. JSON-RPC `initialize` 握手
3. `tools/list` 拿工具清单
4. 每个工具包成 `MCPTool(client_id=name, ...)` 注册
5. 调用时 `tools/call` 走 stdio

### 运行时管理（`/api/mcp/servers`）

| 接口 | 方法 | 作用 |
|------|------|------|
| `/api/mcp/servers` | GET | 列已连外部 server + 全部注册工具 |
| `/api/mcp/servers` | POST | 运行时连接（60s 超时） |
| `/api/mcp/servers/{name}` | DELETE | 断开 |

**白名单防 RCE**：运行时连接只允许 `external_mcp` 里预声明的 `(name, cmd)`，不能连任意命令。连/断后自动 `_rebuild_agent()` 重建 Agent。

### 配置

```json
{
  "external_mcp": [
    { "name": "filesystem", "cmd": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"] }
  ]
}
```

> 同名 server 重复连接会跳过。启动超时 60s，失败不阻塞主进程。

---

## 5. 工具注册时序

```
register_all_tools(deps)  (app/tools.py, lifespan 里调)
  ├─ register_local_tools(manager)          ← 4 个基础工具
  │    describe_state / fetch_webpage / http_request / web_search
  │
  └─ 9 个依赖注入工具（工厂创建 handler）：
       vision_chat / get_entities / get_device_manual / call_service
       verify_condition / verify_action
       scheduled_task_create / scheduled_task_list / scheduled_task_delete

convert_all_tools(manager, full_name=False)  ← 全转 LangChain StructuredTool（短名）
build_chat_agent(tools)                       ← LangGraph ReAct Agent，bind_tools
_connect_external_mcp_servers()               ← 后台连外部 server
```

> `current_time` / `get_weather` 是内部辅助函数（被 `verify_condition` 调用），不注册为独立工具。
