# Aether 集成平台 Phase 4 设计：飞书机器人（W4）

**日期**: 2026-08-06
**状态**: 待评审
**前置**: Phase 1-2 已完成（插件骨架 + 小爱播报/直通 + 全局打断 + 热加载 + 插件管理）

---

## 1. 背景与目标

Phase 4 接入飞书当机器人助手——用户在飞书私聊或群聊 @机器人，消息进 Aether LLM 处理，回复发回飞书。

### 1.1 验收标准

| 验收项 | 行为 |
|--------|------|
| 私聊机器人 | 飞书私聊发消息 → Aether LLM 处理 → 回复发回飞书 |
| 群聊 @机器人 | 飞书群 @机器人 → Aether LLM 处理 → 回复发回群聊 |
| URL 验证 | 飞书配 webhook 时发 challenge → Aether 原样返回 |
| session 隔离 | 每个飞书会话独立对话历史（feishu_{chat_id}） |
| 小爱不受影响 | 飞书触发的回复不触发小爱广播（飞书走独立链路） |
| 解耦验证 | 删 integrations/feishu/ + 不配 webhook → 零影响 |

---

## 2. 决策记录

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 飞书 webhook 位置 | **宿主侧（feishu_routes.py）** | webhook 需公网端点 + 签名校验，不适合放子进程 |
| 发消息位置 | **子进程（飞书插件）** | 凭证隔离 + 崩溃隔离，与小爱同模式 |
| 飞书是否注册全局 output_sink | **不注册全局，走定向调用** | broadcast 是 fan-out（发给所有 sink），无法定向 chat_id。飞书触发回复若进 broadcast 会重复发送 + 误触发小爱 |
| 定向发送方式 | **IntegrationLayer.speak_to** | 新增方法，定向 RPC 到指定插件的 sink.speak，带 context（chat_id） |
| Dispatcher 调用方式 | **复用 dispatch()（REST 同步版）** | 返回 list[Instruction]，提取 ToastStream 拿 final_content。不造新轮子 |
| webhook URL 路径 | **/webhook/feishu（不走 /api 前缀）** | 避开 api_token_guard 鉴权中间件 + global_rate_limit |
| session 映射 | **chat_id → "feishu_{chat_id}"** | 每个飞书会话独立历史 |
| 场景范围 | **私聊 + 群聊 @机器人** | 飞书事件 API 两种都处理，群聊只在 @机器人 时响应 |
| 公网暴露 | **用户自配（ngrok/公网IP/CF Tunnel）** | 代码就绪，URL 后配。不绑定特定方案 |

---

## 3. 架构总览

### 3.1 数据流（端到端）

```
飞书用户发消息（私聊/群@机器人）
  → 飞书服务器 POST /webhook/feishu（Aether 宿主）
  → 签名校验（verification_token / encrypt_key）
  → 解析事件（im.message.receive_v1）
      ├─ 提取消息内容（去掉 @mention 纯文本）
      ├─ chat_id（会话 ID）
      └─ user_id（发送者）
  → session 映射: "feishu_{chat_id}"
  → Dispatcher.dispatch(event, user_id="feishu_{user_id}")
      → LLM 处理（ReAct agent，可调 HA 设备控制工具）
      → 收集 list[Instruction]
      → 提取 final_content（ToastStream）
  → IntegrationLayer.speak_to("feishu", final_content, {"chat_id": chat_id})
      → RPC sink.speak {text, chat_id}
      → FeishuSink.speak
          → 获取 tenant_access_token（app_id+app_secret 换）
          → POST 飞书发消息 API（发到 chat_id）
  → 用户在飞书收到回复
```

### 3.2 与小爱广播的关系

```
小爱广播（Phase 1-2，不变）:
  Dispatcher.dispatch() → broadcast 钩子 → SinkManager.broadcast → 小爱念

飞书回复（Phase 4，独立链路）:
  webhook → dispatch() → 拿回复文本 → speak_to("feishu") → 飞书发消息
```

**关键**：飞书触发的 dispatch() 仍会触发 broadcast 钩子（小爱可能念），这是预期行为——飞书跟 Aether 对话，小爱同步播报（如果小爱广播开关开着）。飞书回复本身不走 broadcast，走定向 speak_to。

### 3.3 为什么飞书不走全局 broadcast

| 问题 | 全局 broadcast 方案 | 定向 speak_to 方案 |
|------|---------------------|-------------------|
| 定向 chat_id | broadcast 无 target 参数，飞书不知道发哪 | speak_to 带 context，飞书从 context 拿 chat_id |
| 重复发送 | webhook 返回 + broadcast 又发 | webhook 只走 speak_to，不重复 |
| 小爱误触发 | 飞书回复进 broadcast → 小爱也念 | speak_to 只发飞书，小爱不受影响 |
| 小爱同步播报 | 无法控制 | broadcast 钩子仍触发（预期行为，可开关） |

---

## 4. 组件设计

### 4.1 飞书插件（`integrations/feishu/`）—— 子进程

#### manifest.json

