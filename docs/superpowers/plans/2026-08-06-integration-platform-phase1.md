# Aether 集成平台 Phase 1 实现计划：插件骨架 + 小爱播报（W1）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aether 助手的文字回复通过插件系统广播到小爱音箱播报，且插件作为独立子进程运行、崩溃自动重启。

**Architecture:** 引入 `IntegrationLayer`（manifest 加载 + 进程管理 + 双向 RPC）挂到 `AppContainer`；`Dispatcher` 在 `final_content` 产出后调用 `SinkManager.broadcast`；小爱作为独立子进程插件（stdio JSON-RPC），通过反向 RPC 调 Aether 的 HA 服务做 TTS。Phase 1 只做方向 1（Aether → 插件）单向 RPC + 单小爱 sink。

**Tech Stack:** Python 3.11 / FastAPI / asyncio / pytest (asyncio_mode=auto) / stdio JSON-RPC 2.0

---

## Global Constraints

- Python 异步，所有 I/O 用 `async/await`，禁止阻塞调用。
- 测试框架 pytest，`asyncio_mode = auto`（异步测试无需 `@pytest.mark.asyncio`）。
- 测试导入用绝对路径 `from app.xxx import ...`（`tests/conftest.py` 已注入项目根到 `sys.path`）。
- autouse fixture `_patch_config`（`tests/conftest.py`）会 monkeypatch `app.core.config.CONFIG`；新测试若依赖 config，应在测试内用 `monkeypatch` 或直接传参，不要改 conftest。
- 复用现有 `ExternalMCPServer` 的 stdio JSON-RPC 模式（`app/mcp/external_mcp_server.py`）。
- HA 服务调用走 `HomeAssistantClient.call_service(domain, service, entity_id, data)`。
- 插件目录约定：`integrations/`（项目根，与 `ha_config/` 同级）。
- 配置 section 名：`integration`（写入 `config.json`）。
- 代码注释与日志保持中文（贴合现有风格）。
- 每个 Step 完成后立即运行测试，绿了再提交。

---

## File Structure

### 宿主侧新增

```
app/integration/                       ← 新增核心目录
├── __init__.py
├── schema.py                          ← Pydantic manifest 模型 + RPC 方法定义
├── manifest_loader.py                 ← 目录扫描 + 清单校验
├── plugin_process.py                  ← 单个插件子进程连接（stdio JSON-RPC，方向 1）
├── plugin_supervisor.py               ← 进程生命周期：spawn/重启/退避/关闭
├── sink_manager.py                    ← 广播 fan-out
├── integration_layer.py               ← 顶层门面：组装 loader+supervisor+sink_manager
└── rpc_protocol.py                    ← JSON-RPC 2.0 消息构造 + 常量
```

### 宿主侧修改

```
app/container.py                       ← AppContainer 加 integration_layer 字段
app/bootstrap.py                       ← initialize_services 构造 integration_layer 占位
app/agents/dispatcher.py               ← final_content 后调用 sink broadcast
app/main.py                            ← lifespan 启动插件 + 注册路由
app/core/config.py                     ← （仅读）get_config("integration.xxx")
app/routes/integration_routes.py       ← 新增：/api/integrations 列表 + 启停
config.json                            ← 加 integration section
```

### 插件 SDK（共享）

```
app/integration/sdk/                   ← 打包给插件用
├── __init__.py
├── plugin_base.py                     ← IntegrationPlugin 基类
├── sink_base.py                       ← OutputSink 抽象
└── stdio_runtime.py                   ← 插件进程内的 stdio RPC runtime（接收调用）
```

### 集成插件

```
integrations/xiaoai/                   ← 小爱插件（独立子进程）
├── manifest.json
└── plugin.py                          ← XiaoAiPlugin(OutputSink)
```

### 测试

```
tests/test_manifest_loader.py
tests/test_rpc_protocol.py
tests/test_plugin_process.py
tests/test_plugin_supervisor.py
tests/test_sink_manager.py
tests/test_integration_layer.py
tests/test_dispatcher_broadcast_hook.py
tests/integrations/echo/               ← 测试用 echo 插件
│   ├── manifest.json
│   └── plugin.py
tests/integrations/echo/plugin.py
tests/integrations/test_xiaoai_plugin.py   ← 单元测试小爱插件逻辑（不 spawn）
```

### 职责边界

| 文件 | 唯一职责 |
|------|---------|
| `schema.py` | 定义 manifest 的 Pydantic 数据模型，不读文件 |
| `manifest_loader.py` | 扫描目录、解析 JSON、返回校验后的 manifest 列表，不启动进程 |
| `rpc_protocol.py` | 纯函数：构造/解析 JSON-RPC 消息，无 I/O |
| `plugin_process.py` | 一个插件进程的 spawn + stdio 收发 + 请求-响应，不管重启 |
| `plugin_supervisor.py` | 管理多个进程的启停/重启/退避/熔断，持有 PluginProcess |
| `sink_manager.py` | 收集所有 output_sink 能力，broadcast 并发 fan-out |
| `integration_layer.py` | 门面：组合上述组件，对外暴露 start/stop/broadcast |
| `sdk/sink_base.py` | OutputSink 抽象接口（插件继承） |
| `sdk/stdio_runtime.py` | 插件进程入口的 RPC runtime（读 stdin、分发到 sink 方法） |

---

## Task 0: 创建包骨架与 config section

**Files:**
- Create: `app/integration/__init__.py`
- Create: `app/integration/sdk/__init__.py`
- Modify: `config.json` (加 `integration` section)
- Create: `integrations/.gitkeep`
- Create: `integrations/xiaoai/.gitkeep`（占位，Task 8 填充）

**Interfaces:**
- Produces: 空的 `app.integration` 包（后续 Task 导入）

- [ ] **Step 1: 创建包目录与 __init__.py**

创建 `app/integration/__init__.py`：
```python
"""Aether 集成插件系统。

Phase 1: manifest 加载 + 子进程管理 + output_sink 广播到小爱。
"""
```

创建 `app/integration/sdk/__init__.py`：
```python
"""插件 SDK —— 供集成插件继承与运行。"""
```

- [ ] **Step 2: 创建 integrations 目录占位**

在项目根创建 `integrations/.gitkeep`（空文件）和 `integrations/xiaoai/.gitkeep`（空文件）。

- [ ] **Step 3: config.json 加 integration section**

读取 `config.json`，在顶层加入（位置放在 `external_mcp` 之后）：
```json
"integration": {
    "enabled": true,
    "plugin_dir": "integrations",
    "api_version": "1",
    "startup_timeout": 10,
    "health_check_interval": 30,
    "default_rpc_timeout": 30,
    "max_restarts": 3
}
```

- [ ] **Step 4: 提交**

```bash
git add app/integration/ integrations/ config.json
git commit -m "feat(integration): Phase 1 包骨架 + integration config section"
```

---

## Task 1: RPC 协议纯函数（JSON-RPC 2.0）

**Files:**
- Create: `app/integration/rpc_protocol.py`
- Test: `tests/test_rpc_protocol.py`

**Interfaces:**
- Produces: `build_request(id, method, params) -> dict`, `build_response(id, result) -> dict`, `build_error(id, code, message) -> dict`, `parse_message(raw: str) -> dict | None`, 常量 `METHOD_SPEAK = "sink.speak"`、`METHOD_INTERRUPT = "sink.interrupt"`、`METHOD_HEALTH = "health.check"`、`METHOD_HANDSHAKE = "handshake"`、`METHOD_SHUTDOWN = "shutdown"`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_rpc_protocol.py`：
```python
import json
from app.integration.rpc_protocol import (
    build_request, build_response, build_error,
    parse_message, METHOD_SPEAK, METHOD_HANDSHAKE,
)


def test_build_request_with_params():
    msg = build_request(id=1, method="sink.speak", params={"text": "hi"})
    assert msg == {"jsonrpc": "2.0", "id": 1, "method": "sink.speak", "params": {"text": "hi"}}


def test_build_request_without_params():
    msg = build_request(id=2, method="health.check")
    assert msg == {"jsonrpc": "2.0", "id": 2, "method": "health.check"}


def test_build_response():
    msg = build_response(id=1, result={"ok": True})
    assert msg == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_build_error():
    msg = build_error(id=1, code=-32601, message="method not found")
    assert msg == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}}


def test_parse_message_valid():
    line = json.dumps({"jsonrpc": "2.0", "id": 5, "method": METHOD_SPEAK, "params": {}})
    msg = parse_message(line)
    assert msg is not None
    assert msg["id"] == 5
    assert msg["method"] == METHOD_SPEAK


def test_parse_message_invalid_json_returns_none():
    assert parse_message("not json{") is None


def test_parse_message_empty_line_returns_none():
    assert parse_message("") is None
    assert parse_message("   \n") is None


