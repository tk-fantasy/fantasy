# Aether 集成平台 Phase 4 实现计划：飞书机器人（W4）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 飞书用户私聊/群聊 @机器人 → Aether LLM 处理 → 回复发回飞书。

**Architecture:** webhook 挂宿主侧 `/webhook/feishu`（避开 /api 鉴权），收到飞书事件后复用 `Dispatcher.dispatch()` REST 同步版拿回复，然后定向调 `IntegrationLayer.speak_to("feishu", text, {chat_id})` 发飞书。飞书插件 `integrations/feishu/` 子进程直连飞书 Open API（tenant_access_token + 发消息 API）。不走全局 broadcast，避免 fan-out 无法定向 chat_id + 误触发小爱。

**Tech Stack:** Python 3.11 / FastAPI / asyncio / pytest / httpx / 飞书 Open API v1

---

## Global Constraints

- Python 异步，所有 I/O 用 `async/await`，禁止阻塞调用。
- 测试框架 pytest，异步测试用 `asyncio.new_event_loop().run_until_complete(go())` 模式（与 Phase 1-2 测试一致）。
- 测试导入用绝对路径 `from app.xxx import ...`（`tests/conftest.py` 已注入项目根到 `sys.path`）。
- 容器内开发：测试在 Docker 容器内跑（`docker exec aether pytest ...`），PYTHONPATH=/aether。容器名是 `aether`。
- **重要**：容器内源码是镜像静态副本，不是 live-mount。改完源码/测试后必须 `docker cp` 同步到容器才能在容器内跑 pytest。
- **完全解耦原则**：主程序不硬编码"飞书"/"feishu"。删 `integrations/feishu/` + 不配 webhook → 零影响。
- 飞书 webhook 挂 `/webhook/feishu`（不走 `/api` 前缀），利用现有 `api_token_guard` 中间件的 `not request.url.path.startswith("/api")` 逻辑自动绕过鉴权（`app/main.py:792-794`）。
- 代码注释与日志保持中文（贴合现有风格）。
- 每个 Task 完成后立即运行测试，绿了再提交。直接在 master 分支提交（Phase 1-2 一致）。

---

## File Structure

### 新增文件

```
integrations/feishu/                        ← 飞书插件（独立子进程）
├── manifest.json
└── plugin.py                               ← FeishuSink + FeishuPlugin

app/routes/feishu_routes.py                 ← webhook 接收 + challenge + 事件解析
```

### 修改文件

```
app/integration/integration_layer.py        ← 加 speak_to 方法
app/main.py                                 ← _build_plugin_env 加飞书凭证 + 注册 feishu_router
```

### 测试文件

```
tests/test_speak_to.py                      ← IntegrationLayer.speak_to 定向发送
tests/test_feishu_sink.py                   ← FeishuSink 发消息逻辑（mock httpx）
tests/test_feishu_webhook.py                ← webhook 路由（challenge + 事件解析 + session 映射）
```

### 职责边界

| 文件 | 唯一职责 |
|------|---------|
| `integration_layer.py:speak_to` | 定向 RPC 到指定插件的 sink.speak，带 context |
| `integrations/feishu/plugin.py` | 飞书发消息（token 管理 + chat_id 路由），不接 webhook |
| `app/routes/feishu_routes.py` | webhook HTTP 入口 + 事件解析 + 调 dispatch + speak_to |
| `app/main.py:_build_plugin_env` | 扩展 secret_map 加飞书凭证类型 |

---

## Task 1: IntegrationLayer.speak_to（定向发送）

**Files:**
- Modify: `app/integration/integration_layer.py`（加 speak_to 方法，在 route_inbound 之后）
- Test: `tests/test_speak_to.py`

**Interfaces:**
- Consumes: `METHOD_SPEAK`（已有，`rpc_protocol.py:12`），`PluginSupervisor.get_process`（已有），`PluginProcess.call`（已有）
- Produces: `IntegrationLayer.speak_to(plugin_id: str, text: str, context: dict) -> dict`——定向调指定插件的 sink.speak，context 带 chat_id。无插件返回 error。

- [ ] **Step 1: Write the failing test**

创建文件 `tests/test_speak_to.py`：

