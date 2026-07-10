# 本地 Ollama 模型部署

这篇讲怎么在你自己电脑上跑 AI 大模型。Aether 支持用 Ollama 在本地跑模型，对话全在本地处理，不联网、不泄隐私。

> **核心一句话**：Ollama 在 Aether 里就是一个普通的 Key。在 `/keys` 页加一个 type 为 `chat`/`vision`/`embed` 的 Key，Base URL 填 `http://127.0.0.1:11434/v1`，然后在 `/models` 页选中它。流程和配云端 API 完全一样。

## 什么时候用本地模型

| 你的情况 | 建议 |
|------|------|
| 不想把聊天内容传到网上 | 本地模型 |
| 网络不稳定或没网 | 本地模型 |
| 要反应快、记性好的对话 | 云端 API |
| 要识别图像（看摄像头） | 云端优先（本地也行） |
| 想 24 小时一直跑 | 云端为主 + 本地当备用 |

Aether 可以**同时配云端和本地模型**，随时在 `/models` 页切换，不用重启。

## 安装 Ollama

### Windows

1. 打开 https://ollama.com/download/windows
2. 下载 `.exe` 安装包
3. 双击安装，一路 Next
4. 装好后 Ollama 在后台自动运行（任务栏有图标）

检查装好了没：

```powershell
ollama --version
```

### 确认它在跑

```powershell
netstat -ano | findstr ":11434"
```

看到 11434 端口在监听就对了。

## 下载模型

### 对话和看图模型（推荐 qwen3.5:9b）

```powershell
ollama pull qwen3.5:9b
```

这个模型既能聊天也能看图，下载约 5-6 GB，需要几分钟。

### 嵌入模型（推荐 qwen3-embedding:0.6b）

```powershell
ollama pull qwen3-embedding:0.6b
```

用来做 Emoji 语义搜索和 RAG 文档检索。文件很小，约 400 MB。

### 看看都下了哪些

```powershell
ollama list
```

## 把 Ollama 加成 Key

1. 聊天框输入 `/keys` 进密钥管理页
2. 点 **「+ 添加 Key」**
3. 填：

| 字段 | 填什么 |
|------|------|
| Base URL | `http://127.0.0.1:11434/v1`（**注意带 `/v1`**） |
| Model | `qwen3.5:9b` |
| Type | `chat`（或 `vision` / `embed`，按用途） |
| API Key | **空着**（本地不要密钥） |

4. 保存

> 如果要同时用对话和看图，加两个 Key（type 分别 `chat` 和 `vision`），Model 可以填同一个 `qwen3.5:9b`（它支持多模态）。

## 给角色绑定 Ollama

去 `/models` 页（`/models` 斜杠命令）：

- 对话角色 → 选刚加的 chat Key
- 视觉角色 → 选 vision Key
- 嵌入角色 → 选 embed Key

选了立即生效，不用重启。

## 超时时间

本地模型快慢看电脑配置。Aether 的超时在 `config.json` 的 `llm` 段配置（`chat_timeout_seconds` 等）：

| 你的配置 | 建议超时 |
|------|---------|
| RTX 3060 及以上显卡 | 聊天 20s、看图 30s |
| GTX 1060 或入门显卡 | 聊天 40s、看图 60s |
| 没 GPU，纯 CPU 跑 | 聊天 60s、看图 120s |

## 你的电脑能跑多快

| 对比 | 有 GPU | 没 GPU（CPU） |
|------|---------|---------|
| 速度 | 快（RTX 3060 约 30 字/秒） | 慢（i7 约 5 字/秒） |
| 内存需求 | 6-8GB 显存（9B 模型） | 8-16GB 内存 |
| 同时跑几个请求 | 一般 1-2 个 | 受 CPU 核心限制 |
| 耗电 | 较高 | 较低 |

### 选哪个模型

| 模型 | 参数量 | 需要显存 | 速度 | 对话质量 |
|------|--------|---------|---------|---------|
| `qwen3.5:3b` | 30 亿 | ~2.5 GB | 很快 | 一般 |
| `qwen3.5:7b` | 70 亿 | ~5 GB | 快 | 不错 |
| `qwen3.5:9b` | 90 亿 | ~6 GB | 中等 | **推荐** |
| `qwen3.5:14b` | 140 亿 | ~9 GB | 偏慢 | 最好 |

建议 `qwen3.5:9b`，速度和效果平衡。

## 推荐搭配

1. **主力**：`/models` 给对话/视觉分配云端 Key（智谱等，快、稳）
2. **备用**：加上 Ollama Key，云端挂了切过去
3. **嵌入专用**：用本地嵌入模型（免费、快、隐私安全）

换模型只在 `/models` 页改下拉框，立即生效。

## 出问题了怎么办

### Ollama 连不上

```powershell
ollama serve          # 手动启动服务
netstat -ano | findstr ":11434"   # 确认端口在监听
```

确认 `/keys` 页 Base URL 是 `http://127.0.0.1:11434/v1`（带 `/v1`，不是 `http://localhost:11434`）。

### 回应太慢超时

- 把 `config.json` 里超时调大
- 换小模型（如 `qwen3.5:3b`）
- 看显卡是不是被别的程序占了

### 显卡没被用上

```powershell
ollama run qwen3.5:9b --verbose
# 输出里找 "cuda" 相关信息
```

驱动有问题就重装 NVIDIA 驱动和 CUDA Toolkit。

### 模型文件坏了

```powershell
ollama pull qwen3.5:9b --force
```

搞定了！本地模型跑起来后，Aether 就多了个随时可用的本地大脑，和云端搭配着用最灵活。