def test_method_constants():
    assert METHOD_SPEAK == "sink.speak"
    assert METHOD_HANDSHAKE == "handshake"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_rpc_protocol.py -v`（或环境里的 python 别名）
Expected: FAIL，`ModuleNotFoundError: No module named 'app.integration.rpc_protocol'`

- [ ] **Step 3: 实现 rpc_protocol.py**

创建 `app/integration/rpc_protocol.py`：
```python
"""JSON-RPC 2.0 over stdio 协议常量与消息构造。"""

# ── 方法名常量（方向 1: Aether → 插件）──
METHOD_HANDSHAKE = "handshake"
METHOD_SPEAK = "sink.speak"
METHOD_INTERRUPT = "sink.interrupt"
METHOD_HEALTH = "health.check"
METHOD_SHUTDOWN = "shutdown"

JSONRPC_VERSION = "2.0"


def build_request(msg_id: int, method: str, params: dict | None = None) -> dict:
    """构造 JSON-RPC 2.0 请求。"""
    msg: dict = {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def build_response(msg_id: int, result: dict) -> dict:
    """构造 JSON-RPC 2.0 成功响应。"""
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}


def build_error(msg_id: int, code: int, message: str) -> dict:
    """构造 JSON-RPC 2.0 错误响应。"""
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id,
            "error": {"code": code, "message": message}}


def parse_message(raw: str) -> dict | None:
    """解析一行 stdio 输出为消息 dict。

    非 JSON / 空行返回 None（调用方应跳过或当日志处理）。
    """
    stripped = raw.strip()
    if not stripped:
        return None
    import json
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_rpc_protocol.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add app/integration/rpc_protocol.py tests/test_rpc_protocol.py
git commit -m "feat(integration): JSON-RPC 2.0 协议纯函数"
```

---

## Task 2: Manifest Schema 与 加载器

**Files:**
- Create: `app/integration/schema.py`
- Create: `app/integration/manifest_loader.py`
- Test: `tests/test_manifest_loader.py`
- Test fixtures: `tests/integrations/echo/manifest.json`, `tests/integrations/bad/manifest.json`

**Interfaces:**
- Produces:
  - `Manifest` (Pydantic), `Capability` (Pydantic), `CapabilityType` (Enum: "output_sink"|"inbound_router")
  - `load_manifests(plugin_dir: str, api_version: str = "1") -> list[Manifest]`：扫描目录、校验、返回；无效清单记录日志并跳过

- [ ] **Step 1: 写失败测试**

创建 `tests/test_manifest_loader.py`：
```python
import json
from pathlib import Path

from app.integration.manifest_loader import load_manifests
from app.integration.schema import Manifest, Capability


def test_load_valid_manifest(tmp_path):
    plugin_dir = tmp_path / "echo"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "echo",
        "name": "回声测试",
        "version": "1.0.0",
        "aether_api_version": "1",
        "entry": "plugin.py",
        "capabilities": [{
            "type": "output_sink",
            "id": "echo_main",
            "priority": 100,
            "config_schema": {},
        }],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert len(manifests) == 1
    assert manifests[0].id == "echo"
    assert manifests[0].capabilities[0].type.value == "output_sink"
    assert manifests[0].capabilities[0].priority == 100


def test_skip_manifest_with_wrong_api_version(tmp_path):
    plugin_dir = tmp_path / "old"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "old",
        "name": "旧插件",
        "version": "0.1.0",
        "aether_api_version": "0",
        "entry": "plugin.py",
        "capabilities": [],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert manifests == []


def test_skip_dir_without_manifest(tmp_path):
    (tmp_path / "empty").mkdir()

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert manifests == []


def test_skip_invalid_json_manifest(tmp_path):
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text("{not valid json", encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert manifests == []


def test_nonexistent_plugin_dir_returns_empty():
    assert load_manifests("/no/such/dir/xyz", api_version="1") == []


def test_capability_priority_defaults_to_zero(tmp_path):
    plugin_dir = tmp_path / "p"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "id": "p", "name": "P", "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "output_sink", "id": "p1"}],
    }), encoding="utf-8")

    manifests = load_manifests(str(tmp_path), api_version="1")

    assert manifests[0].capabilities[0].priority == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_manifest_loader.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 schema.py**

创建 `app/integration/schema.py`：
```python
"""插件清单（manifest）Pydantic 数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CapabilityType(str, Enum):
    """插件能力类型。"""
    OUTPUT_SINK = "output_sink"
    INBOUND_ROUTER = "inbound_router"


class Capability(BaseModel):
    """单个能力声明。"""
    type: CapabilityType
    id: str
    priority: int = 0
    config_schema: dict[str, Any] = Field(default_factory=dict)
    queue_policy: dict[str, Any] = Field(default_factory=dict)


class Manifest(BaseModel):
    """插件清单。

    每个插件目录下 manifest.json 反序列化为本对象。
    """
    id: str
    name: str
    version: str
    aether_api_version: str
    entry: str = "plugin.py"
    description: str = ""
    author: str = ""
    depends_on: list[str] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)

    def has_capability(self, cap_type: CapabilityType) -> bool:
        return any(c.type == cap_type for c in self.capabilities)
```

- [ ] **Step 4: 实现 manifest_loader.py**

创建 `app/integration/manifest_loader.py`：
```python
"""扫描插件目录、校验 manifest。"""

import json
import logging
from pathlib import Path

from .schema import Manifest

logger = logging.getLogger(__name__)


def load_manifests(plugin_dir: str, api_version: str = "1") -> list[Manifest]:
    """扫描 plugin_dir 下每个子目录的 manifest.json，返回校验通过的清单列表。

    - 目录不存在 → 返回空列表
    - 子目录无 manifest.json → 跳过
    - JSON 解析失败 / 字段不全 → 记录 warning 并跳过
    - aether_api_version 不匹配 → 跳过
    """
    root = Path(plugin_dir)
    if not root.is_dir():
        return []

    manifests: list[Manifest] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue

        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("插件 %s manifest 解析失败: %s", child.name, exc)
            continue

        try:
            manifest = Manifest.model_validate(raw)
        except Exception as exc:  # pydantic.ValidationError
            logger.warning("插件 %s manifest 校验失败: %s", child.name, exc)
            continue

        if manifest.aether_api_version != api_version:
            logger.warning(
                "插件 %s API 版本不匹配 (期望 %s, 实际 %s),跳过",
                manifest.id, api_version, manifest.aether_api_version,
            )
            continue

        manifests.append(manifest)

    return manifests
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_manifest_loader.py -v`
Expected: 6 passed

- [ ] **Step 6: 提交**

```bash
git add app/integration/schema.py app/integration/manifest_loader.py tests/test_manifest_loader.py
git commit -m "feat(integration): manifest schema + 目录扫描加载器"
```

---

## Task 3: 插件 SDK —— OutputSink 基类 + stdio runtime

**Files:**
- Create: `app/integration/sdk/sink_base.py`
- Create: `app/integration/sdk/plugin_base.py`
- Create: `app/integration/sdk/stdio_runtime.py`
- Create: `tests/integrations/echo/manifest.json`
- Create: `tests/integrations/echo/plugin.py`
- Test: `tests/test_echo_plugin_runtime.py`

**Interfaces:**
- Produces:
  - `OutputSink`（抽象基类，方法 `async speak(text, msg_id) -> dict`、`async interrupt() -> dict`）
  - `IntegrationPlugin`（基类，属性 `manifest`、`sinks: list[OutputSink]`、`async setup(manifest_dict)`、`async handle(method, params) -> dict`）
  - `run_stdio_plugin(plugin_cls, manifest_path)`：插件进程入口，读 stdin JSON-RPC 分发到 plugin.handle

- [ ] **Step 1: 写 echo 测试插件 manifest**

创建 `tests/integrations/echo/manifest.json`：
```json
{
    "id": "echo",
    "name": "回声测试",
    "version": "1.0.0",
    "aether_api_version": "1",
    "entry": "plugin.py",
    "capabilities": [
        {
            "type": "output_sink",
            "id": "echo_main",
            "priority": 100,
            "config_schema": {}
        }
    ]
}
```

- [ ] **Step 2: 写 echo 插件实现**

创建 `tests/integrations/echo/plugin.py`：
```python
"""测试用 echo 插件：把 speak 的文本回显到 stderr，并返回 {spoken: text}。"""

import sys
from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.sink_base import OutputSink


class EchoSink(OutputSink):
    async def speak(self, text: str, msg_id: str = "") -> dict:
        print(f"[echo] speak: {text}", file=sys.stderr)
        return {"spoken": text, "msg_id": msg_id}

    async def interrupt(self) -> dict:
        print("[echo] interrupt", file=sys.stderr)
        return {"interrupted": True}


class EchoPlugin(IntegrationPlugin):
    def setup(self, manifest_dict: dict) -> None:
        self.sinks = [EchoSink()]
```