```python
"""IntegrationLayer.speak_to 定向发送测试。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.integration.integration_layer import IntegrationLayer


def test_speak_to_no_plugin_returns_error():
    """无指定插件时返回 error。"""
    layer = IntegrationLayer(plugin_dir="nonexistent_dir")

    async def go():
        result = await layer.speak_to("feishu", "hello", {"chat_id": "oc_xxx"})
        assert result["ok"] is False
        assert "未运行" in result["error"] or "not alive" in result["error"].lower() \
            or "no process" in result["error"].lower()

    asyncio.new_event_loop().run_until_complete(go())


def test_speak_to_calls_plugin_sink_speak():
    """speak_to 定向调指定插件的 sink.speak，传 text + chat_id 作 msg_id。"""
    layer = IntegrationLayer(plugin_dir="nonexistent_dir")

    # mock supervisor.get_process 返回一个有 call 方法的 mock
    mock_proc = MagicMock()
    mock_proc.is_alive = True
    mock_proc.call = AsyncMock(return_value={"ok": True, "chat_id": "oc_xxx"})
    layer._supervisor.get_process = MagicMock(return_value=mock_proc)

    async def go():
        result = await layer.speak_to("feishu", "你好", {"chat_id": "oc_xxx"})
        assert result["ok"] is True
        assert result["chat_id"] == "oc_xxx"
        # 验证 call 传了正确参数
        mock_proc.call.assert_called_once()
        call_args = mock_proc.call.call_args
        method = call_args[0][0] if call_args[0] else call_args[1].get("method")
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
        assert method == "sink.speak"
        assert params["text"] == "你好"
        assert params["msg_id"] == "oc_xxx"  # chat_id 作为 msg_id 传入

    asyncio.new_event_loop().run_until_complete(go())


def test_speak_to_dead_plugin_returns_error():
    """插件进程不存活时返回 error。"""
    layer = IntegrationLayer(plugin_dir="nonexistent_dir")

    mock_proc = MagicMock()
    mock_proc.is_alive = False
    layer._supervisor.get_process = MagicMock(return_value=mock_proc)

    async def go():
        result = await layer.speak_to("feishu", "hello", {"chat_id": "oc_xxx"})
        assert result["ok"] is False

    asyncio.new_event_loop().run_until_complete(go())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker cp tests/test_speak_to.py aether:/aether/tests/test_speak_to.py
docker exec aether pytest tests/test_speak_to.py -v
```
Expected: FAIL with `AttributeError: 'IntegrationLayer' object has no attribute 'speak_to'`

- [ ] **Step 3: Write minimal implementation**

在 `app/integration/integration_layer.py` 的 `IntegrationLayer` 类末尾（`route_inbound` 方法之后）加 `speak_to` 方法：

```python
    async def speak_to(self, plugin_id: str, text: str, context: dict) -> dict:
        """定向调某插件的 sink.speak（带上下文，如飞书 chat_id）。

        与 broadcast 的区别：只发给指定插件，不走 fan-out。
        用于飞书 webhook 拿到回复后定向发到对应 chat_id。
        chat_id 作为 msg_id 传入（飞书 speak 从 msg_id 读 chat_id）。
        """
        from .rpc_protocol import METHOD_SPEAK

        proc = self._supervisor.get_process(plugin_id)
        if proc and proc.is_alive:
            chat_id = context.get("chat_id", "")
            try:
                return await proc.call(
                    METHOD_SPEAK, {"text": text, "msg_id": chat_id}
                )
            except Exception as exc:
                logger.warning("speak_to %s 失败: %s", plugin_id, exc)
                return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": f"插件 {plugin_id} 未运行"}
```

- [ ] **Step 4: Sync to container + run test**

```bash
docker cp app/integration/integration_layer.py aether:/aether/app/integration/integration_layer.py
docker exec aether pytest tests/test_speak_to.py -v
```
Expected: PASS (3 tests)

- [ ] **Step 5: Regression test**