```json
{
    "id": "feishu",
    "name": "飞书机器人",
    "version": "1.0.0",
    "aether_api_version": "1",
    "author": "Aether",
    "description": "飞书聊天机器人（文字双向）",
    "entry": "plugin.py",
    "capabilities": [
        {
            "type": "output_sink",
            "id": "feishu_sink",
            "priority": 90,
            "config_schema": {}
        }
    ],
    "permissions": [],
    "secrets": ["feishu_app_id", "feishu_app_secret"],
    "ui_contributions": [],
    "resources": {
        "max_memory_mb": 128,
        "restart_on_crash": true,
        "max_restarts": 3
    }
}
```

> **注**：飞书声明 output_sink，但不被 SinkManager.broadcast fan-out 到——因为宿主走 speak_to 定向调用。SinkManager._collect_sink_processes 会收集到飞书，但飞书触发的回复不走 broadcast，小爱触发的广播 fan-out 到飞书时会发到一个无效 chat_id（飞书 speak 从 msg_id 拿不到 chat_id 就跳过）。这是可接受的——broadcast 的 msg_id 是 request_id 不是 chat_id，飞书 speak 识别到非 chat_id 格式就 no-op。

#### plugin.py

```python
class FeishuSink(OutputSink):
    """飞书发消息 sink。

    speak(text, msg_id) —— msg_id 在定向调用时是 chat_id（格式 feishu_xxx），
    在 broadcast fan-out 时是 request_id（非 chat_id 格式，跳过）。
    """

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_cache = None  # tenant_access_token 缓存 + 过期时间
        self._seq_lock = asyncio.Lock()  # 串行发消息（飞书 API 限频）

    async def speak(self, text: str, msg_id: str = "") -> dict:
        # msg_id = chat_id（定向调用时宿主传入）
        if not msg_id or not msg_id.startswith("feishu_"):
            return {"ok": False, "skipped": True}  # broadcast fan-out 的乱入，跳过
        chat_id = msg_id
        token = await self._get_tenant_token()
        async with self._seq_lock:
            await self._send_message(token, chat_id, text)
        return {"ok": True, "chat_id": chat_id}

    async def interrupt(self) -> dict:
        return {"ok": True}  # 飞书无 TTS 可打断，no-op

    async def _get_tenant_token(self) -> str:
        """获取 tenant_access_token（缓存 + 自动刷新）。

        POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
        body: {"app_id": self._app_id, "app_secret": self._app_secret}
        返回: {"tenant_access_token": "t-xxx", "expire": 7200}
        缓存 token + 记录过期时间，过期前 60 秒自动刷新。
        """
        if self._token_cache and not self._token_expired():
            return self._token_cache
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            data = resp.json()
            self._token_cache = data["tenant_access_token"]
            self._token_expire = asyncio.get_event_loop().time() + data.get("expire", 7200) - 60
            return self._token_cache

    async def _send_message(self, token: str, chat_id: str, text: str):
        """POST 飞书发消息 API。

        POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id
        header: Authorization: Bearer {token}
        body: {"receive_id": chat_id, "msg_type": "text",
               "content": json.dumps({"text": text})}
        """
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"receive_id": chat_id, "msg_type": "text",
                      "content": json.dumps({"text": text})},
            )
```

### 4.2 webhook 路由（`app/routes/feishu_routes.py`）—— 宿主侧

```python
"""飞书 webhook 路由。"""

router = APIRouter()


@router.post("/webhook/feishu")
async def feishu_webhook(request: Request):
    """飞书事件回调入口。

    挂在 /webhook/feishu（不走 /api 前缀），避开 api_token_guard 鉴权。
    飞书自己用 verification_token/encrypt_key 做校验。
    """
    body = await request.json()

    # 1. URL 验证 challenge（飞书配 webhook 时）
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    # 2. 解析事件
    event = body.get("event", {})
    msg_type = event.get("message", {}).get("message_type")
    if msg_type != "text":
        return {"ok": True}  # 非文本消息忽略

    # 3. 提取消息内容 + chat_id + user_id
    message = event["message"]
    chat_id = message["chat_id"]
    user_id = event["sender"]["sender_id"]["open_id"]
    raw_content = json.loads(message["content"]).get("text", "")

    # 4. 去掉 @mention（群聊时消息含 @_user_1）
    query = re.sub(r"@_user_\d+", "", raw_content).strip()
    if not query:
        return {"ok": True}

    # 5. 调 Dispatcher（复用 REST dispatch）
    container = get_container()
    session_id = f"feishu_{chat_id}"
    event_obj = Event.build_event(
        Nlp.Request(query=query),
        request_id=new_request_id(),
        session_id=session_id,
    )
    instructions = await container.dispatcher.dispatch(
        event_obj, user_id=f"feishu_{user_id}"
    )

    # 6. 提取 final_content
    final_content = _extract_final_content(instructions)
    if not final_content:
        return {"ok": True}

    # 7. 定向发到飞书
    if container.integration_layer:
        await container.integration_layer.speak_to(
            "feishu", final_content, {"chat_id": chat_id}
        )

    return {"ok": True}


def _extract_final_content(instructions: list) -> str:
    """从 Instruction 列表提取 ToastStream final_content。"""
    for inst in instructions:
        payload = inst.get("payload", inst)  # model_dump or raw
        ns = inst.get("header", {}).get("namespace", "")
        name = inst.get("header", {}).get("name", "")
        if ns == "Template" and name == "ToastStream":
            return payload.get("stream", "")
    return ""
```