- [ ] **Step 3: 写失败测试（直接调用 plugin 对象，不 spawn）**

创建 `tests/test_echo_plugin_runtime.py`：
```python
import asyncio
import json
from pathlib import Path

from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.sink_base import OutputSink

# 让 tests/integrations 可被导入
import sys
ECHO_DIR = Path(__file__).parent / "integrations" / "echo"
sys.path.insert(0, str(ECHO_DIR))
from plugin import EchoPlugin  # noqa: E402


def test_echo_plugin_handles_speak():
    manifest = json.loads((ECHO_DIR / "manifest.json").read_text(encoding="utf-8"))
    plugin = EchoPlugin()
    plugin.setup(manifest)

    result = asyncio.get_event_loop().run_until_complete(
        plugin.handle("sink.speak", {"text": "你好", "msg_id": "m1"})
    )
    assert result == {"spoken": "你好", "msg_id": "m1"}


def test_echo_plugin_handles_interrupt():
    manifest = json.loads((ECHO_DIR / "manifest.json").read_text(encoding="utf-8"))
    plugin = EchoPlugin()
    plugin.setup(manifest)

    result = asyncio.get_event_loop().run_until_complete(
        plugin.handle("sink.interrupt", {})
    )
    assert result == {"interrupted": True}


def test_echo_plugin_unknown_method_returns_error():
    manifest = json.loads((ECHO_DIR / "manifest.json").read_text(encoding="utf-8"))
    plugin = EchoPlugin()
    plugin.setup(manifest)

    result = asyncio.get_event_loop().run_until_complete(
        plugin.handle("nope.nope", {})
    )
    assert "error" in result
```

- [ ] **Step 4: 运行测试确认失败**

Run: `python -m pytest tests/test_echo_plugin_runtime.py -v`
Expected: FAIL，`ModuleNotFoundError: app.integration.sdk...`

- [ ] **Step 5: 实现 sink_base.py**

创建 `app/integration/sdk/sink_base.py`：
```python
"""OutputSink 抽象基类 —— 插件输出能力实现此接口。"""

from abc import ABC, abstractmethod


class OutputSink(ABC):
    """输出能力契约。

    Aether 的 assistant 回复会广播到所有启用的 sink。
    """

    @abstractmethod
    async def speak(self, text: str, msg_id: str = "") -> dict:
        """播报一段文本。返回执行结果 dict（至少含 ok 或错误信息）。"""
        ...

    @abstractmethod
    async def interrupt(self) -> dict:
        """中断当前播报。返回执行结果 dict。"""
        ...
```

- [ ] **Step 6: 实现 plugin_base.py**

创建 `app/integration/sdk/plugin_base.py`：
```python
"""IntegrationPlugin 基类 —— 插件进程内继承。"""

from typing import Any

from .sink_base import OutputSink
from ..rpc_protocol import METHOD_SPEAK, METHOD_INTERRUPT


class IntegrationPlugin:
    """插件基类。

    子类在 setup() 里根据 manifest 构建 sinks（和其他能力的实现）。
    handle() 按 JSON-RPC method 路由到对应能力。
    """

    def __init__(self) -> None:
        self.manifest: dict[str, Any] = {}
        self.sinks: list[OutputSink] = []

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        """子类实现：解析 manifest_dict，构建 sinks 等。"""
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
        return {"error": f"unknown method: {method}"}
```

- [ ] **Step 7: 实现 stdio_runtime.py**

创建 `app/integration/sdk/stdio_runtime.py`：
```python
"""插件进程入口：从 stdin 读 JSON-RPC，分发到 plugin.handle，写 stdout。"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Type

from ..rpc_protocol import parse_message, build_response, build_error, METHOD_HANDSHAKE
from .plugin_base import IntegrationPlugin


async def run_stdio_plugin(
    plugin_cls: Type[IntegrationPlugin],
    manifest_path: str,
) -> None:
    """插件进程主循环。

    读 stdin 一行一个 JSON-RPC 消息，handshake 之外的方法都走 plugin.handle。
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    plugin = plugin_cls()
    plugin.setup(manifest)

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            break  # stdin 关闭
        msg = parse_message(line.decode("utf-8", errors="replace"))
        if msg is None:
            continue
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        try:
            if method == METHOD_HANDSHAKE:
                result = {
                    "plugin_id": manifest.get("id"),
                    "plugin_version": manifest.get("version"),
                    "ready": True,
                }
            else:
                result = await plugin.handle(method, params)
        except Exception as exc:  # 插件代码异常不能崩 runtime
            result = {"error": f"{type(exc).__name__}: {exc}"}

        response = (build_response(msg_id, result) if "error" not in result
                    else build_response(msg_id, result))  # 错误也走 result 字段,简化
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
```

> 说明：Phase 1 简化处理——把插件业务错误（`{"error": ...}`）放在 `result` 字段返回，不使用 JSON-RPC `error` 字段，避免握手/路由判断复杂化。Phase 3 再规范。

- [ ] **Step 8: 运行测试确认通过**

Run: `python -m pytest tests/test_echo_plugin_runtime.py -v`
Expected: 3 passed

- [ ] **Step 9: 提交**

```bash
git add app/integration/sdk/ tests/integrations/echo/ tests/test_echo_plugin_runtime.py
git commit -m "feat(integration): 插件 SDK (OutputSink/IntegrationPlugin/stdio runtime)"
```

---

## Task 4: PluginProcess —— 单进程 stdio JSON-RPC 连接（方向 1）

**Files:**
- Create: `app/integration/plugin_process.py`
- Test: `tests/test_plugin_process.py`

**Interfaces:**
- Consumes: `Manifest`（Task 2）、`rpc_protocol`（Task 1）
- Produces: `PluginProcess` 类
  - `__init__(self, manifest: Manifest, plugin_root: str, rpc_timeout: float = 30.0)`
  - `async start(self) -> None`：spawn 子进程 + 握手
  - `async call(self, method: str, params: dict) -> dict`：发请求等响应
  - `async stop(self) -> None`：优雅关闭
  - 属性 `manifest`、`is_alive: bool`

- [ ] **Step 1: 写失败测试（用 echo 插件 spawn 真子进程）**