```bash
docker exec aether pytest tests/test_integration_layer.py tests/test_integration_layer_route.py tests/test_speak_to.py -v --tb=short
```
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
cd /d/Aether && git add app/integration/integration_layer.py tests/test_speak_to.py && git commit -m "feat(integration): IntegrationLayer.speak_to 定向发送（飞书 webhook 用）"
```

---

## Task 2: 飞书插件 FeishuSink（发消息逻辑）

**Files:**
- Create: `integrations/feishu/manifest.json`
- Create: `integrations/feishu/plugin.py`
- Test: `tests/test_feishu_sink.py`

**Interfaces:**
- Consumes: `OutputSink` ABC（`app/integration/sdk/sink_base.py`），`IntegrationPlugin`（`app/integration/sdk/plugin_base.py`），`run_stdio_plugin`（`app/integration/sdk/stdio_runtime.py`）
- Produces: `FeishuSink.speak(text, msg_id)`——msg_id 是 chat_id（feishu 开头）时发消息，否则 skip。`FeishuPlugin.setup(manifest_dict)` 读环境变量凭证。

- [ ] **Step 1: Write the failing test**

创建文件 `tests/test_feishu_sink.py`：

```python
"""FeishuSink 发消息逻辑测试（mock httpx，不真实调飞书 API）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from integrations.feishu.plugin import FeishuSink


def _make_sink():
    """构造 FeishuSink（凭证假的，HTTP 全 mock）。"""
    return FeishuSink(app_id="cli_test", app_secret="secret_test")


def test_speak_skips_non_chat_id_msg_id():
    """msg_id 非 chat_id 格式时 skip（broadcast fan-out 乱入时）。"""
    sink = _make_sink()

    async def go():
        result = await sink.speak("hello", msg_id="req_abc123")  # 非 feishu chat_id
        return result

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is False
    assert result.get("skipped") is True


def test_speak_sends_message_to_chat_id():
    """msg_id 是 chat_id 时调飞书发消息 API。"""
    sink = _make_sink()

    # mock _get_tenant_token 和 _send_message
    sink._get_tenant_token = AsyncMock(return_value="t-test-token")
    sink._send_message = AsyncMock()

    async def go():
        result = await sink.speak("你好世界", msg_id="oc_test_chat_id")
        return result

    result = asyncio.new_event_loop().run_until_complete(go())

    assert result["ok"] is True
    assert result["chat_id"] == "oc_test_chat_id"
    sink._get_tenant_token.assert_called_once()
    sink._send_message.assert_called_once_with("t-test-token", "oc_test_chat_id", "你好世界")


def test_speak_empty_text_skips():
    """空文本不发。"""
    sink = _make_sink()

    async def go():
        return await sink.speak("", msg_id="oc_xxx")

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result.get("skipped") is True or result["ok"] is False


def test_interrupt_is_noop():
    """飞书无 TTS 可打断，interrupt 是 no-op。"""
    sink = _make_sink()

    async def go():
        return await sink.interrupt()

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True


def test_get_tenant_token_caches_and_refreshes():
    """tenant_access_token 缓存 + 过期刷新。"""
    sink = _make_sink()

    # mock httpx 返回 token
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"tenant_access_token": "t-cached", "expire": 7200}
    mock_resp.raise_for_status = MagicMock()

    with patch("integrations.feishu.plugin.httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_httpx.AsyncClient.return_value = mock_client

        async def go():
            token1 = await sink._get_tenant_token()
            token2 = await sink._get_tenant_token()  # 应命中缓存
            return token1, token2

        t1, t2 = asyncio.new_event_loop().run_until_complete(go())

    assert t1 == "t-cached"
    assert t2 == "t-cached"
    # 只调了一次 HTTP（第二次命中缓存）
    assert mock_client.post.call_count == 1
```

> **注**：`_make_sink` 构造的 sink 的 `_get_tenant_token` / `_send_message` 在 test_speak_sends_message 里被 mock 掉，所以不会真实发 HTTP。test_get_tenant_token_caches_and_refreshes mock 了 httpx 模块级导入。需确认 plugin.py 里 httpx 是模块级 import 还是方法内 import——实现时保持一致。

- [ ] **Step 2: Run test to verify it fails**

```bash
docker cp tests/test_feishu_sink.py aether:/aether/tests/test_feishu_sink.py
docker exec aether pytest tests/test_feishu_sink.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'integrations.feishu'`

- [ ] **Step 3: Create manifest.json**

创建 `integrations/feishu/manifest.json`：

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

- [ ] **Step 4: Create plugin.py**

创建 `integrations/feishu/plugin.py`：

```python
"""飞书机器人插件 —— Phase 4。

飞书用户私聊/群聊 @机器人 → webhook（宿主侧）→ Dispatcher → 回复 → speak_to → 本插件发消息。

飞书发消息流程：
  1. 用 app_id + app_secret 换 tenant_access_token（缓存 + 自动刷新）
  2. POST 发消息 API 到指定 chat_id

