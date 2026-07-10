# AI 大模型密钥配置指南

这篇讲怎么配置 AI 大模型的"钥匙"（API 密钥）。Aether 把所有密钥统一放在 `/keys` 页管理——LLM 的对话、视觉、嵌入、摘要、还有语音识别（STT），都用同一套机制。

## 核心概念：Key 和角色

Aether 用"密钥 + 角色"两层模型：

- **密钥（Key）**：一组 `base_url + model + type + api_key`，就是某个模型服务的接入凭证。你可以加很多个
- **角色（Role）**：Aether 有 5 个角色——对话、视觉、嵌入、摘要、语音。每个角色指定"用哪个 Key"

这样一个 Key 能被多个角色复用，换模型时只改角色绑定，不用重填密钥。

### 5 个角色分别干什么

| 角色 | type | 干什么 | 必须配吗 |
|------|------|------|------|
| 对话 chat | `chat` | 主聊天、自然语言时间翻译、条件判断 | **必须** |
| 视觉 vision | `vision` | 看摄像头画面、视觉规则评估 | 想用摄像头就得配 |
| 嵌入 embed | `embed` | Emoji 语义搜索、RAG 文档检索、语义图 | 想用这些功能就得配 |
| 摘要 summary | `summary` | 聊天太长时自动压缩成摘要 | 建议配，不配就不能自动压缩 |
| 语音 stt | `stt` | 把你的语音转成文字 | 想用语音输入就得配 |

## 怎么到 `/keys` 页

侧边栏没有直接入口。两种方式：

1. 聊天框输入 `/keys` 斜杠命令
2. 浏览器访问 `http://localhost:5173/keys`

## 添加一个 Key

1. 在 `/keys` 页点 **「+ 添加 Key」**
2. 填这些字段：

| 字段 | 说明 | 示例 |
|------|------|------|
| Base URL | 模型服务地址（不含路径） | `https://open.bigmodel.cn/api/paas/v4` |
| Model | 模型名 | `glm-4-flash` |
| Type | 密钥类型 | `chat` / `vision` / `embed` / `summary` / `stt` |
| API Key | 密钥值（密码框，不显示） | `你的密钥` |

3. 点 **「保存」**

保存后密钥值会写入 `.env` 文件（环境变量名自动生成，如 `LLM_KEY_XXX`），配置元数据存进数据库。`.env` 已被 gitignore，不会上传。

### 各家服务商配置示例

**智谱 GLM（推荐，免费额度够用）**
- 注册：https://open.bigmodel.cn
- Base URL：`https://open.bigmodel.cn/api/paas/v4`
- 模型：`glm-4-flash`（chat/summary）、`glm-4v-flash`（vision）

**SiliconFlow**
- 注册：https://siliconflow.cn
- 嵌入模型：`BAAI/bge-m3`，type 选 `embed`
- 语音模型：`FunAudioLLM/SenseVoiceSmall`，type 选 `stt`

**本地 Ollama**（详见「本地 Ollama 模型部署」）
- Base URL：`http://127.0.0.1:11434/v1`（注意带 `/v1`）
- API Key：空着（本地不要密钥）
- 模型：`qwen3.5:9b` 等

## 给角色绑定 Key

加完 Key 后，去 `/models` 页（聊天框输入 `/models`）给每个角色指定用哪个 Key：

1. 进入 `/models` 页
2. 每个角色一行，右边是个下拉框，列出所有同 type 的 Key
3. 选一个，立即保存生效（不用重启）

> 下拉框只显示和角色 type 匹配的 Key。比如对话角色只看到 type=chat 的 Key。

## STT 语音识别密钥

STT 密钥也走 `/keys` 页统一管理（type 选 `stt`）。配好后：

- 聊天页（`/chat`）和文档助手页（`/doc`）的麦克风按钮就能用了
- 录音上传到 `/api/stt/transcribe`，后端转发给 SiliconFlow SenseVoiceSmall 转文字
- 识别结果**追加到输入框**（不自动发送），你可以再编辑

> STT 默认用 SiliconFlow 的 `FunAudioLLM/SenseVoiceSmall`，支持 webm/wav/mp3。

## 首次设置向导

第一次注册登录后，如果还没配过密钥，会进入设置向导（`/setup`），第二步就是配 LLM：

- 4 个角色 Tab（对话/视觉/嵌入/摘要），每个填 Base URL + API Key + 模型名
- 底部状态行用 ✅/⬜ 显示每个角色是否配齐
- 对话角色必须配齐才能完成

> 向导只配 4 个角色（不含 STT）。STT 之后在 `/keys` 页单独加。

## 多用户隔离

Aether 支持多用户，每个用户的密钥是**独立隔离**的：

- 你在 `/keys` 加的 Key 只属于你
- 切换用户时，会加载那个用户的密钥配置
- 这样家里多人共用一台 Aether，各用各的模型服务，互不影响

## 密钥存哪

| 存储位置 | 内容 |
|------|------|
| `.env` 文件 | 密钥值（敏感，已 gitignore） |
| 数据库 user_settings | 密钥元数据（base_url/model/type/env 名） |
| `config.json` 的 `llm_keys` | 全局默认（启动时用，被用户级覆盖） |

> 密钥值只存 `.env`，绝不进数据库或 config.json，避免泄露。

## 出问题了怎么办

### 添加 Key 后聊天还是报错

**现象**：加了 chat 类型的 Key，但对话报"LLM 未配置"

**解决**：
- 去 `/models` 页确认对话角色绑定了刚加的 Key
- 检查 Key 的 type 是否选对（对话要选 `chat`）
- 看 `logs/app.log` 有没有具体错误

### Key 测试失败

**现象**：保存时报连接失败

**解决**：
- 检查 Base URL 对不对（智谱是 `/api/paas/v4` 结尾）
- 检查 API Key 有没有多余空格
- 确认网络能访问该服务商
- 试用 `/ha` 页的"测试模型"功能（`POST /api/models/test`）

### 切换用户后密钥丢了

这是正常的——每个用户密钥独立。切换回原用户就恢复了。

### 想用本地 Ollama 但不知道怎么配

看「本地 Ollama 模型部署」那篇，本质上就是把 Ollama 当成一个 type=chat 的 Key 加进来，Base URL 填 `http://127.0.0.1:11434/v1`。

搞定了！密钥配好后，Aether 的对话、看图、语音输入就都能用了。接下来可以配天气、远程访问等。