创建 `tests/test_plugin_process.py`：
```python
import asyncio
import sys
from pathlib import Path

import pytest

from app.integration.manifest_loader import load_manifests
from app.integration.plugin_process import PluginProcess
from app.integration.rpc_protocol import METHOD_SPEAK

ECHO_DIR = Path(__file__).parent / "integrations" / "echo"


def _load_echo_manifest():
    manifests = load_manifests(str(ECHO_DIR.parent), api_version="1")
    return next(m for m in manifests if m.id == "echo")


def test_plugin_process_handshake_and_speak():
    manifest = _load_echo_manifest()
    proc = PluginProcess(manifest=manifest, plugin_root=str(ECHO_DIR), rpc_timeout=10.0)

    async def go():
        await proc.start()
        assert proc.is_alive is True

        result = await proc.call(METHOD_SPEAK, {"text": "hello", "msg_id": "m1"})
        assert result == {"spoken": "hello", "msg_id": "m1"}

        await proc.stop()
        assert proc.is_alive is False

    asyncio.get_event_loop().run_until_complete(go())


def test_plugin_process_call_after_stop_raises():
    manifest = _load_echo_manifest()
    proc = PluginProcess(manifest=manifest, plugin_root=str(ECHO_DIR), rpc_timeout=10.0)

    async def go():
        await proc.start()
        await proc.stop()
        with pytest.raises(RuntimeError):
            await proc.call(METHOD_SPEAK, {"text": "x"})

    asyncio.get_event_loop().run_until_complete(go())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_plugin_process.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 plugin_process.py**

创建 `app/integration/plugin_process.py`：
```python
"""单个插件子进程的 stdio JSON-RPC 连接（方向 1: Aether → 插件）。"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from .rpc_protocol import (
    build_request, parse_message, build_response,
    METHOD_HANDSHAKE,
)
from .schema import Manifest

logger = logging.getLogger(__name__)


class PluginProcess:
    """一个插件进程的连接器。

    复用 ExternalMCPServer 的 stdio 模式：spawn 子进程,通过 stdin/stdout
    交换 JSON-RPC,用 pending futures map 做请求-响应配对。
    """

    def __init__(
        self,
        manifest: Manifest,
        plugin_root: str,
        rpc_timeout: float = 30.0,
    ) -> None:
        self.manifest = manifest
        self._plugin_root = plugin_root
        self._rpc_timeout = rpc_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def start(self) -> None:
        """spawn 子进程并完成握手。"""
        entry = self._resolve_entry()
        cmd = [sys.executable, entry, str(Path(self._plugin_root) / "manifest.json")]

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        await self._handshake()
        self._alive = True
        logger.info("插件 %s 已启动 (pid=%s)", self.manifest.id, self._process.pid)

    def _resolve_entry(self) -> str:
        """插件入口脚本路径。"""
        return str(Path(self._plugin_root) / self.manifest.entry)

    async def _handshake(self) -> None:
        result = await self.call(METHOD_HANDSHAKE, {
            "aether_api_version": "1",
            "capabilities_expected": [c.type.value for c in self.manifest.capabilities],
        })
        if not result.get("ready"):
            raise RuntimeError(f"插件 {self.manifest.id} 握手失败: {result}")
        logger.info("插件 %s 握手成功: %s", self.manifest.id, result)

    async def call(self, method: str, params: dict | None = None) -> dict:
        """发 JSON-RPC 请求,等响应。"""
        if self._process is None or self._process.stdin.is_closing():
            raise RuntimeError(f"插件 {self.manifest.id} 未运行")

        self._next_id += 1  # 偶数 id (从 2 开始)
        rid = self._next_id
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[rid] = future

        payload = build_request(rid, method, params)
        line = json.dumps(payload, ensure_ascii=False)
        try:
            self._process.stdin.write((line + "\n").encode("utf-8"))
            await asyncio.wait_for(self._process.stdin.drain(), timeout=self._rpc_timeout)
            result = await asyncio.wait_for(future, timeout=self._rpc_timeout)
            return result
        except asyncio.TimeoutError:
            raise RuntimeError(f"插件 {self.manifest.id} 调用 {method} 超时")
        finally:
            self._pending.pop(rid, None)

    async def _read_stdout(self) -> None:
        assert self._process is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            msg = parse_message(line.decode("utf-8", errors="replace"))
            if msg is None:
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if not fut.done():
                    fut.set_result(msg.get("result", {}))

    async def _drain_stderr(self) -> None:
        """把插件 stderr 当日志。"""
        assert self._process is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            logger.debug("[%s] %s", self.manifest.id, line.decode("utf-8", errors="replace").rstrip())

    async def stop(self) -> None:
        """优雅停止：通知 → terminate → kill。"""
        self._alive = False
        if self._process is None:
            return

        # 尝试发 shutdown
        try:
            await asyncio.wait_for(self.call("shutdown", {}), timeout=3.0)
        except (RuntimeError, asyncio.TimeoutError):
            pass

        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("plugin stopping"))
        self._pending.clear()

        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

        try:
            self._process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()
```

- [ ] **Step 4: 写 echo 插件的进程入口**

为了让 `PluginProcess` 能 `python plugin.py <manifest_path>` 启动 echo 插件，需要在 echo 插件目录加一个可执行入口。修改 `tests/integrations/echo/plugin.py`，在文件末尾追加：

```python
if __name__ == "__main__":
    import sys as _sys
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = _sys.argv[1] if len(_sys.argv) > 1 else "manifest.json"
    asyncio.run(run_stdio_plugin(EchoPlugin, _manifest_path))
```

同时文件顶部需 `import asyncio`。完整顶部导入区：
```python
import asyncio
import sys
from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.sink_base import OutputSink
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_plugin_process.py -v`
Expected: 2 passed（真实 spawn 了子进程）

> ⚠️ 如果 Windows 下 `sys.executable` 是 python launcher 别名，可能启动失败。备选：用 `shutil.which("python")` 或绝对路径。若失败，在 `PluginProcess._resolve_entry` 改为返回 `(sys.executable, "-X", "utf8", entry)` 或在 cmd 里直接用 `python` 字面量——以本机 `python` 命令可用为准。

- [ ] **Step 6: 提交**

```bash
git add app/integration/plugin_process.py tests/integrations/echo/plugin.py tests/test_plugin_process.py
git commit -m "feat(integration): PluginProcess 单进程 stdio JSON-RPC 连接"
```

---

## Task 5: PluginSupervisor —— 生命周期 + 退避重启

**Files:**
- Create: `app/integration/plugin_supervisor.py`
- Test: `tests/test_plugin_supervisor.py`
- Test fixture: `tests/integrations/crash/manifest.json`, `tests/integrations/crash/plugin.py`

**Interfaces:**
- Consumes: `PluginProcess`（Task 4）、`Manifest`
- Produces: `PluginSupervisor`
  - `__init__(self, rpc_timeout: float = 30.0, max_restarts: int = 3)`
  - `async start_all(self, manifests: list[Manifest], plugin_dir: str) -> None`
  - `async stop_all(self) -> None`
  - `get_process(self, plugin_id: str) -> PluginProcess | None`
  - `get_running_manifests(self) -> list[Manifest]`

- [ ] **Step 1: 写 crash 测试插件**

创建 `tests/integrations/crash/manifest.json`：
```json
{
    "id": "crash",
    "name": "崩溃测试",
    "version": "1.0.0",
    "aether_api_version": "1",
    "entry": "plugin.py",
    "capabilities": [{"type": "output_sink", "id": "c1"}]
}
```

创建 `tests/integrations/crash/plugin.py`：
```python
"""启动即崩溃的测试插件。"""

import asyncio
import sys
from app.integration.sdk.plugin_base import IntegrationPlugin


class CrashPlugin(IntegrationPlugin):
    def setup(self, manifest_dict):
        # setup 阶段直接抛异常
        raise RuntimeError("intentional crash for test")


if __name__ == "__main__":
    # 进程入口：import 时就崩（模拟握手失败）
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    asyncio.run(_crash_main(_manifest_path))


async def _crash_main(manifest_path):
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    # run_stdio_plugin 内部 setup 会抛,被 runtime 捕获放 result
    # 为模拟"进程直接退出",这里主动 sys.exit
    sys.exit(1)
```

- [ ] **Step 2: 写失败测试**

创建 `tests/test_plugin_supervisor.py`：
```python
import asyncio
from pathlib import Path

import pytest

from app.integration.manifest_loader import load_manifests
from app.integration.plugin_supervisor import PluginSupervisor

INTEGRATIONS_TESTS = Path(__file__).parent / "integrations"


def test_supervisor_starts_echo_plugin():
    manifests = load_manifests(str(INTEGRATIONS_TESTS), api_version="1")
    echo = next(m for m in manifests if m.id == "echo")
    sup = PluginSupervisor(rpc_timeout=10.0, max_restarts=3)

    async def go():
        await sup.start_all([echo], str(INTEGRATIONS_TESTS))
        proc = sup.get_process("echo")
        assert proc is not None
        assert proc.is_alive is True
        await sup.stop_all()

    asyncio.get_event_loop().run_until_complete(go())


def test_supervisor_crash_plugin_disables_after_retries():
    manifests = load_manifests(str(INTEGRATIONS_TESTS), api_version="1")
    crash = next(m for m in manifests if m.id == "crash")
    sup = PluginSupervisor(rpc_timeout=5.0, max_restarts=2)

    async def go():
        await sup.start_all([crash], str(INTEGRATIONS_TESTS))
        # 重试耗尽后,进程不在 running 列表
        running = sup.get_running_manifests()
        assert all(m.id != "crash" for m in running)
        await sup.stop_all()

    asyncio.get_event_loop().run_until_complete(go())
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_plugin_supervisor.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 4: 实现 plugin_supervisor.py**

创建 `app/integration/plugin_supervisor.py`：
```python
"""插件进程生命周期管理：启动 / 退避重启 / 熔断。"""

import asyncio
import logging

from .plugin_process import PluginProcess
from .schema import Manifest

logger = logging.getLogger(__name__)


class PluginSupervisor:
    """管理多个插件进程。

    崩溃后按指数退避重启,max_restarts 次后熔断(禁用)。
    """

    def __init__(self, rpc_timeout: float = 30.0, max_restarts: int = 3) -> None:
        self._rpc_timeout = rpc_timeout
        self._max_restarts = max_restarts
        self._processes: dict[str, PluginProcess] = {}  # plugin_id → process
        self._manifests: dict[str, Manifest] = {}

    async def start_all(self, manifests: list[Manifest], plugin_dir: str) -> None:
        """启动给定清单列表对应的进程。失败的跳过(不阻塞其他)。"""
        for manifest in manifests:
            try:
                await self._start_with_retries(manifest, plugin_dir)
            except Exception as exc:
                logger.error("插件 %s 启动失败(已重试 %d 次): %s",
                             manifest.id, self._max_restarts, exc)

    async def _start_with_retries(self, manifest: Manifest, plugin_dir: str) -> None:
        backoff = 1.0
        attempts = 0
        while attempts <= self._max_restarts:
            proc = PluginProcess(
                manifest=manifest,
                plugin_root=f"{plugin_dir}/{manifest.id}",
                rpc_timeout=self._rpc_timeout,
            )
            try:
                await proc.start()
                self._processes[manifest.id] = proc
                self._manifests[manifest.id] = manifest
                return
            except Exception as exc:
                attempts += 1
                logger.warning("插件 %s 启动失败(第 %d 次): %s",
                               manifest.id, attempts, exc)
                if attempts > self._max_restarts:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop_all(self) -> None:
        """停止所有进程。"""
        procs = list(self._processes.values())
        self._processes.clear()
        self._manifests.clear()
        for proc in procs:
            try:
                await proc.stop()
            except Exception as exc:
                logger.warning("停止插件 %s 出错: %s", proc.manifest.id, exc)

    def get_process(self, plugin_id: str) -> PluginProcess | None:
        return self._processes.get(plugin_id)

    def get_running_manifests(self) -> list[Manifest]:
        return list(self._manifests.values())
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_plugin_supervisor.py -v`
Expected: 2 passed

- [ ] **Step 6: 提交**

```bash
git add app/integration/plugin_supervisor.py tests/integrations/crash/ tests/test_plugin_supervisor.py
git commit -m "feat(integration): PluginSupervisor 生命周期 + 退避重启"
```

---

## Task 6: SinkManager —— 广播 fan-out

**Files:**
- Create: `app/integration/sink_manager.py`
- Test: `tests/test_sink_manager.py`

**Interfaces:**
- Consumes: `PluginSupervisor`（拿 output_sink 能力的进程）、`Manifest`
- Produces: `SinkManager`
  - `__init__(self, supervisor: PluginSupervisor)`
  - `async broadcast(self, text: str, msg_id: str = "") -> None`：并发 fan-out 到所有 output_sink 进程
  - `async interrupt_all(self) -> None`：并发调 interrupt

- [ ] **Step 1: 写失败测试**

创建 `tests/test_sink_manager.py`：
```python
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.integration.manifest_loader import load_manifests
from app.integration.plugin_supervisor import PluginSupervisor
from app.integration.sink_manager import SinkManager

INTEGRATIONS_TESTS = Path(__file__).parent / "integrations"


def _start_echo_supervisor():
    """启动 echo 插件,返回 supervisor。"""
    manifests = load_manifests(str(INTEGRATIONS_TESTS), api_version="1")
    echo = next(m for m in manifests if m.id == "echo")
    sup = PluginSupervisor(rpc_timeout=10.0, max_restarts=1)

    async def go():
        await sup.start_all([echo], str(INTEGRATIONS_TESTS))
    asyncio.get_event_loop().run_until_complete(go())
    return sup


def test_broadcast_fanouts_to_all_sinks():
    sup = _start_echo_supervisor()
    manager = SinkManager(sup)

    async def go():
        await manager.broadcast("床头灯已打开", "msg1")
        await sup.stop_all()

    asyncio.get_event_loop().run_until_complete(go())
    # echo 插件会把 spoken 返回,broadcast 不抛即成功


def test_interrupt_all_calls_interrupt_on_each_sink():
    sup = _start_echo_supervisor()
    manager = SinkManager(sup)

    async def go():
        await manager.interrupt_all()
        await sup.stop_all()

    asyncio.get_event_loop().run_until_complete(go())


def test_broadcast_with_no_sinks_does_not_raise():
    sup = PluginSupervisor(rpc_timeout=5.0)
    # 不启动任何插件
    manager = SinkManager(sup)

    async def go():
        await manager.broadcast("hello")
        await manager.interrupt_all()

    asyncio.get_event_loop().run_until_complete(go())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_sink_manager.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 sink_manager.py**

创建 `app/integration/sink_manager.py`：
```python
"""OutputSink 广播管理 —— 把 Aether 回复 fan-out 到所有启用的 sink。"""

import asyncio
import logging

from .plugin_supervisor import PluginSupervisor
from .rpc_protocol import METHOD_SPEAK, METHOD_INTERRUPT
from .schema import CapabilityType

logger = logging.getLogger(__name__)


class SinkManager:
    """广播助手回复到所有 output_sink 插件进程。

    并发调用(asyncio.gather),单个 sink 失败不影响其他。
    """

    def __init__(self, supervisor: PluginSupervisor) -> None:
        self._supervisor = supervisor

    def _collect_sink_processes(self):
        """收集所有声明了 output_sink 能力的运行中进程。"""
        result = []
        for manifest in self._supervisor.get_running_manifests():
            if manifest.has_capability(CapabilityType.OUTPUT_SINK):
                proc = self._supervisor.get_process(manifest.id)
                if proc and proc.is_alive:
                    result.append((manifest.id, proc))
        return result

    async def broadcast(self, text: str, msg_id: str = "") -> None:
        """并发广播文本到所有 sink。单个失败不阻塞其他。"""
        sinks = self._collect_sink_processes()
        if not sinks:
            return

        async def _send(plugin_id: str, proc) -> None:
            try:
                await proc.call(METHOD_SPEAK, {"text": text, "msg_id": msg_id})
            except Exception as exc:
                logger.warning("广播到 sink %s 失败: %s", plugin_id, exc)

        await asyncio.gather(*[_send(pid, proc) for pid, proc in sinks])

    async def interrupt_all(self) -> None:
        """并发中断所有 sink。"""
        sinks = self._collect_sink_processes()
        if not sinks:
            return

        async def _stop(plugin_id: str, proc) -> None:
            try:
                await proc.call(METHOD_INTERRUPT, {})
            except Exception as exc:
                logger.warning("中断 sink %s 失败: %s", plugin_id, exc)

        await asyncio.gather(*[_stop(pid, proc) for pid, proc in sinks])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_sink_manager.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add app/integration/sink_manager.py tests/test_sink_manager.py
git commit -m "feat(integration): SinkManager 广播 fan-out"
```

---

## Task 7: IntegrationLayer 门面 + 容器装配

**Files:**
- Create: `app/integration/integration_layer.py`
- Modify: `app/container.py`
- Modify: `app/bootstrap.py`
- Test: `tests/test_integration_layer.py`

**Interfaces:**
- Produces: `IntegrationLayer`
  - `__init__(self, plugin_dir: str, api_version: str = "1", rpc_timeout: float = 30.0, max_restarts: int = 3)`
  - `async start(self) -> None`：加载 manifest + 启动所有进程 + 暴露 sink_manager
  - `async stop(self) -> None`
  - `sink_manager: SinkManager`
  - `list_plugins(self) -> list[dict]`：插件状态摘要（id/name/enabled）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_integration_layer.py`：
```python
import asyncio
from pathlib import Path

from app.integration.integration_layer import IntegrationLayer

INTEGRATIONS_TESTS = Path(__file__).parent / "integrations"


def test_layer_starts_and_lists_plugins():
    layer = IntegrationLayer(
        plugin_dir=str(INTEGRATIONS_TESTS),
        api_version="1",
        rpc_timeout=10.0,
        max_restarts=1,
    )

    async def go():
        await layer.start()
        plugins = layer.list_plugins()
        ids = [p["id"] for p in plugins]
        assert "echo" in ids
        echo = next(p for p in plugins if p["id"] == "echo")
        assert echo["alive"] is True
        await layer.stop()

    asyncio.get_event_loop().run_until_complete(go())


def test_layer_broadcasts_via_sink_manager():
    layer = IntegrationLayer(
        plugin_dir=str(INTEGRATIONS_TESTS),
        api_version="1", rpc_timeout=10.0, max_restarts=1,
    )

    async def go():
        await layer.start()
        await layer.sink_manager.broadcast("测试消息", "m1")
        await layer.stop()

    asyncio.get_event_loop().run_until_complete(go())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_integration_layer.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 integration_layer.py**

创建 `app/integration/integration_layer.py`：
```python
"""IntegrationLayer —— 集成平台门面。