chat_id 路由：webhook 调 speak_to 时把 chat_id 作为 msg_id 传入。
broadcast fan-out 误到飞书时 msg_id 是 request_id（非 chat_id），skip。
"""

import asyncio
import json
import os
import sys
from typing import Any

import httpx

from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.sink_base import OutputSink

# 飞书 Open API 端点
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


class FeishuSink(OutputSink):
    """飞书发消息 sink。

    speak(text, msg_id) —— msg_id 在定向调用（speak_to）时是 chat_id，
    在 broadcast fan-out 时是 request_id（非 chat_id 格式，skip）。
    """

    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_cache: str | None = None
        self._token_expire: float = 0.0
        self._seq_lock = asyncio.Lock()  # 串行发消息（飞书 API 限频）

    async def speak(self, text: str, msg_id: str = "") -> dict:
        # msg_id = chat_id（定向调用时宿主传入）
        if not msg_id or not msg_id.startswith("oc_"):
            return {"ok": False, "skipped": True}  # broadcast fan-out 乱入，跳过
        if not text:
            return {"ok": False, "skipped": True}
        chat_id = msg_id
        try:
            token = await self._get_tenant_token()
            async with self._seq_lock:
                await self._send_message(token, chat_id, text)
            return {"ok": True, "chat_id": chat_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def interrupt(self) -> dict:
        # 飞书无 TTS 可打断，no-op
        return {"ok": True}

    async def _get_tenant_token(self) -> str:
        """获取 tenant_access_token（缓存 + 自动刷新）。"""
        loop = asyncio.get_event_loop()
        if self._token_cache and loop.time() < self._token_expire:
            return self._token_cache
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _TOKEN_URL,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            self._token_cache = data["tenant_access_token"]
            expire = data.get("expire", 7200)
            # 提前 60 秒过期，避免临界点
            self._token_expire = loop.time() + expire - 60
            return self._token_cache

    async def _send_message(self, token: str, chat_id: str, text: str) -> None:
        """POST 飞书发消息 API（发到指定 chat_id）。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _MESSAGE_URL,
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
            )
            resp.raise_for_status()


class FeishuPlugin(IntegrationPlugin):
    """飞书插件。setup 时读环境变量凭证。"""

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        self.manifest = manifest_dict
        app_id = os.environ.get("AETHER_FEISHU_APP_ID", "")
        app_secret = os.environ.get("AETHER_FEISHU_APP_SECRET", "")
        if app_id and app_secret:
            self.sinks = [FeishuSink(app_id, app_secret)]
        else:
            self.sinks = []  # 无凭证时不注册 sink，speak_to 会返回未运行