### 4.3 IntegrationLayer.speak_to（新增定向发送）

```python
async def speak_to(self, plugin_id: str, text: str, context: dict) -> dict:
    """定向调某插件的 sink.speak（带上下文，如飞书 chat_id）。

    与 broadcast 的区别：只发给指定插件，不走 fan-out。
    用于飞书 webhook 拿到回复后定向发到对应 chat_id。
    """
    proc = self._supervisor.get_process(plugin_id)
    if proc and proc.is_alive:
        # chat_id 作为 msg_id 传入（飞书 speak 从 msg_id 读 chat_id）
        chat_id = context.get("chat_id", "")
        try:
            return await proc.call(METHOD_SPEAK, {"text": text, "msg_id": chat_id})
        except Exception as exc:
            logger.warning("speak_to %s 失败: %s", plugin_id, exc)
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"插件 {plugin_id} 未运行"}
```

### 4.4 凭证配置

#### .env（用户填）

```env
FEISHU_APP_ID=cli_xxxx
FEISHU_APP_SECRET=xxxx
FEISHU_VERIFICATION_TOKEN=xxxx
FEISHU_ENCRYPT_KEY=xxxx
```

#### _build_plugin_env 扩展（main.py）

```python
# secret_map 扩展（现有 ha_url/ha_token 旁加飞书凭证）
secret_map = {
    "ha_url": ("AETHER_HA_URL", ha_url),
    "ha_token": ("AETHER_HA_TOKEN", ha_token),
    "feishu_app_id": ("AETHER_FEISHU_APP_ID", os.environ.get("FEISHU_APP_ID", "")),
    "feishu_app_secret": ("AETHER_FEISHU_APP_SECRET", os.environ.get("FEISHU_APP_SECRET", "")),
}
```

飞书插件 manifest 声明 `secrets: ["feishu_app_id", "feishu_app_secret"]`，宿主按声明注入环境变量。

#### webhook 签名校验

webhook 路由用 `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` 校验请求（从环境变量读，不进插件进程）。

---

## 5. 代码结构

### 新增文件

```
integrations/feishu/                       ← 飞书插件（独立子进程）
├── manifest.json
└── plugin.py                              ← FeishuSink + FeishuPlugin

app/routes/feishu_routes.py                ← webhook 接收 + 签名校验
```

### 修改文件

```
app/integration/integration_layer.py       ← 加 speak_to 方法
app/main.py                                ← _build_plugin_env 加飞书凭证 + 注册 feishu_router
```

### 测试文件

```
tests/test_feishu_sink.py                  ← FeishuSink 发消息逻辑（mock HTTP）
tests/test_feishu_webhook.py               ← webhook 路由（challenge + 事件解析 + session 映射）
tests/test_speak_to.py                     ← IntegrationLayer.speak_to 定向发送
```

---

## 6. 边界与错误处理

| 场景 | 行为 |
|------|------|
| 飞书插件未启用 | speak_to 返回 {ok:False, error:"未运行"}，webhook 正常返回 |
| 飞书发消息 API 失败 | speak_to 抛异常，webhook 记日志，返回 200（不阻塞飞书重试） |
| tenant_access_token 过期 | FeishuSink 自动刷新（缓存 + 过期检测） |
| 非文本消息（图片/语音） | webhook 忽略，返回 {ok:True} |
| 群聊不 @机器人 | 消息不含 mention，正常处理（或按需只处理 @mention） |
| 飞书触发的回复触发小爱广播 | 预期行为（broadcast 钩子仍触发），用户可用小爱广播开关关闭 |
| broadcast fan-out 误到飞书 | 飞书 speak 检查 msg_id 格式，非 chat_id 格式则 skip（no-op） |

---

## 7. 不在 Phase 4 做的

- 飞书多用户与 Aether 本地用户打通（defer，多用户隔离）
- 飞书主动推送（非被动回复，如定时提醒发到飞书）
- 飞书富文本/卡片消息（V1 只纯文本）
- 飞书 @机器人 的精确 mention 解析（V1 用正则去 @，够用）
- Phase 3 双向 RPC（反向调用），飞书插件仍直连飞书 API（跟小爱直连 HA 同模式）

---

## 8. 面试谈资

| 主题 | 体现点 |
|------|--------|
| webhook 安全 | 签名校验 + 避开框架鉴权中间件 + 飞书 challenge 验证 |
| 定向 vs 广播 | 飞书走定向 speak_to 而非全局 broadcast，解决 fan-out 无法定向的问题 |
| session 隔离 | chat_id → session_id 映射，多飞书会话独立历史 |
| 复用现有架构 | 复用 REST dispatch + manifest secrets + 插件 SDK，不造新轮子 |
| 凭证管理 | .env + manifest secrets 声明 + 宿主注入，插件进程不直接读配置文件 |