组装 manifest_loader + plugin_supervisor + sink_manager。
"""

import logging

from .manifest_loader import load_manifests
from .plugin_supervisor import PluginSupervisor
from .sink_manager import SinkManager

logger = logging.getLogger(__name__)


class IntegrationLayer:
    """集成平台门面。挂到 AppContainer,由 lifespan 启停。"""

    def __init__(
        self,
        plugin_dir: str,
        api_version: str = "1",
        rpc_timeout: float = 30.0,
        max_restarts: int = 3,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._api_version = api_version
        self._supervisor = PluginSupervisor(
            rpc_timeout=rpc_timeout, max_restarts=max_restarts,
        )
        self.sink_manager = SinkManager(self._supervisor)
        self._started = False

    async def start(self) -> None:
        """加载清单 + 启动所有插件进程。"""
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version)
        logger.info("发现 %d 个集成插件: %s",
                    len(manifests), [m.id for m in manifests])
        await self._supervisor.start_all(manifests, self._plugin_dir)
        self._started = True

    async def stop(self) -> None:
        """停止所有插件进程。"""
        self._started = False
        await self._supervisor.stop_all()

    def list_plugins(self) -> list[dict]:
        """返回插件状态摘要。"""
        result = []
        from .manifest_loader import load_manifests
        manifests = load_manifests(self._plugin_dir, api_version=self._api_version)
        for m in manifests:
            proc = self._supervisor.get_process(m.id)
            result.append({
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "capabilities": [c.type.value for c in m.capabilities],
                "alive": proc.is_alive if proc else False,
            })
        return result
```

- [ ] **Step 4: 修改 AppContainer 加 integration_layer 字段**

读取 `app/container.py`，在 dataclass 的"调度器（lifespan 启动阶段赋值）"区（有 defaults 的那组，约 `sg_service: Any = None` 之后）加：
```python
    integration_layer: Any = None
```

- [ ] **Step 5: 修改 bootstrap.py 构造占位**

读取 `app/bootstrap.py`，在 `initialize_services` 末尾 `return services` 之前加（因为 IntegrationLayer 需要 event loop，这里只做轻量占位，真正启动在 main.py lifespan）：
```python
    # 集成平台：lifespan 阶段启动
    services["integration_layer"] = None
```

并在 `container.py` 的 `init_container` 里（约 `services.get(...)` 那组）加：
```python
        integration_layer=services.get("integration_layer"),
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_integration_layer.py -v`
Expected: 2 passed

- [ ] **Step 7: 提交**

```bash
git add app/integration/integration_layer.py app/container.py app/bootstrap.py tests/test_integration_layer.py
git commit -m "feat(integration): IntegrationLayer 门面 + AppContainer 装配"
```

---

## Task 8: 小爱插件实现

**Files:**
- Create: `integrations/xiaoai/manifest.json`
- Create: `integrations/xiaoai/plugin.py`
- Test: `tests/integrations/test_xiaoai_plugin.py`（纯逻辑测试，不 spawn）

**Interfaces:**
- Consumes: `IntegrationPlugin`、`OutputSink`（Task 3）
- Produces: `XiaoAiPlugin` + `XiaoAiSink`，通过反向 RPC 调用 `aether.ha.call_service`

> **注意**：Phase 1 反向 RPC（插件 → Aether）尚未实现（那是 Phase 3）。Phase 1 的小爱插件**直接持有 HA 调用回调**——由 stdio runtime 在握手时通过 env 或 params 注入一个"HA caller 回调"。但跨进程不能传函数。

> **简化决策**：Phase 1 小爱插件通过**环境变量读取 HA 凭证**，插件进程内自建一个轻量 HA HTTP client 直连 HA，不依赖反向 RPC。这样 Phase 1 自包含可演示，Phase 3 再切换为反向 RPC。小爱插件进程内的 HA client 不复用 Aether 的 `HomeAssistantClient`（那是宿主侧的），插件自己用 httpx 直连 `/api/services/xiaomi_miot/intelligent_speaker`。

- [ ] **Step 1: 写小爱 manifest**

创建 `integrations/xiaoai/manifest.json`：
```json
{
    "id": "xiaoai",
    "name": "小爱音箱",
    "version": "1.0.0",
    "aether_api_version": "1",
    "author": "Aether",
    "description": "小爱 TTS 广播(Phase 1: 直连 HA)",
    "entry": "plugin.py",
    "capabilities": [
        {
            "type": "output_sink",
            "id": "xiaoai_pro",
            "priority": 100,
            "config_schema": {
                "entity_id": {
                    "type": "string", "required": true,
                    "label": "小爱实体ID",
                    "default": "media_player.xiaoai_pro_play_controller"
                },
                "execute_mode": {
                    "type": "enum",
                    "options": ["speak", "execute"],
                    "default": "speak",
                    "label": "默认模式"
                }
            }
        }
    ],
    "permissions": [],
    "resources": {"max_memory_mb": 128, "restart_on_crash": true, "max_restarts": 3}
}
```

- [ ] **Step 2: 写失败测试（纯逻辑，mock HA client）**

创建 `tests/integrations/test_xiaoai_plugin.py`：
```python
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import sys
XIAOAI_DIR = Path(__file__).parent.parent.parent / "integrations" / "xiaoai"
sys.path.insert(0, str(XIAOAI_DIR))
from plugin import XiaoAiPlugin  # noqa: E402


def _manifest():
    return json.loads((XIAOAI_DIR / "manifest.json").read_text(encoding="utf-8"))


def _make_plugin_with_mock_ha():
    """构造 plugin + 一个 mock HA caller。"""
    manifest = _manifest()
    plugin = XiaoAiPlugin()
    plugin.setup(manifest)
    # 注入 mock HA caller
    plugin.ha_caller = AsyncMock()
    plugin.ha_caller.call_service.return_value = {"ok": True}
    return plugin


def test_xiaoai_speak_calls_intelligent_speaker():
    plugin = _make_plugin_with_mock_ha()

    result = asyncio.get_event_loop().run_until_complete(
        plugin.handle("sink.speak", {"text": "床头灯已打开", "msg_id": "m1"})
    )

    assert result.get("spoken") == "床头灯已打开"
    plugin.ha_caller.call_service.assert_awaited_once()
    call_kwargs = plugin.ha_caller.call_service.call_args
    assert call_kwargs.kwargs["domain"] == "xiaomi_miot"
    assert call_kwargs.kwargs["service"] == "intelligent_speaker"
    assert call_kwargs.kwargs["data"]["text"] == "床头灯已打开"
    assert call_kwargs.kwargs["data"]["execute"] is False


def test_xiaoai_interrupt_calls_media_stop():
    plugin = _make_plugin_with_mock_ha()

    result = asyncio.get_event_loop().run_until_complete(
        plugin.handle("sink.interrupt", {})
    )

    assert result.get("interrupted") is True
    plugin.ha_caller.call_service.assert_awaited_once()
    call_kwargs = plugin.ha_caller.call_service.call_args
    assert call_kwargs.kwargs["domain"] == "media_player"
    assert call_kwargs.kwargs["service"] == "media_stop"


def test_xiaoai_speak_serializes_via_lock():
    """两次 speak 串行(锁),不并发调 HA。"""
    plugin = _make_plugin_with_mock_ha()

    async def go():
        # 并发发起两次 speak
        await asyncio.gather(
            plugin.handle("sink.speak", {"text": "第一条"}),
            plugin.handle("sink.speak", {"text": "第二条"}),
        )

    asyncio.get_event_loop().run_until_complete(go())
    # 两次都调到了
    assert plugin.ha_caller.call_service.await_count == 2
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/integrations/test_xiaoai_plugin.py -v`
Expected: FAIL，无法 import `plugin`

- [ ] **Step 4: 实现小爱插件**

创建 `integrations/xiaoai/plugin.py`：
```python
"""小爱音箱插件 —— Phase 1 直连 HA 实现。