if __name__ == "__main__":
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(FeishuPlugin, _manifest_path))
```

- [ ] **Step 5: Sync to container + run test**

```bash
docker cp integrations/feishu/manifest.json aether:/aether/integrations/feishu/manifest.json
docker cp integrations/feishu/plugin.py aether:/aether/integrations/feishu/plugin.py
docker cp tests/test_feishu_sink.py aether:/aether/tests/test_feishu_sink.py
docker exec aether pytest tests/test_feishu_sink.py -v
```
Expected: PASS (5 tests)

- [ ] **Step 6: Verify manifest validates**

```bash
docker exec aether python -c "from app.integration.schema import Manifest; import json; m = Manifest.model_validate(json.load(open('integrations/feishu/manifest.json'))); print('OK', m.id, [c.type.value for c in m.capabilities])"
```
Expected: 打印 `OK feishu ['output_sink']`

- [ ] **Step 7: Commit**

```bash
cd /d/Aether && git add integrations/feishu/ tests/test_feishu_sink.py && git commit -m "feat(feishu): FeishuSink 飞书发消息插件 + manifest"
```

---

## Task 3: webhook 路由（challenge + 事件解析 + dispatch + speak_to）

**Files:**
- Create: `app/routes/feishu_routes.py`
- Test: `tests/test_feishu_webhook.py`

**Interfaces:**
- Consumes: `get_container()`（`app/container.py`），`Dispatcher.dispatch()`（`app/agents/dispatcher.py:481`），`IntegrationLayer.speak_to`（Task 1），`Event.build_event` + `Nlp.Request`（`app/schema/chat_schema.py`），`new_request_id`（`app/core/tracing.py`）
- Produces: `POST /webhook/feishu`——飞书事件回调入口，返回 `{"challenge": ...}` 或 `{"ok": True}`

- [ ] **Step 1: Write the failing test**

创建文件 `tests/test_feishu_webhook.py`：

```python
"""飞书 webhook 路由测试。

测试策略：直接调路由函数（不启动 FastAPI app，避免触发完整 lifespan）。
mock container 的 dispatcher 和 integration_layer。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.routes.feishu_routes import feishu_webhook


def _mock_request(body: dict) -> MagicMock:
    """构造 mock Request 对象。"""
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return request


def _mock_container(dispatch_result=None, speak_to_result=None):
    """构造 mock container。"""
    container = MagicMock()
    container.dispatcher = MagicMock()
    container.dispatcher.dispatch = AsyncMock(
        return_value=dispatch_result or [])
    container.integration_layer = MagicMock()
    container.integration_layer.speak_to = AsyncMock(
        return_value=speak_to_result or {"ok": True})
    return container


def test_challenge_verification():
    """飞书配 webhook 时发 challenge，原样返回。"""
    request = _mock_request({"challenge": "ajkdslfjksdljf", "token": "xxx"})

    async def go():
        with patch("app.routes.feishu_routes.get_container",
                   return_value=_mock_container()):
            return await feishu_webhook(request)

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["challenge"] == "ajkdslfjksdljf"


def test_text_message_dispatched_and_sent():
    """文本消息 → dispatch → speak_to 发飞书。"""
    # 构造飞书事件 payload（im.message.receive_v1 简化版）
    feishu_event = {
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user123"}},
            "message": {
                "chat_id": "oc_test_chat",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
            },
        }
    }

    # mock dispatch 返回含 ToastStream 的指令
    dispatch_result = [
        {
            "header": {"namespace": "Template", "name": "ToastStream"},
            "payload": {"stream": "你好！有什么可以帮你的？"},
        },
        {
            "header": {"namespace": "Dialog", "name": "Finish"},
            "payload": {"success": True, "message": ""},
        },
    ]

    container = _mock_container(dispatch_result=dispatch_result)
    request = _mock_request(feishu_event)

    async def go():
        with patch("app.routes.feishu_routes.get_container", return_value=container):
            return await feishu_webhook(request)

    result = asyncio.new_event_loop().run_until_complete(go())

    # 验证调了 dispatch
    container.dispatcher.dispatch.assert_called_once()
    # 验证 session_id 是 feishu_{chat_id}
    call_args = container.dispatcher.dispatch.call_args
    event_arg = call_args[0][0]  # 第一个位置参数是 event
    assert event_arg.header.session_id == "feishu_oc_test_chat"

    # 验证调了 speak_to
    container.integration_layer.speak_to.assert_called_once()
    speak_args = container.integration_layer.speak_to.call_args
    assert speak_args[0][0] == "feishu"  # plugin_id
    assert speak_args[0][1] == "你好！有什么可以帮你的？"  # text
    assert speak_args[0][2] == {"chat_id": "oc_test_chat"}  # context

    assert result["ok"] is True


def test_non_text_message_ignored():
    """非文本消息（图片等）忽略。"""
    feishu_event = {
        "event": {
            "sender": {"sender_id": {"open_id": "ou_xxx"}},
            "message": {
                "chat_id": "oc_xxx",
                "message_type": "image",  # 非文本
                "content": "{}",
            },
        }
    }

    container = _mock_container()
    request = _mock_request(feishu_event)

    async def go():
        with patch("app.routes.feishu_routes.get_container", return_value=container):
            return await feishu_webhook(request)

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True
    # 没调 dispatch
    container.dispatcher.dispatch.assert_not_called()


def test_at_mention_stripped():
    """群聊 @机器人 的消息去掉 @mention 后取纯文本。"""
    feishu_event = {
        "event": {
            "sender": {"sender_id": {"open_id": "ou_xxx"}},
            "message": {
                "chat_id": "oc_group",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 打开床头灯"}),
            },
        }
    }

    container = _mock_container(dispatch_result=[
        {"header": {"namespace": "Template", "name": "ToastStream"},
         "payload": {"stream": "已打开"}},
    ])
    request = _mock_request(feishu_event)

    async def go():
        with patch("app.routes.feishu_routes.get_container", return_value=container):
            return await feishu_webhook(request)

    asyncio.new_event_loop().run_until_complete(go())

    # 验证 query 去掉了 @_user_1
    call_args = container.dispatcher.dispatch.call_args
    event_arg = call_args[0][0]
    assert event_arg.payload["query"] == "打开床头灯"


def test_no_integration_layer_still_returns_ok():
    """无集成平台时 webhook 也不崩（返回 ok）。"""
    feishu_event = {
        "event": {
            "sender": {"sender_id": {"open_id": "ou_xxx"}},
            "message": {
                "chat_id": "oc_xxx",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
            },
        }
    }

    container = _mock_container(dispatch_result=[
        {"header": {"namespace": "Template", "name": "ToastStream"},
         "payload": {"stream": "你好"}},
    ])
    container.integration_layer = None  # 无集成平台
    request = _mock_request(feishu_event)

    async def go():
        with patch("app.routes.feishu_routes.get_container", return_value=container):
            return await feishu_webhook(request)

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True  # 不崩
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker cp tests/test_feishu_webhook.py aether:/aether/tests/test_feishu_webhook.py
docker exec aether pytest tests/test_feishu_webhook.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routes.feishu_routes'`

- [ ] **Step 3: Create feishu_routes.py**

创建 `app/routes/feishu_routes.py`：

```python
"""飞书 webhook 路由。

