# LLM 模型角色配置

Aether 不只用一个模型，而是按**角色**分配不同模型——聊天用一个、视觉用一个、摘要用一个、嵌入用一个、语音转写用一个。这样你可以让贵的模型干精细活，便宜的模型干粗活，省钱又好用。这篇告诉你每个角色干啥、怎么配。

---

## 1. 五个角色都干啥

| 角色 | 标签 | 干什么 | 在哪用 |
|------|------|--------|--------|
| `chat` | 对话 | 主聊天、意图校验、自动化规则判断 | 聊天页、Validator、自动化 |
| `vision` | 视觉 | 看摄像头画面回答问题（VL 多模态推理） | `vision_chat` 工具、摄像头运动推理 |
| `summary` | 摘要 | 把长对话压成摘要 | `/compress`、会话自动压缩 |
| `embed` | 嵌入 | 文本向量化，给 RAG 文档检索用 | 文档助手、语义图 |
| `stt` | 语音 | 语音转文字 | 聊天页 🎤 语音输入 |

> 注意：`chat` 角色最忙，主聊天、校验、自动化都用它，建议配一个能力强又不太贵的模型。`vision` 必须是多模态模型（能看图）。`stt` 走的是 SiliconFlow 的 SenseVoice，密钥类型固定。

---

## 2. 先去配密钥

模型配置的前提是**已经有对应类型的密钥**。密钥在 `/keys` 页管理（看[LLM API 密钥配置指南](../01-安装部署/LLM-API密钥配置指南.md)）。

每个密钥有 5 种类型之一：`chat` / `vision` / `summary` / `embed` / `stt`。比如你加了两个 `chat` 类型的密钥（一个 GLM、一个 DeepSeek），那 `chat` 角色就能在这两个里挑。

---

## 3. 配模型角色

1. 在聊天框打 `/models`，或侧边栏进「模型配置」页。
2. 看到 5 行设置，每行一个角色：对话 / 视觉 / 摘要 / 嵌入 / 语音。
3. 每行右边是个下拉框，列出**该角色类型下的所有密钥**，格式是 `模型名 (base_url)`。
4. 选一个，**自动保存**——不用点保存按钮，选完就生效。

```
对话        chat 角色使用的模型            [glm-4-flash (https://open.bigmodel.cn/api/paas/v4) ▼]
视觉        vision 角色使用的模型          [glm-4v-flash (https://open.bigmodel.cn/api/paas/v4) ▼]
摘要        summary 角色使用的模型         [glm-4-flash (https://open.bigmodel.cn/api/paas/v4) ▼]
嵌入        embed 角色使用的模型           [embedding-3 (https://open.bigmodel.cn/api/paas/v4) ▼]
语音        stt 角色使用的模型             [SenseVoiceSmall (https://api.siliconflow.cn/v1) ▼]
```

如果某个角色下拉显示「-- 未选择 --」，说明还没给它分配密钥——那个功能就用不了。

---

## 4. 调用的是哪个接口

前端配角色调的是这两个接口：

- `GET /api/llm/settings` — 拿当前每个角色分配的 `key_id`，以及配置警告。
- `POST /api/llm/settings` — 提交 `{ role, key_id }`，给某个角色指定密钥。改完会同步到当前用户的 `user_settings.providers`。

后端的 `llm_settings_service` 维护 `providers` 这个「角色 → key_id」映射表。各服务运行时通过 `resolve_key_for_role("chat")` 这类调用，按角色查到对应密钥，再用它的 `base_url` / `model` / `api_key_env` 发请求。

---

## 5. 多用户隔离

密钥和角色分配都是**按用户存**的：

- 每个用户在 `user_settings` 里有自己的 `llm_keys`（密钥列表）和 `providers`（角色→key 映射）。
- A 用户配的模型，B 用户看不到、也用不了。
- 注册新用户时，自动初始化空的 `llm_keys` 和 `providers`。
- 管理员在 `/keys` 页加的密钥会同步进当前用户的存储。

所以一家人共用一个 Aether 实例，每个人可以配自己的模型和密钥，互不干扰。

---

## 6. 配置建议

### 经济型（省钱）

| 角色 | 推荐 |
|------|------|
| chat | glm-4-flash / deepseek-chat（便宜能打） |
| vision | glm-4v-flash（免费额度） |
| summary | glm-4-flash（摘要不需要大模型） |
| embed | embedding-3（便宜） |
| stt | SenseVoiceSmall（SiliconFlow，便宜） |

### 能打型（体验好）

| 角色 | 推荐 |
|------|------|
| chat | glm-4-plus / deepseek-v3 / qwen-max |
| vision | glm-4v-plus / qwen-vl-max |
| summary | glm-4-flash（摘要够用就行） |
| embed | bge-m3 / embedding-3 |
| stt | SenseVoiceSmall |

### 本地 Ollama

所有角色都能指向本地 Ollama（`base_url` 填 `http://localhost:11434/v1`）：

- chat → qwen2.5:7b 之类
- vision → minicpm-v 或 llava
- summary → 小一点的模型就行
- embed → nomic-embed-text / bge-m3
- stt → 这个没法本地，得用 SiliconFlow（Ollama 不支持 SenseVoice）

本地模型配置看[本地 Ollama 模型部署](../01-安装部署/本地Ollama模型部署.md)。

---

## 7. 没配会怎样

- **chat 没配**：聊天页发消息没反应，setup 向导也会提示你先配 LLM。
- **vision 没配**：`vision_chat` 工具调不动，问摄像头相关问题会失败。
- **summary 没配**：`/compress` 报错，自动压缩也不工作。
- **embed 没配**：文档助手（`/doc`）建不了索引，语义图（`/sg`）也构建不了。
- **stt 没配**：🎤 语音输入转写失败。

`GET /api/llm/settings` 返回的 `warnings` 字段会列出这些缺配警告，前端也会相应提示。

---

## 8. 切换模型要重启吗

不用。`/models` 页选完即生效，下一次请求就用新模型了。各服务每次调用都现查 `providers` 映射，不缓存客户端实例（vision/summary/embed 客户端在启动时建好，但密钥池会随 `/keys` 改动 reload）。

> 唯一例外：如果你换了 `vision` 模型，正在跑的摄像头推理可能要等下一帧才用上新模型——不影响聊天。