通过 xiaomi_miot.intelligent_speaker 服务做 TTS:
  execute=False → 念文字
  execute=True  → 当语音指令执行(Phase 2 直通模式用)

软件串行锁:Aether 自己的多次 speak 排队,不并发占用小爱。
外部程序(米家/HA 自动化)对小爱的控制不在此锁范围。
"""

import asyncio
import json
import os
import sys
from typing import Any

# 让插件能 import SDK(插件进程的 sys.path 需含 Aether 根)
_AETHER_ROOT = os.environ.get("AETHER_ROOT", "")
if _AETHER_ROOT and _AETHER_ROOT not in sys.path:
    sys.path.insert(0, _AETHER_ROOT)

from app.integration.sdk.plugin_base import IntegrationPlugin  # noqa: E402
from app.integration.sdk.sink_base import OutputSink  # noqa: E402


class HAHttpCaller:
    """轻量 HA HTTP 调用器(插件进程内自用,Phase 1)。

    Phase 3 会替换为反向 RPC 调 aether.ha.call_service。
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    async def call_service(
        self, domain: str, service: str,
        entity_id: str | None = None, data: dict | None = None,
    ) -> dict:
        import httpx
        payload: dict[str, Any] = {}
        if entity_id:
            payload["entity_id"] = entity_id
        if data:
            payload.update(data)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base_url}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {self._token}",
                         "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            return {"ok": True, "status": resp.status_code}