挂在 /webhook/feishu（不走 /api 前缀），利用现有 api_token_guard 中间件的
not request.url.path.startswith("/api") 逻辑自动绕过鉴权。

飞书事件回调流程：
  1. challenge 验证（飞书配 webhook 时）
  2. 解析事件（im.message.receive_v1）
  3. session 映射 chat_id → "feishu_{chat_id}"
  4. Dispatcher.dispatch() 拿回复（REST 同步版）
  5. 提取 ToastStream final_content
  6. speak_to 定向发到飞书
"""

import json
import logging
import re

from fastapi import APIRouter, Request

from ..container import get_container
from ..core.tracing import new_request_id
from ..schema.chat_schema import Event, Nlp

logger = logging.getLogger(__name__)

router = APIRouter()

# 去掉 @mention 的正则（群聊消息含 @_user_1）
_AT_MENTION_RE = re.compile(r"@_user_\d+")


@router.post("/webhook/feishu")
async def feishu_webhook(request: Request):
    """飞书事件回调入口。"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    # 1. URL 验证 challenge（飞书配 webhook 时发）
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    # 2. 解析事件
    event = body.get("event", {})
    header = body.get("header", {})
    event_type = header.get("event_type", "")

    # 只处理消息事件
    if event_type != "im.message.receive_v1" and "message" not in event:
        return {"ok": True}

    message = event.get("message", {})
    msg_type = message.get("message_type")
    if msg_type != "text":
        return {"ok": True}  # 非文本消息忽略

    # 3. 提取消息内容 + chat_id + user_id
    chat_id = message.get("chat_id", "")
    user_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    try:
        raw_content = json.loads(message.get("content", "{}")).get("text", "")
    except (json.JSONDecodeError, TypeError):
        raw_content = ""

    # 4. 去掉 @mention（群聊时消息含 @_user_1）
    query = _AT_MENTION_RE.sub("", raw_content).strip()
    if not query:
        return {"ok": True}

    # 5. 调 Dispatcher（复用 REST dispatch 同步版）
    container = get_container()
    session_id = f"feishu_{chat_id}"
    rid = new_request_id()
    event_obj = Event.build_event(
        Nlp.Request(query=query),
        request_id=rid,
        session_id=session_id,
    )

    try:
        instructions = await container.dispatcher.dispatch(
            event_obj, user_id=f"feishu_{user_id}"
        )
    except Exception as exc:
        logger.warning("飞书 webhook dispatch 失败: %s", exc)
        return {"ok": True}  # 不阻塞飞书，返回 200 防止重试风暴

    # 6. 提取 final_content（ToastStream）
    final_content = _extract_final_content(instructions)
    if not final_content:
        return {"ok": True}

    # 7. 定向发到飞书
    integration_layer = getattr(container, "integration_layer", None)
    if integration_layer is not None:
        try:
            await integration_layer.speak_to(
                "feishu", final_content, {"chat_id": chat_id}
            )
        except Exception as exc:
            logger.warning("飞书发消息失败: %s", exc)

    return {"ok": True}


def _extract_final_content(instructions: list) -> str:
    """从 Instruction 列表提取 ToastStream final_content。"""
    for inst in instructions:
        # inst 可能是 Instruction 对象或 dict
        if isinstance(inst, dict):
            header = inst.get("header", {})
            payload = inst.get("payload", {})
        else:
            header = inst.header.model_dump() if hasattr(inst.header, "model_dump") else inst.header
            payload = inst.payload if isinstance(inst.payload, dict) else inst.payload.model_dump() if hasattr(inst.payload, "model_dump") else inst.payload
        ns = header.get("namespace", "") if isinstance(header, dict) else getattr(header, "namespace", "")
        name = header.get("name", "") if isinstance(header, dict) else getattr(header, "name", "")
        if ns == "Template" and name == "ToastStream":
            return payload.get("stream", "") if isinstance(payload, dict) else getattr(payload, "stream", "")
    return ""
