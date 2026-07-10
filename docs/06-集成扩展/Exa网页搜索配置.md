# 让管家学会上网搜东西——Exa 网页搜索

你问管家一个问题，除了它自己脑子里的知识，还能真的上网去搜。Aether 用 **Exa** 提供网页搜索能力，管家可以随时帮你查最新消息、找资料、看新闻。

## Exa 是什么？为什么用它？

Exa 是一个为 AI 设计的搜索 API。跟传统搜索引擎不一样的地方是：它返回的是**结构化的、给模型读的结果**，而不是一堆网页链接让人去看。AI 管家拿到结果就能直接理解、总结给你。

Aether 接 Exa 的方式有点特别：走的是 **MCP（Model Context Protocol）协议**，调的是 Exa 官方的 MCP 端点 `https://mcp.exa.ai/mcp`，工具名 `web_search_exa`。这样做的好处是和 Aether 的工具体系统一，管家调用 `web_search` 工具时，底层自动转发给 Exa。

## 怎么配置

搜索配置在 **`/advanced`（高级设置）** 页面的「网页搜索（Exa）」一栏，对应 `config.json` 里的：

```json
"web_search": {
  "exa": {
    "api_key": ""
  }
}
```

只有一个配置项：`api_key`。

| 值 | 效果 |
|----|------|
| 留空 `""` | **匿名调用** Exa MCP，能直接用，但有速率限制 |
| 填入你的 Exa API Key | 享每月 2 万次免费额度，不限速 |

> Exa Key **不在** `/keys`（LLM 密钥）页面管理——它和 LLM Key 是两套体系。LLM Key 有 5 种角色（chat/vision/summary/embed/stt），搜索是独立的，配在高级设置里。

### 去哪申请 Exa Key

到 Exa 官网注册账号即可拿到 API Key，每月有 2 万次免费额度。不想申请也完全没问题，留空匿名调用就能用，只是有速率限制。

## 管家是怎么上网搜的？

```
你问问题 → 管家觉得需要上网查 → 调用 web_search 工具
    → 后端转发到 Exa MCP 端点
    → Exa 返回搜索结果（标题 + 摘要 + 链接）
    → 管家总结一下 → 回复你
```

几个细节：

- **默认搜 5 条**，每次结果包含标题、摘要和链接。
- 你可以让管家多搜点：「帮我搜 10 条关于 XXX 的信息」，管家会把 `max_results` 调到 10（上限 10 条）。
- 搜索参数 `type=auto`、`livecrawl=fallback` 是写死的，Exa 会自动决定是实时抓取还是用缓存。

## 搜到结果后还能深读：fetch_webpage

搜出来的结果往往只有摘要。如果你想让管家读某个网页的完整内容，它会用另一个工具 `fetch_webpage`：

- 这个工具**不用 Exa**，是 Aether 自己实现的网页抓取器。
- 直接用 httpx 请求网址，把 HTML 转成 markdown 给管家读。
- 带 SSRF 防护（不会去访问内网地址）、5MB 大小限制、默认返回 4000 字。
- 遇到 Cloudflare 拦截会自动换 UA 重试一次。

所以完整流程常常是：`web_search` 找到相关链接 → `fetch_webpage` 读某篇详情 → 管家综合回答。

## 关于隐私

- 匿名调用时，Exa 不知道你是谁，但有速率限制。
- 填了 Key 则走你的 Exa 账号额度，搜索请求由 Exa 处理。
- Aether 本地不记录你的搜索历史（对话历史按会话保存，但搜索行为本身不单独存档）。

> 早期的 Aether 用过本地 SearXNG 容器做搜索，现在已经完全移除，`docker-compose.yml` 里不再有 searxng 服务。如果你看到老文档提到 SearXNG，以这篇为准。

## 排查

### 搜不了 / 提示搜索失败

- 匿名调用被限速了 → 等一会儿，或填入自己的 Exa Key。
- 网络不通 → Exa 是云端服务，需要能访问 `https://mcp.exa.ai`。
- 看后端日志有没有 `web_search` 相关报错。

### 搜出来的结果太少 / 太旧

- 让管家调大 `max_results`（最多 10）。
- Exa 的 `livecrawl=fallback` 会尽量给实时内容，但部分站点可能只有缓存。

### fetch_webpage 读不到内容

- 有些站点反爬严格（频繁返回 403），可能抓不到。
- 非网页内容（图片、PDF 二进制）会被拒绝，只读文本类。
- 内网地址（`192.168.*`、`localhost` 等）被 SSRF 防护拦截，这是安全设计。