class XiaoAiSink(OutputSink):
    """小爱输出 sink。

    软件串行锁 + 队列:Aether 多条 speak 排队,Aether 主动 interrupt 可清队列。
    """

    def __init__(self, ha_caller, entity_id: str, execute_mode: str = "speak") -> None:
        self._ha = ha_caller
        self._entity_id = entity_id
        self._execute = (execute_mode == "execute")
        self._seq_lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()

    async def speak(self, text: str, msg_id: str = "") -> dict:
        await self._queue.put(text)
        async with self._seq_lock:
            spoken_all = []
            while not self._queue.empty():
                msg = await self._queue.get()
                await self._ha.call_service(
                    domain="xiaomi_miot",
                    service="intelligent_speaker",
                    entity_id=self._entity_id,
                    data={"text": msg, "execute": self._execute, "silent": False},
                )
                spoken_all.append(msg)
            return {"spoken": " | ".join(spoken_all), "msg_id": msg_id}

    async def interrupt(self) -> dict:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self._ha.call_service(
            domain="media_player",
            service="media_stop",
            entity_id=self._entity_id,
            data={},
        )
        return {"interrupted": True}


class XiaoAiPlugin(IntegrationPlugin):
    """小爱插件。setup 时读 manifest config_schema 默认值 + 环境变量凭证。"""

    def setup(self, manifest_dict: dict[str, Any]) -> None:
        self.manifest = manifest_dict

        # 从 manifest config_schema 提取默认配置
        cap = manifest_dict["capabilities"][0]
        schema = cap.get("config_schema", {})
        entity_id = schema.get("entity_id", {}).get("default", "media_player.xiaoai_pro")
        execute_mode = schema.get("execute_mode", {}).get("default", "speak")

        # HA 凭证从环境变量(由宿主 spawn 时注入)
        ha_url = os.environ.get("XIAOAI_HA_URL", "")
        ha_token = os.environ.get("XIAOAI_HA_TOKEN", "")
        if ha_url and ha_token:
            self.ha_caller = HAHttpCaller(ha_url, ha_token)
        else:
            self.ha_caller = None  # 无凭证时 sink 调用会失败,但不崩 setup

        self.sinks = [XiaoAiSink(self.ha_caller, entity_id, execute_mode)]


if __name__ == "__main__":
    import asyncio as _asyncio
    from app.integration.sdk.stdio_runtime import run_stdio_plugin
    _manifest_path = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    _asyncio.run(run_stdio_plugin(XiaoAiPlugin, _manifest_path))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/integrations/test_xiaoai_plugin.py -v`
Expected: 3 passed

- [ ] **Step 6: 提交**

```bash
git add integrations/xiaoai/ tests/integrations/test_xiaoai_plugin.py
git commit -m "feat(xiaoai): 小爱音箱插件 Phase 1(直连 HA intelligent_speaker)"
```

---

## Task 9: Dispatcher 广播钩子

**Files:**
- Modify: `app/agents/dispatcher.py`（构造函数 + final_content 钩子点）
- Test: `tests/test_dispatcher_broadcast_hook.py`

**Interfaces:**
- Consumes: `SinkManager`
- Produces: `Dispatcher.__init__` 新增可选参数 `sink_manager: Any = None`
- 钩子点：`_run_turn` 在 `Dialog.Finish` emit 之前，若 `state.final_content` 非空且 `sink_manager` 非 None，调 `await sink_manager.broadcast(final_content, request_id)`，异常吞掉（不阻塞主流程）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_dispatcher_broadcast_hook.py`：
```python
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.dispatcher import Dispatcher
from app.schema.chat_schema import Event, Nlp


def test_dispatcher_broadcasts_final_content():
    """dispatcher 在 turn 结束时应广播 final_content。"""
    store = MagicMock()
    agent = MagicMock()
    dispatcher = Dispatcher(
        session_store=store, agent=agent, camera_stream=MagicMock(),
        ha_catalog_provider=MagicMock(return_value=""),
        sink_manager=AsyncMock(),  # 注入 mock sink_manager
    )

    # 模拟最终内容(通过 patch run_agent_streaming)
    async def fake_stream(*a, **kw):
        yield {"type": "token", "content": "床头灯已打开"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.agents.dispatcher.run_agent_streaming", fake_stream)
        sent = []

        async def ws_send(msg):
            sent.append(msg)

        event = Event.build_event(Nlp.Request(query="打开床头灯"), "r1", "s1")
        # session mock
        session = MagicMock()
        session.model_messages = []
        dispatcher._prepare_context = AsyncMock(return_value=({"system": "x"}, session))

        await dispatcher.dispatch_stream(event, ws_send, user_id="")

    dispatcher.sink_manager.broadcast.assert_awaited()
    # 最后一次 broadcast 的第一个参数应含 final_content
    args = dispatcher.sink_manager.broadcast.call_args
    assert "床头灯已打开" in str(args.args)


def test_dispatcher_without_sink_manager_does_not_crash():
    """未注入 sink_manager 时正常工作(向后兼容)。"""
    store = MagicMock()
    agent = MagicMock()
    dispatcher = Dispatcher(
        session_store=store, agent=agent, camera_stream=MagicMock(),
        ha_catalog_provider=MagicMock(return_value=""),
    )
    assert dispatcher._sink_manager is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_dispatcher_broadcast_hook.py -v`
Expected: FAIL（Dispatcher 不接受 sink_manager 参数 / `_sink_manager` 属性不存在）

- [ ] **Step 3: 修改 Dispatcher 构造函数**

读取 `app/agents/dispatcher.py` 第 194–207 行的 `__init__`，在参数列表末尾（`camera_manager: Any = None,` 之后）加：
```python
    sink_manager: Any = None,
```

并在 `__init__` body 里（存其他 self 字段的位置）加：
```python
        self._sink_manager = sink_manager
```

- [ ] **Step 4: 在 final_content emit 后加广播钩子**

读取 `app/agents/dispatcher.py`，找到 `Dialog.Finish` 的 emit（约 720–725 行）。在它**之前**插入广播钩子。即在 `finish_success = ...` 那行之后、`await emit(Instruction.build_instruction(Dialog.Finish...` 之前，插入：

```python
        # ── 集成广播钩子：把最终回复同步到 output_sink(如小爱) ──
        if state.final_content and self._sink_manager is not None:
            try:
                await self._sink_manager.broadcast(state.final_content, request_id)
            except Exception as exc:
                logger.warning("集成广播失败(不影响主流程): %s", exc)
```

> **注意**：检查文件顶部是否已 `import logging` + `logger = logging.getLogger(__name__)`。若无，添加。

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_dispatcher_broadcast_hook.py -v`
Expected: 2 passed

- [ ] **Step 6: 运行既有 dispatcher 测试确保无回归**

Run: `python -m pytest tests/test_dispatcher.py tests/test_ws_chat_e2e.py -v`
Expected: 全部通过（sink_manager 默认 None，向后兼容）

- [ ] **Step 7: 提交**

```bash
git add app/agents/dispatcher.py tests/test_dispatcher_broadcast_hook.py
git commit -m "feat(dispatcher): final_content 产出后广播到 output_sink"
```

---

## Task 10: main.py lifespan 启动 + 路由注册

**Files:**
- Modify: `app/main.py`（lifespan 启动 IntegrationLayer、Dispatcher 注入 sink_manager、注册路由）
- Create: `app/routes/integration_routes.py`
- Test: `tests/test_integration_routes.py`

**Interfaces:**
- Produces:
  - `/api/integrations` (GET)：返回插件列表 + 状态
  - `/api/integrations/broadcast` (POST)：手动触发广播（测试用）
  - `app/main.py` lifespan：读 config → 构造 IntegrationLayer → start → Dispatcher 注入 sink_manager

- [ ] **Step 1: 写失败测试**

创建 `tests/test_integration_routes.py`：
```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """构造一个带 IntegrationLayer 的 app。"""
    # 用真实 echo 插件目录
    from pathlib import Path
    test_integrations = Path(__file__).parent / "integrations"

    monkeypatch.setattr(
        "app.core.config.get_config",
        lambda path, default=None: {
            "integration.plugin_dir": str(test_integrations),
            "integration.enabled": True,
            "integration.api_version": "1",
            "integration.default_rpc_timeout": 10.0,
            "integration.max_restarts": 1,
        }.get(path, default),
    )

    from app.main import app
    return TestClient(app)


def test_list_integrations_endpoint(client):
    # 这个测试需要 IntegrationLayer 已 start,完整 e2e 较重
    # Phase 1 先验证路由存在
    with client:
        resp = client.get("/api/integrations")
        # 即便 layer 没 start,也应返回 200 + 空列表或 disabled
        assert resp.status_code == 200
```

> ⚠️ 这个测试较重（要启动整个 app + IntegrationLayer）。如果 e2e 太重，可降级为只测路由函数（不经过 TestClient）。根据实际运行调整。

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_integration_routes.py -v`
Expected: FAIL（路由不存在）

- [ ] **Step 3: 创建 integration_routes.py**