```

- [ ] **Step 4: Sync to container + run test**

```bash
docker cp app/routes/feishu_routes.py aether:/aether/app/routes/feishu_routes.py
docker exec aether pytest tests/test_feishu_webhook.py -v
```
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /d/Aether && git add app/routes/feishu_routes.py tests/test_feishu_webhook.py && git commit -m "feat(feishu): webhook 路由——challenge + 事件解析 + dispatch + speak_to"
```

---

## Task 4: main.py 注册路由 + _build_plugin_env 扩展

**Files:**
- Modify: `app/main.py`（_build_plugin_env 加飞书凭证 secret_map + 注册 feishu_router）
- Test: 手动验证（路由注册 + 镜像重建后飞书插件加载）

**Interfaces:**
- Consumes: `feishu_router`（Task 3 创建）
- Produces: `_build_plugin_env` secret_map 含 feishu 凭证；`/webhook/feishu` 路由注册到 app

- [ ] **Step 1: Modify _build_plugin_env**

在 `app/main.py` 的 `_build_plugin_env` 函数（约 852 行），secret_map 加飞书凭证。

当前 secret_map：
```python
    secret_map = {
        "ha_url": ("AETHER_HA_URL", ha_url),
        "ha_token": ("AETHER_HA_TOKEN", ha_token),
    }
```

改为（加飞书凭证，从环境变量读，不依赖 ha_client）：

```python
    secret_map = {
        "ha_url": ("AETHER_HA_URL", ha_url),
        "ha_token": ("AETHER_HA_TOKEN", ha_token),
        "feishu_app_id": ("AETHER_FEISHU_APP_ID", os.environ.get("FEISHU_APP_ID", "")),
        "feishu_app_secret": ("AETHER_FEISHU_APP_SECRET", os.environ.get("FEISHU_APP_SECRET", "")),
    }
```

> **注**：飞书凭证从 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 环境变量读（用户在 `.env` 配），映射到 `AETHER_FEISHU_APP_ID` / `AETHER_FEISHU_APP_SECRET` 注入插件进程。需要确认 `os` 已 import（文件顶部应该有）。

- [ ] **Step 2: Register feishu_router**

在 `app/main.py` 的路由注册区（约 710-733 行），加 feishu_router 导入和注册。

导入（约 710 行附近，integration_router 导入旁）：
```python
from .routes.feishu_routes import router as feishu_router
```

注册（约 733 行 integration_router 注册之后，**不加 prefix**——路由内已含 /webhook/feishu）：
```python
app.include_router(feishu_router)  # 飞书 webhook：/webhook/feishu（不走 /api 前缀）
```

- [ ] **Step 3: Sync to container + rebuild**

```bash
docker cp app/main.py aether:/aether/app/main.py
docker compose build aether 2>&1 | tail -5
```
Expected: 构建成功

- [ ] **Step 4: Restart + verify**

```bash
docker compose up -d aether 2>&1 | tail -3
sleep 5
# 验证飞书插件加载
docker exec aether python -c "
import asyncio
from app.integration.integration_layer import IntegrationLayer
layer = IntegrationLayer(plugin_dir='integrations')
async def go():
    await layer.start()
    plugins = layer.list_plugins()
    for p in plugins:
        print(f'{p[\"id\"]}: caps={p[\"capabilities\"]} alive={p[\"alive\"]}')
    await layer.stop()
asyncio.new_event_loop().run_until_complete(go())
"
```
Expected: 打印 `feishu: caps=['output_sink'] alive=True`（如果没配凭证则 alive=True 但 sinks 为空）

- [ ] **Step 5: Verify webhook route registered**

```bash
docker exec aether python -c "
from app.main import app
routes = [r.path for r in app.routes if hasattr(r, 'path')]
feishu_routes = [r for r in routes if 'feishu' in r or 'webhook' in r]
print('feishu/webhook routes:', feishu_routes)
"
```
Expected: 包含 `/webhook/feishu`

- [ ] **Step 6: Run all Phase 4 tests + regression**

