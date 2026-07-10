# 外部 MCP Server 集成

Aether 管家本身已经能干不少事，但如果你想让它在对话里调更多的外部工具——读写文件、查数据库、连别的智能服务——可以通过 **MCP（Model Context Protocol）** 接入。

## 一、这是什么

MCP 是一种「通用插头」标准。就像 USB 能连各种设备一样，MCP 能让管家接入各种外部工具。配好之后，管家会自动发现这些工具，调用方式和内置工具一模一样，你在对话里感觉不到差别。

Aether 的 MCP 分两类：

- **本地内置工具**：开箱即用，不用配。
- **外部 MCP Server**：你自己在 `config.json` 里声明，管家启动时连上。

## 二、管家自带的能力（不用配）

Aether 内置 12 个工具，开箱即用：

| 工具 | 作用 |
|------|------|
| `describe_state` | 查询摄像头画面状态和最近一次工具调用结果 |
| `fetch_webpage` | 抓取网页正文（markdown），SSRF 防护 |
| `http_request` | 发送 HTTP 请求调用外部 API |
| `web_search` | 上网搜索（走 Exa） |
| `vision_chat` | 看摄像头画面回答问题 |
| `get_entities` | 读取家里所有智能设备 |
| `call_service` | 控制设备（开关灯、调空调等） |
| `verify_condition` | 验证时间/天气/画面/设备条件是否成立 |
| `verify_action` | 验证设备是否真的执行了指令 |
| `scheduled_task_create` | 创建定时任务 |
| `scheduled_task_list` | 列出定时任务 |
| `scheduled_task_delete` | 删除定时任务 |

> 这些是 AI 管家在对话里能调的。前端聊天界面会为常用的 8 个显示工具卡片（见《AI管家聊天入门》）。

## 三、怎么接入外部工具

在 `config.json` 的 `external_mcp` 数组里声明：

```json
{
  "external_mcp": [
    {
      "name": "filesystem",
      "cmd": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\你的用户名\\Documents"]
    },
    {
      "name": "sqlite",
      "cmd": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "/path/to/database.db"]
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `name` | 给这个工具起个名字，比如 `filesystem` |
| `cmd` | 启动命令，必须是系统里能跑的（`npx`、`uvx`、`python` 等） |
| `args` | 命令参数数组（可选） |

配好后重启 Aether，管家启动时会自动连接、发现工具。

## 四、运行时管理：连接/断开

除了改配置重启，Aether 还提供运行时管理接口（`/api/mcp/servers`），可以在不重启的情况下连接或断开外部 server：

| 接口 | 方法 | 作用 |
|------|------|------|
| `/api/mcp/servers` | GET | 列出已连接的外部 server 和全部注册工具 |
| `/api/mcp/servers` | POST | 运行时连接一个外部 server |
| `/api/mcp/servers/{name}` | DELETE | 断开指定 server |

### 安全限制（重要）

**运行时连接接口只能连 `external_mcp` 里预声明的 server，不能连任意命令。**

这是为了防止通过 API 执行任意命令（RCE）。白名单由 `(name, cmd)` 共同唯一标识——你必须先在 `config.json` 里声明，运行时接口才能连。想接新的 server，编辑 `config.json` 加一条声明，再调接口连接即可。

连接超时 60 秒（有些工具第一次启动要下载依赖，会慢一点）。连接/断开后会自动重建 Agent，让管家立刻用上（或停用）新工具。

## 五、动手试试：接入文件系统工具

**第一步**：装好 Node.js（18+）。

**第二步**：在 `config.json` 加声明：

```json
{
  "external_mcp": [
    {
      "name": "filesystem",
      "cmd": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\你的用户名\\Documents"]
    }
  ]
}
```

**第三步**：重启 Aether（或调 `POST /api/mcp/servers` 连接）。

**第四步**：跟管家说「帮我看看 Documents 文件夹里有什么文件」，它会自动调文件系统工具去查。

> **安全提醒**：接文件工具时，限制它只能访问你允许的目录（在 `args` 里指定路径），别暴露敏感文件夹。

## 六、后台做了什么

1. **启动连接**：Aether 启动时读 `external_mcp`，逐个用 `cmd + args` 拉起子进程，走 stdio JSON-RPC 握手。
2. **发现工具**：握手后问对方「你有哪些工具」，把工具名和描述注册进来。
3. **融合调用**：管家对话时，外部工具和内置工具一起进 LLM 的工具列表，按需调用。
4. **优雅关闭**：Aether 退出时给子进程发关闭信号，不留残留进程。

## 七、怎么确认接上了

- 调 `GET /api/mcp/servers`，看 `servers` 里有没有、`tools` 里多了哪些。
- 启动日志里看连接记录（失败会有报错）。
- 直接让管家做一件需要那个工具的事，看能不能搞定。

## 八、排查

### 连接失败

- 命令行手动跑 `cmd`（比如 `npx --version`），确认能执行。
- 检查 `args` 写对没。
- 第一次启动可能要下载依赖，给足 60 秒。
- 运行时连接被拒（403 `mcp_not_whitelisted`）→ 没在 `external_mcp` 预声明，先加配置。

### 工具不出现 / 管家不会用

- 连接成功但工具没生效 → 重启或确认已自动重建 Agent。
- 把日志级别调到 DEBUG 看详细输出。
- 工具的描述写得太模糊，管家理解不了它干什么用。

### 工具崩了

- 看日志报错。
- 调 `DELETE /api/mcp/servers/{name}` 断开再 `POST` 重连。
- 实在不行重启 Aether。