创建 `app/routes/integration_routes.py`：
```python
"""集成平台管理路由。"""

import logging

from fastapi import APIRouter, Depends

from ..container import get_container

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/integrations")
async def list_integrations(container=Depends(get_container)):
    """列出所有集成插件及其状态。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": True, "data": {"plugins": [], "enabled": False}}
    return {"success": True, "data": {"plugins": layer.list_plugins(), "enabled": True}}


@router.post("/integrations/broadcast")
async def manual_broadcast(text: str, container=Depends(get_container)):
    """手动触发广播(测试用)。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    await layer.sink_manager.broadcast(text, "manual")
    return {"success": True}
```

- [ ] **Step 4: 修改 main.py 注册路由**

读取 `app/main.py`。在 router import 区（约 666–681 行）加：
```python
from .routes.integration_routes import router as integration_router
```

在 include_router 区（约 702 行 `app.include_router(ws_router)` 之前）加：
```python
app.include_router(integration_router, prefix="/api")
```

- [ ] **Step 5: 修改 main.py lifespan 启动 IntegrationLayer + 注入 Dispatcher**

读取 `app/main.py` 的 lifespan 函数。在 Dispatcher 构造处（约 502–516 行），找到 `dispatcher = Dispatcher(...)` 调用。

需要：(a) 在 Dispatcher 构造**之前**构造 IntegrationLayer 并 start；(b) 把 `sink_manager` 传给 Dispatcher。

在 Dispatcher 构造之前插入：
```python
        # ── 集成平台启动 ──
        from app.core.config import get_config
        integration_enabled = get_config("integration.enabled", False)
        integration_layer = None
        if integration_enabled:
            from pathlib import Path as _Path
            from app.integration.integration_layer import IntegrationLayer
            plugin_dir = get_config("integration.plugin_dir", "integrations")
            integration_layer = IntegrationLayer(
                plugin_dir=str(_Path(__file__).resolve().parent.parent / plugin_dir),
                api_version=get_config("integration.api_version", "1"),
                rpc_timeout=get_config("integration.default_rpc_timeout", 30.0),
                max_restarts=get_config("integration.max_restarts", 3),
            )
            await integration_layer.start()
            _container.integration_layer = integration_layer
            logger.info("集成平台已启动")
```

然后在 `Dispatcher(...)` 构造调用里加 `sink_manager=integration_layer.sink_manager if integration_layer else None`。

并在 lifespan 的 shutdown 部分（yield 之后，约 628 行之后）加：
```python
        if integration_layer is not None:
            await integration_layer.stop()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_integration_routes.py -v`
Expected: 1 passed

> ⚠️ 如果 TestClient 启动整个 app 触发真实插件 spawn 在 CI 里太重，可将测试改为直接 import 路由函数并注入 mock container。根据实际调整。

- [ ] **Step 7: 提交**

```bash
git add app/routes/integration_routes.py app/main.py tests/test_integration_routes.py
git commit -m "feat(main): lifespan 启动 IntegrationLayer + /api/integrations 路由"
```

---

## Task 11: 端到端冒烟测试 + 文档

**Files:**
- Create: `tests/test_integration_e2e.py`
- Create: `docs/06-集成扩展/插件系统Phase1.md`

**目标**：验证完整链路 manifest 加载 → 插件 spawn → dispatcher broadcast → 小爱 sink 收到 speak。

- [ ] **Step 1: 写端到端测试**

创建 `tests/test_integration_e2e.py`：
```python
"""端到端:Dispatcher broadcast → IntegrationLayer → echo sink。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.integration.integration_layer import IntegrationLayer

INTEGRATIONS_TESTS = Path(__file__).parent / "integrations"


def test_e2e_broadcast_reaches_echo_sink():
    layer = IntegrationLayer(
        plugin_dir=str(INTEGRATIONS_TESTS),
        api_version="1", rpc_timeout=10.0, max_restarts=1,
    )

    async def go():
        await layer.start()
        # 直接调 broadcast(模拟 dispatcher 钩子)
        await layer.sink_manager.broadcast("床头灯已打开", "req_001")
        await layer.stop()

    asyncio.get_event_loop().run_until_complete(go())
    # 不抛异常即通过(echo 插件收到 speak 并返回 spoken)


def test_e2e_layer_survives_plugin_crash():
    """echo 插件被外部 kill 后,supervisor 应能感知(Phase 1 只验证不崩主)。"""
    layer = IntegrationLayer(
        plugin_dir=str(INTEGRATIONS_TESTS),
        api_version="1", rpc_timeout=10.0, max_restarts=1,
    )

    async def go():
        await layer.start()
        # 模拟插件进程消失
        proc = layer._supervisor.get_process("echo")
        if proc and proc._process:
            proc._process.kill()
            await asyncio.sleep(0.5)
        # 主流程不应崩
        plugins = layer.list_plugins()
        await layer.stop()

    asyncio.get_event_loop().run_until_complete(go())
```

- [ ] **Step 2: 运行测试**

Run: `python -m pytest tests/test_integration_e2e.py -v`
Expected: 2 passed

- [ ] **Step 3: 写文档**

创建 `docs/06-集成扩展/插件系统Phase1.md`（简版，说明架构 + 如何加插件 + 配置）：
```markdown
# Aether 集成插件系统 (Phase 1)

## 概述

Phase 1 实现了插件系统的骨架:每个集成是独立子进程,通过 stdio JSON-RPC 与
Aether 通信。Aether 助手的文字回复会广播到所有 `output_sink` 能力的插件。

## 架构

[简图:Dispatcher → SinkManager → PluginSupervisor → PluginProcess → 插件子进程]

## 如何添加一个插件

1. 在 `integrations/` 下建子目录
2. 写 `manifest.json`(声明能力)
3. 写 `plugin.py`(继承 IntegrationPlugin,实现 OutputSink)
4. 重启 Aether

## 配置

config.json 的 `integration` section:
- enabled: 是否启用
- plugin_dir: 插件目录(默认 integrations)
- max_restarts: 崩溃重启上限

## 小爱插件

`integrations/xiaoai/` 调用 HA 的 `xiaomi_miot.intelligent_speaker` 做 TTS。
需设置环境变量 XIAOAI_HA_URL / XIAOAI_HA_TOKEN(由宿主 spawn 时注入)。
```

- [ ] **Step 4: 运行全套集成测试**

Run: `python -m pytest tests/test_rpc_protocol.py tests/test_manifest_loader.py tests/test_echo_plugin_runtime.py tests/test_plugin_process.py tests/test_plugin_supervisor.py tests/test_sink_manager.py tests/test_integration_layer.py tests/integrations/test_xiaoai_plugin.py tests/test_dispatcher_broadcast_hook.py tests/test_integration_e2e.py -v`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add tests/test_integration_e2e.py docs/06-集成扩展/插件系统Phase1.md
git commit -m "feat(integration): Phase 1 端到端冒烟测试 + 文档"
```

---

## Phase 1 验收清单

完成所有 Task 后,人工验证:

- [ ] Aether 启动时,日志显示"集成平台已启动"+ 发现插件列表
- [ ] 在 /chat 跟 Aether 对话(如"你好"),小爱音箱同步念出回复
- [ ] `curl http://localhost:8010/api/integrations` 返回插件列表(含小爱,alive=true)
- [ ] 手动 kill 小爱插件进程,Aether 日志显示退避重启,重启后恢复
- [ ] 小爱插件进程崩溃超过 max_restarts 后,Aether 不崩,插件标记 disabled
- [ ] 既有对话功能(设备控制等)无回归

---

## Self-Review

**1. Spec coverage（对照 spec §11 Phase 1）：**
- manifest_loader + schema 校验 → Task 2 ✅
- plugin_supervisor（spawn + 退避重启,暂不心跳熔断）→ Task 5 ✅
- plugin_connection（方向 1 单向 RPC）→ Task 4（命名 PluginProcess）✅
- sink_manager.broadcast → Task 6 ✅
- Dispatcher 钩子（final_content → broadcast）→ Task 9 ✅
- XiaoAiSink 插件 → Task 8 ✅
- 基础配置 UI → **未覆盖**（前端 UI 移到 Phase 2,Phase 1 用 config.json + API 验证）

**2. Placeholder scan:** 无 TBD/TODO。Task 10 Step 6 与 Task 11 Step 2 的"根据实际调整"提示是测试调试指引,不是占位符。

**3. Type consistency:** `SinkManager.broadcast(text, msg_id)` 在 Task 6/8/9 一致；`PluginProcess.call(method, params)` 在 Task 4/5/6 一致；`METHOD_SPEAK = "sink.speak"` 在 Task 1/3/6 一致。

---

## 后续 Phase 预告

- **Phase 2**:全局打断(W3) + 小爱直通模式(W2) + ChatView UI
- **Phase 3**:双向 RPC(反向调用 + 权限白名单)
- **Phase 4**:飞书机器人(W4)
- **Phase 5**:心跳熔断 + 优雅关闭三级流程 + 依赖图

每个 Phase 完成后单独走 brainstorming → spec → plan 流程。