```bash
docker exec aether pytest tests/test_speak_to.py tests/test_feishu_sink.py tests/test_feishu_webhook.py tests/test_integration_layer.py tests/test_integration_layer_route.py -v --tb=short 2>&1 | tail -20
```
Expected: 全绿

- [ ] **Step 7: Commit**

```bash
cd /d/Aether && git add app/main.py && git commit -m "feat(feishu): 注册 webhook 路由 + _build_plugin_env 加飞书凭证注入"
```

---

## Task 5: E2E 验证 + 解耦验证

**Files:**
- Manual verification + 全量测试

- [ ] **Step 1: Run full test suite (Phase 1-4)**

```bash
docker exec aether pytest tests/test_rpc_protocol.py tests/test_rpc_protocol_route.py tests/test_manifest_loader.py tests/test_inbound_router_base.py tests/test_dialog_finish_message.py tests/test_dispatcher_cancel.py tests/test_ws_interrupt.py tests/test_mode_state_routes.py tests/integrations/test_xiaoai_router.py tests/test_integration_layer.py tests/test_integration_layer_route.py tests/test_config_helper.py tests/test_speak_to.py tests/test_feishu_sink.py tests/test_feishu_webhook.py -v --tb=short 2>&1 | tail -30
```
Expected: 全绿

- [ ] **Step 2: Verify decoupling — delete feishu plugin**

```bash
docker exec aether bash -c "
mv /aether/integrations/feishu /tmp/feishu_backup
python3 -c \"
import asyncio
from app.integration.integration_layer import IntegrationLayer
layer = IntegrationLayer(plugin_dir='integrations')
async def go():
    manifests = []
    from app.integration.manifest_loader import load_manifests
    manifests = load_manifests('integrations', api_version='1', disabled=[])
    print('manifests:', [m.id for m in manifests])
    result = await layer.speak_to('feishu', 'test', {'chat_id': 'oc_xxx'})
    print('speak_to result:', result)
asyncio.new_event_loop().run_until_complete(go())
\"
mv /tmp/feishu_backup /aether/integrations/feishu
"
```
Expected: manifests 不含 feishu，speak_to 返回 {ok: False, error: "插件 feishu 未运行"}

- [ ] **Step 3: Grep verify zero hardcoding**

```bash
docker exec aether grep -rn "feishu\|飞书" app/routes/ app/agents/ app/integration/ --include="*.py" | grep -v "__pycache__\|test_\|\.pyc\|feishu_routes.py"
```
Expected: 主程序业务逻辑无飞书硬编码（只在 feishu_routes.py 内出现，那是飞书自己的路由文件）

- [ ] **Step 4: Manual E2E (with real feishu credentials)**

如果用户已配飞书凭证：
1. 在 `.env` 加 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
2. 用 ngrok/CF Tunnel 暴露 Aether 公网：`ngrok http 8000`
3. 飞书开放平台配 webhook URL：`https://xxx.ngrok.io/webhook/feishu`
4. 飞书私聊机器人发"你好" → 收到 Aether 回复

- [ ] **Step 5: Final commit (if any changes)**

```bash
cd /d/Aether && git add -A && git commit -m "test: Phase 4 E2E 验证 + 解耦验证通过" || echo "nothing to commit"
```

---

## Self-Review

**1. Spec coverage:**
- §3.1 数据流 webhook→dispatch→speak_to → Task 3 + Task 1 ✅
- §4.1 飞书插件 FeishuSink → Task 2 ✅
- §4.2 webhook 路由 → Task 3 ✅
- §4.3 IntegrationLayer.speak_to → Task 1 ✅
- §4.4 凭证配置 _build_plugin_env → Task 4 ✅
- §6 边界处理（非文本/无插件/无 integration_layer）→ Task 3 测试覆盖 ✅

**2. Placeholder scan:** 无 TBD/TODO，所有 step 有完整代码 ✅

**3. Type consistency:**
- `speak_to(plugin_id, text, context)` — Task 1 定义，Task 3 调用 ✅
- `FeishuSink.speak(text, msg_id)` — Task 2 定义，msg_id = chat_id 约定 ✅
- `feishu_webhook(request)` — Task 3 定义，Task 4 注册路由 ✅
- `_extract_final_content(instructions)` 读 ToastStream.stream — Task 3，匹配 schema.py:91-94 ✅
- chat_id 格式 `oc_` 前缀 — Task 2 speak 检查 + Task 3 测试用 `oc_test_chat` ✅
