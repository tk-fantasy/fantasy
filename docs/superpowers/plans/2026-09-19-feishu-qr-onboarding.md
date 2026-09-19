# 飞书扫码一键接入 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在飞书插件配置弹窗内提供「飞书 App 扫码 → 自动创建机器人并接入」，凭证自动落库 + 热重连；全链路插件化解耦（删 `integrations/feishu/` 即整体下线，核心零残留）。

**Architecture:** 核心侧只加三个通用机制（宿主集成 UI 贡献、method RPC 桥接 + `config_changed` 热重启约定、未启动集成也注册），策略与状态全部在 `integrations/feishu/`（`qr_setup.py` 设备授权三步 + `call_method` 白名单 + 自带前端面板 `FeishuQrSetup.vue`，经 `PluginSlot` 的 `import.meta.glob` 动态加载进配置弹窗）。设计文档：`docs/superpowers/specs/2026-09-19-feishu-qr-onboarding-design.md`。

**Tech Stack:** FastAPI + httpx（已有）；Vue3 `<script setup>` + `qrcode` npm 包（已有）；pytest（`asyncio_mode = auto`）。

## Global Constraints

- **解耦红线：核心代码（`app/`）不得出现 "feishu"/"飞书"/"qr_start" 等插件策略字样**；只允许通用机制词汇（`call_method`、`ui_contributions`、`config_changed`）。插件策略全部住在 `integrations/feishu/`。
- 不新增任何 Python / npm 依赖（`httpx`、`qrcode` 均已在 requirements / package.json）。
- secret 不回传前端、不写日志；`device_code`/凭证只在内存，成功或取消即清。
- 测试风格沿用仓库现状：直接调路由函数 + mock container（`MagicMock`），不用 TestClient；`asyncio_mode=auto` 可直接写 `async def test_`。
- 提交信息：中文 conventional（`feat(feishu):` / `test:` / `docs:` 等），每任务至少一提交。
- 运行测试：`python -m pytest tests/<file> -v`（工作目录 `D:\Aether`）。

---

### Task 1: 核心机制 A——宿主集成 meta 支持 UI 贡献

**Files:**
- Modify: `app/main.py:1126-1153`（`_load_host_integration_meta`）
- Modify: `app/integration/integration_layer.py:350-370`（`list_ui_contributions`）
- Test: `tests/test_host_integration_extensions.py`（新建，本任务先写 UI 贡献部分）

**Interfaces:**
- Produces: `list_ui_contributions()` 返回条目形状 `{plugin_id, slot, type, props, state_key, action}`（宿主集成条目 props/state_key/action 为 None）；`_load_host_integration_meta` 返回 dict 新增 `"ui_contributions": [...]`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_host_integration_extensions.py`：

```python
"""宿主侧集成通用机制扩展测试：UI 贡献 + method 桥接（不硬编码插件语义）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.integration.integration_layer import IntegrationLayer


def _layer_with_host(tmp_path, host_info):
    """构造带指定宿主集成注册信息的 layer（plugin_dir 为空目录 → 无 manifest 干扰）。"""
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    layer.host_integrations["feishu"] = host_info
    return layer


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Task 1：UI 贡献 ──

def test_list_ui_contributions_includes_host_integrations(tmp_path):
    """宿主集成的 ui_contributions 并入返回（形状与 manifest 贡献一致）。"""
    layer = _layer_with_host(tmp_path, {
        "name": "飞书机器人",
        "ui_contributions": [{"slot": "plugin_config_modal", "type": "custom_component"}],
    })
    assert layer.list_ui_contributions() == [{
        "plugin_id": "feishu", "slot": "plugin_config_modal",
        "type": "custom_component", "props": None,
        "state_key": None, "action": None,
    }]


def test_list_ui_contributions_host_without_contributions(tmp_path):
    """未声明 ui_contributions 的宿主集成不产生条目。"""
    layer = _layer_with_host(tmp_path, {"name": "some_host"})
    assert layer.list_ui_contributions() == []
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_host_integration_extensions.py -v`
Expected: FAIL（`list_ui_contributions` 返回 `[]`，缺宿主集成条目）

- [ ] **Step 3: 实现**

`app/integration/integration_layer.py` 的 `list_ui_contributions`，在 `for manifest in manifests:` 循环之后、`return result` 之前追加：

```python
        # 宿主侧集成的 UI 贡献（meta.py UI_CONTRIBUTIONS 声明，形状同 manifest）
        for integ_id, info in self.host_integrations.items():
            for ui in info.get("ui_contributions") or []:
                if isinstance(ui, dict):
                    result.append({
                        "plugin_id": integ_id,
                        "slot": ui.get("slot"),
                        "type": ui.get("type"),
                        "props": ui.get("props"),
                        "state_key": ui.get("state_key"),
                        "action": ui.get("action"),
                    })
        return result
```

`app/main.py` 的 `_load_host_integration_meta`：`default_meta` 增加 `"ui_contributions": [],`；加载成功分支的 return dict 增加 `"ui_contributions": getattr(mod, "UI_CONTRIBUTIONS", []),`。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_host_integration_extensions.py tests/test_integration_routes.py -v`
Expected: 全部 PASS（含既有 ui_contributions 路由测试不回归）

- [ ] **Step 5: 提交**

```bash
git add app/main.py app/integration/integration_layer.py tests/test_host_integration_extensions.py
git commit -m "feat(integration): 宿主侧集成支持 meta 声明 UI 贡献"
```

---

### Task 2: 核心机制 B——method RPC 桥接 + config_changed 约定 + 未启动集成也注册

**Files:**
- Modify: `app/routes/integration_routes.py:274-300`（`call_plugin_method`）、文件顶部 import
- Modify: `app/main.py:1108-1121`（`_start_host_integrations` 注册段）、`app/main.py:1176-1182`（`_restart_host_integration` meta 段）
- Test: `tests/test_host_integration_extensions.py`（追加）

**Interfaces:**
- Produces: 宿主集成在 `layer.host_integrations[id]` 携带 `call_method`（async `(method: str, params: dict) -> dict`）；返回 dict 顶层 `success is False` 原样透传；顶层 `config_changed: True` 时宿主调 `container.restart_host_integration_fn(plugin_id, loop)` 并在响应附 `applied: "restarted"|"not_found"|"saved"`；否则包 `{"success": True, "data": result}` 信封。
- 语义修正：`_start_host_integrations` 对 `start()` 返回 None（如凭证未配置）的集成也收录 `started` 并注册（`alive=False`）——扫码面板必须在未配置时可达，restart 才能按新配置拉起。

- [ ] **Step 1: 写失败测试**

在 `tests/test_host_integration_extensions.py` 追加：

```python
# ── Task 2：method 桥接 + config_changed 约定 ──

def _plugin_method_target():
    from app.routes.integration_routes import PluginMethodRequest, call_plugin_method
    return PluginMethodRequest, call_plugin_method


def test_call_plugin_method_host_dispatch():
    """宿主集成携带 call_method 时直接分发，成功结果包 success/data 信封。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = AsyncMock(return_value={"status": "pending"})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    result = _run(call_plugin_method(
        "feishu", "qr_poll", PluginMethodRequest(params={}),
        container=_mock_container(layer=layer)))
    assert result == {"success": True, "data": {"status": "pending"}}
    call_method.assert_awaited_once_with("qr_poll", {})


def test_call_plugin_method_host_sync_call_method():
    """call_method 为同步函数时同样支持。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = MagicMock(return_value={"ok": 1})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    result = _run(call_plugin_method(
        "feishu", "anything", PluginMethodRequest(params={"a": 1}),
        container=_mock_container(layer=layer)))
    assert result == {"success": True, "data": {"ok": 1}}
    call_method.assert_called_once_with("anything", {"a": 1})


def test_call_plugin_method_host_failure_passthrough():
    """插件自述失败（success=False）原样透传，不包信封。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = AsyncMock(return_value={"success": False, "message": "未知方法: x"})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    result = _run(call_plugin_method(
        "feishu", "x", PluginMethodRequest(params={}),
        container=_mock_container(layer=layer)))
    assert result == {"success": False, "message": "未知方法: x"}


def test_call_plugin_method_config_changed_triggers_restart():
    """config_changed=True → 调 restart_host_integration_fn，applied=restarted。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = AsyncMock(return_value={"status": "success", "config_changed": True})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    container = _mock_container(layer=layer)
    container.restart_host_integration_fn = MagicMock(return_value=True)
    result = _run(call_plugin_method(
        "feishu", "qr_poll", PluginMethodRequest(params={}),
        container=container))
    assert result["data"]["applied"] == "restarted"
    container.restart_host_integration_fn.assert_called_once()
    assert container.restart_host_integration_fn.call_args.args[0] == "feishu"


def test_call_plugin_method_config_changed_restart_raises():
    """热重启异常时配置仍已保存：applied=saved，不向调用方抛错。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = AsyncMock(return_value={"status": "success", "config_changed": True})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    container = _mock_container(layer=layer)
    container.restart_host_integration_fn = MagicMock(side_effect=RuntimeError("boom"))
    result = _run(call_plugin_method(
        "feishu", "qr_poll", PluginMethodRequest(params={}),
        container=container))
    assert result["data"]["applied"] == "saved"


def test_call_plugin_method_framework_blocked_for_host():
    """框架方法黑名单对宿主集成同样生效。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": AsyncMock()}}
    result = _run(call_plugin_method(
        "feishu", "sink.speak", PluginMethodRequest(params={}),
        container=_mock_container(layer=layer)))
    assert result["success"] is False
    assert "框架方法" in result["message"]


def test_call_plugin_method_falls_back_to_supervisor():
    """host_integrations 无此插件时回落子进程路径（未运行报错，行为不回归）。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    layer = MagicMock()
    layer.host_integrations = {}
    layer._supervisor.get_process.return_value = None
    result = _run(call_plugin_method(
        "xiaoai", "some_method", PluginMethodRequest(params={}),
        container=_mock_container(layer=layer)))
    assert result["success"] is False
    assert "未运行" in result["message"]


def test_start_host_integrations_registers_unconfigured(tmp_path):
    """start() 返回 None（凭证未配置）的宿主集成也收录并注册（alive=False）。

    这是扫码一键接入的前置：未配置时管理页才有卡片可点、restart 才找得到。
    注意：本测试 import app.main（模块级初始化在测试环境可承受，见 conftest 注释）。
    """
    plug = tmp_path / "dummyhost"
    plug.mkdir()
    (plug / "main.py").write_text(
        "def start(dispatch_fn, loop):\n    return None\n", encoding="utf-8")
    from app import main as app_main
    container = MagicMock()
    container.integration_layer = MagicMock()
    started = app_main._start_host_integrations(container, asyncio.new_event_loop())
    assert [n for n, _, _ in started if n == "dummyhost"]
    registered = {
        name: meta for name, meta in
        (c.args for c in container.integration_layer.register_host_integration.call_args_list)
    }
    assert registered["dummyhost"]["alive"] is False
    assert registered["dummyhost"]["call_method"] is None  # 未声明 → None
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_host_integration_extensions.py -v`
Expected: Task 2 各用例 FAIL（宿主分发不存在、未注册等）

- [ ] **Step 3: 实现**

`app/routes/integration_routes.py` 文件顶部 import 区（`import io` 附近）加 `import inspect`；在 `call_plugin_method`（原 274-300 行）整体替换为：

```python
def _restart_host_plugin(container, plugin_id: str) -> str:
    """config_changed 约定的执行端：热重启宿主集成。

    返回 applied：restarted=已重启；not_found=找不到集成；saved=无法重启（配置已落盘）。
    """
    import asyncio

    restart = getattr(container, "restart_host_integration_fn", None)
    if not callable(restart):
        return "saved"
    try:
        ok = restart(plugin_id, asyncio.get_running_loop())
        return "restarted" if ok else "not_found"
    except Exception:  # noqa: BLE001
        logger.exception("宿主集成 %s 热重启失败（配置已保存，下次启动生效）", plugin_id)
        return "saved"


@router.post("/integrations/{plugin_id}/method/{method}")
async def call_plugin_method(
    plugin_id: str,
    method: str,
    req: PluginMethodRequest | None = None,
    container=Depends(get_container),
    admin: dict = Depends(get_current_admin),
):
    """调用插件自定义方法（管理员）。

    子进程插件：setup 里 register_method 注册，经 supervisor RPC；
    宿主侧集成：注册信息携带 call_method 句柄，进程内直接分发。
    插件面板（ui_contributions 的 custom_component）经此入口与插件交互：
    参数透传、结果透传，宿主不做语义解释。
    """
    if method in _FRAMEWORK_METHODS:
        return {"success": False, "message": f"框架方法不允许经此入口调用: {method}"}
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    params = (req.params if req is not None else {}) or {}

    # 宿主侧集成（进程内）优先
    host_info = getattr(layer, "host_integrations", {}).get(plugin_id)
    call_method = host_info.get("call_method") if isinstance(host_info, dict) else None
    if call_method is not None:
        try:
            result = call_method(method, params)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"调用失败: {exc}"}
        if not isinstance(result, dict):
            return {"success": True, "data": {"result": result}}
        if result.get("success") is False:
            return result  # 插件自述失败（自带 message），原样透传
        # 约定：插件声明 config_changed 表示已改自身配置，宿主负责热重启
        if result.get("config_changed"):
            result["applied"] = _restart_host_plugin(container, plugin_id)
        return {"success": True, "data": result}

    # 子进程插件（原路径）
    proc = layer._supervisor.get_process(plugin_id)
    if proc is None or not proc.is_alive:
        return {"success": False, "message": f"插件 {plugin_id} 未运行"}
    try:
        result = await proc.call(method, params)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "message": f"调用失败: {exc}"}
    return {"success": True, "data": result}
```

`app/main.py` `_start_host_integrations` 注册段，把：

```python
            if hasattr(mod, "start"):
                instance = mod.start(dispatch_fn, loop)
                if instance:
                    started.append((name, mod, instance))
                    logger.info("宿主侧集成 %s 已启动", name)
                    # 注册到 IntegrationLayer 供插件管理页显示
                    meta = _load_host_integration_meta(name, integrations_dir)
                    if container.integration_layer:
                        container.integration_layer.register_host_integration(name, meta)
```

替换为：

```python
            if hasattr(mod, "start"):
                instance = mod.start(dispatch_fn, loop)
                # 未启动（如凭证未配置）也收录并注册：管理页要能显示卡片、
                # 提供配置/扫码入口；restart 才能找到并按新配置拉起。
                started.append((name, mod, instance))
                meta = _load_host_integration_meta(name, integrations_dir)
                meta["call_method"] = getattr(mod, "call_method", None)
                if container.integration_layer:
                    meta["alive"] = instance is not None
                    container.integration_layer.register_host_integration(name, meta)
                if instance:
                    logger.info("宿主侧集成 %s 已启动", name)
```

`app/main.py` `_restart_host_integration` 中 `meta = _load_host_integration_meta(...)` 之后、`register_host_integration` 之前加：

```python
        meta["call_method"] = getattr(mod, "call_method", None)
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_host_integration_extensions.py tests/test_integration_routes.py tests/test_infra_coverage.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/main.py app/routes/integration_routes.py tests/test_host_integration_extensions.py
git commit -m "feat(integration): method RPC 桥接宿主集成 + config_changed 热重启约定；未启动集成也注册"
```

---

### Task 3: 弹窗通用渲染 slot + 插件面板变更广播

**Files:**
- Modify: `frontend/src/views/PluginManageView.vue`（template 第 67 行 plugin-meta div 之后；script import 与 onMounted 段）

**Interfaces:**
- Produces: slot 名 `plugin_config_modal`（插件 ui_contributions 声明此 slot 即渲染进配置弹窗）；窗口事件 `aether:plugins-changed`（插件面板改完配置后 `window.dispatchEvent`，管理页刷新列表）。

- [ ] **Step 1: template 改动**

在 `<div class="plugin-meta" ...>...</div>`（第 62-67 行）之后插入：

```html
        <!-- 插件自定义面板：ui_contributions 声明 plugin_config_modal 的插件在此渲染
             （如飞书扫码一键接入）。无贡献的插件此处渲染为空，零影响。 -->
        <PluginSlot slot="plugin_config_modal" />
```

- [ ] **Step 2: script 改动**

import 区（第 121-123 行）改为：

```js
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { apiGet, apiPost } from '../utils/api'
import AdvancedModal from '../components/AdvancedModal.vue'
import PluginSlot from '../components/integration/PluginSlot.vue'
```

把末尾 `onMounted(loadPlugins)` 替换为：

```js
onMounted(async () => {
  // 插件面板（如飞书扫码接入）改完配置后广播事件，管理页刷新列表与存活徽标
  window.addEventListener('aether:plugins-changed', loadPlugins)
  await loadPlugins()
})

onBeforeUnmount(() => {
  window.removeEventListener('aether:plugins-changed', loadPlugins)
})
```

- [ ] **Step 3: 构建验证**

Run: `cd D:/Aether/frontend && npm run build`
Expected: 构建成功（本任务暂无贡献者，slot 渲染为空）

- [ ] **Step 4: 提交**

```bash
git add frontend/src/views/PluginManageView.vue
git commit -m "feat(frontend): 配置弹窗通用插件面板 slot + plugins-changed 刷新广播"
```

---

### Task 4: 插件侧 qr_setup.py——设备授权三步 + 会话状态（TDD）

**Files:**
- Create: `integrations/feishu/qr_setup.py`
- Test: `tests/test_feishu_qr_setup.py`（新建）

**Interfaces:**
- Produces（供 Task 5 使用）:
  - `async start_session() -> dict`：`{qr_url, user_code, expires_in, interval}`；失败抛 `FeishuQrSetupError`
  - `async poll_once() -> dict`：`{status: "pending"|"scanned"|"denied"|"expired"|"success"|"error", message?}`；success 时凭证入 `_pending_credentials` 且会话关闭
  - `consume_result() -> tuple[str, str] | None`（取走 app_id/app_secret，取走即失效）
  - `cancel_session() -> None`
  - `FeishuQrSetupError(Exception)`（message 用户可读）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_feishu_qr_setup.py`：

```python
"""飞书扫码 qr_setup 状态机测试（mock 飞书端点，不触网）。"""

import asyncio
import time

import pytest

from integrations.feishu import qr_setup


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset_session():
    qr_setup._session = None
    qr_setup._pending_credentials = None
    yield
    qr_setup._session = None
    qr_setup._pending_credentials = None


def _patch_feishu(monkeypatch, responses):
    """按调用顺序回放响应（Exception 表示网络异常）；返回请求体记录列表。"""
    calls = []
    iterator = iter(responses)

    async def fake_post(payload):
        calls.append(dict(payload))
        resp = next(iterator)
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(qr_setup, "_post_registration", fake_post)
    return calls


_INIT_OK = {"nonce": "n", "supported_auth_methods": ["private_key_jwt", "client_secret"]}
_BEGIN_OK = {
    "device_code": "dev123",
    "user_code": "AB12-CD34",
    "verification_uri": "https://open.feishu.cn/page/launcher",
    "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=AB12-CD34",
    "expires_in": 3600,
    "interval": 5,
}


def test_start_session_returns_qr_fields(monkeypatch):
    calls = _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    result = _run(qr_setup.start_session())
    assert result["qr_url"] == _BEGIN_OK["verification_uri_complete"]
    assert result["user_code"] == "AB12-CD34"
    assert result["expires_in"] == 3600
    assert result["interval"] == 5
    # begin 参数按协议：PersonalAgent 原型 + client_secret
    assert calls[1]["action"] == "begin"
    assert calls[1]["archetype"] == "PersonalAgent"
    assert calls[1]["auth_method"] == "client_secret"
    assert qr_setup._session["device_code"] == "dev123"
    assert qr_setup._session["last_status"] == "pending"


def test_start_session_unsupported_auth_rejected(monkeypatch):
    _patch_feishu(monkeypatch, [{"supported_auth_methods": ["private_key_jwt"]}])
    with pytest.raises(qr_setup.FeishuQrSetupError):
        _run(qr_setup.start_session())


def test_start_session_bad_begin_rejected(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, {"no_device_code": True}])
    with pytest.raises(qr_setup.FeishuQrSetupError):
        _run(qr_setup.start_session())


def test_poll_without_session_returns_expired():
    result = _run(qr_setup.poll_once())
    assert result["status"] == "expired"


def _mk_async_stub(responses):
    """不记录调用的按序回放桩（用于 poll 阶段重新打桩）。"""
    iterator = iter(responses)

    async def fake_post(payload):
        return next(iterator)

    return fake_post


def test_poll_pending_then_scanned_then_success(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    monkeypatch.setattr(qr_setup, "_post_registration", _mk_async_stub([
        {"error": "authorization_pending"},
        {"user_info": {"open_id": "ou_x"}},
        {"client_id": "cli_aaa123", "client_secret": "sec_xyz"},
    ]))
    assert _run(qr_setup.poll_once())["status"] == "pending"
    assert _run(qr_setup.poll_once())["status"] == "scanned"
    success = _run(qr_setup.poll_once())
    assert success["status"] == "success"
    assert qr_setup.consume_result() == ("cli_aaa123", "sec_xyz")
    assert qr_setup.consume_result() is None          # 取走即失效
    assert qr_setup._session is None                  # 会话已关闭


def test_poll_rate_limit_returns_cached_without_request(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    qr_setup._session["last_poll_ts"] = time.monotonic()  # 刚轮询过
    calls = _patch_feishu(monkeypatch, [])                # 任何请求都不该发生
    assert _run(qr_setup.poll_once())["status"] == "pending"
    assert calls == []


def test_poll_slow_down_increases_interval(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    _patch_feishu(monkeypatch, [{"error": "slow_down"}])
    _run(qr_setup.poll_once())
    assert qr_setup._session["interval"] == 10


def test_poll_denied(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK, {"error": "access_denied"}])
    _run(qr_setup.start_session())
    result = _run(qr_setup.poll_once())
    assert result["status"] == "denied"
    assert qr_setup._session is None


def test_poll_expired_token(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK, {"error": "expired_token"}])
    _run(qr_setup.start_session())
    assert _run(qr_setup.poll_once())["status"] == "expired"


def test_poll_local_expiry(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    qr_setup._session["expires_at"] = time.monotonic() - 1
    assert _run(qr_setup.poll_once())["status"] == "expired"
    assert qr_setup._session is None


def test_poll_consecutive_errors_threshold(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    for _ in range(4):
        _patch_feishu(monkeypatch, [RuntimeError("net down")])
        assert _run(qr_setup.poll_once())["status"] == "pending"  # 容错续等
    _patch_feishu(monkeypatch, [RuntimeError("net down")])
    result = _run(qr_setup.poll_once())  # 第 5 次：报通道故障
    assert result["status"] == "error"
    assert qr_setup._session is None


def test_cancel_session_clears_state(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    qr_setup.cancel_session()
    assert qr_setup._session is None
    assert _run(qr_setup.poll_once())["status"] == "expired"
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_feishu_qr_setup.py -v`
Expected: FAIL（`integrations.feishu.qr_setup` 模块不存在）

- [ ] **Step 3: 实现**

新建 `integrations/feishu/qr_setup.py`：

```python
"""飞书扫码一键接入——设备授权式应用注册。

走飞书账号体系的未公开端点 accounts.feishu.cn/oauth/v1/app/registration
（OAuth Device Flow 风格）：init 校验环境 → begin 生成二维码 → poll 换取
client_id/client_secret。协议经 OpenClaw 生产验证
（extensions/feishu/src/app-registration.ts），2026-09 实测开放。

飞书若收紧该端点：本模块抛 FeishuQrSetupError，前端引导改用手动配置
（弹窗内 config_schema 表单），接入主路径不受影响。

会话只存内存：单管理员场景，重复 start 覆盖前会话；服务重启即清空。
"""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_ACCOUNTS_URL = "https://accounts.feishu.cn"
_REGISTRATION_PATH = "/oauth/v1/app/registration"
_REQUEST_TIMEOUT = 10.0
_DEFAULT_INTERVAL = 5        # poll 轮询间隔（秒），begin 实际返回优先
_DEFAULT_EXPIRES_IN = 3600   # 二维码有效期（秒）
_MAX_CONSECUTIVE_ERRORS = 5  # poll 连续异常阈值，达阈值报通道故障


class FeishuQrSetupError(Exception):
    """扫码接入流程错误（message 面向最终用户可读）。"""


_session: dict | None = None
_pending_credentials: tuple[str, str] | None = None


async def _post_registration(payload: dict) -> dict:
    """POST 飞书注册端点（表单编码），返回 JSON dict。非 dict 响应视为协议异常。"""
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_ACCOUNTS_URL}{_REGISTRATION_PATH}",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise FeishuQrSetupError("飞书扫码通道响应异常，请改用手动配置")
    return data


async def start_session() -> dict:
    """发起扫码会话：init 校验 + begin 取二维码。返回前端展示字段。"""
    global _session
    init_data = await _post_registration({"action": "init"})
    if "client_secret" not in (init_data.get("supported_auth_methods") or []):
        raise FeishuQrSetupError("当前飞书环境不支持扫码接入，请改用手动配置")
    begin = await _post_registration({
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    device_code = begin.get("device_code")
    qr_url = begin.get("verification_uri_complete") or begin.get("verification_uri")
    if not device_code or not qr_url:
        raise FeishuQrSetupError("飞书扫码通道响应异常，请改用手动配置")
    interval = max(1, int(begin.get("interval") or _DEFAULT_INTERVAL))
    expires_in = int(begin.get("expires_in") or _DEFAULT_EXPIRES_IN)
    _session = {
        "device_code": device_code,
        "qr_url": qr_url,
        "user_code": str(begin.get("user_code") or ""),
        "expires_at": time.monotonic() + expires_in,
        "interval": interval,
        "last_poll_ts": 0.0,
        "last_status": "pending",
        "consecutive_errors": 0,
    }
    logger.info("飞书扫码会话已建立（user_code=%s，%ds 有效）",
                _session["user_code"], expires_in)
    return {
        "qr_url": qr_url,
        "user_code": _session["user_code"],
        "expires_in": expires_in,
        "interval": interval,
    }


async def poll_once() -> dict:
    """按协议轮询一次（内部限速）。

    返回 {status: pending|scanned|denied|expired|success|error, message?}。
    success 时凭证存入 _pending_credentials（consume_result 取走即失效），
    会话即刻关闭，杜绝二次消费。
    """
    global _session, _pending_credentials
    if _session is None:
        return {"status": "expired", "message": "扫码会话不存在，请重新获取二维码"}
    if time.monotonic() >= _session["expires_at"]:
        _session = None
        return {"status": "expired", "message": "二维码已过期，请重新获取"}
    now = time.monotonic()
    if now - _session["last_poll_ts"] < _session["interval"]:
        return {"status": _session["last_status"]}
    _session["last_poll_ts"] = now
    try:
        data = await _post_registration(
            {"action": "poll", "device_code": _session["device_code"]})
    except Exception as exc:  # noqa: BLE001 —— 网络抖动按容错计数，不打断扫码
        logger.warning("飞书扫码 poll 请求失败: %s", exc)
        return _count_poll_error()
    if not isinstance(data, dict):
        return _count_poll_error()

    err = data.get("error")
    if err == "slow_down":
        _session["interval"] += 5
        _session["last_status"] = "pending"
        return {"status": "pending"}
    if err == "authorization_pending":
        _session["last_status"] = "pending"
        return {"status": "pending"}
    if err == "access_denied":
        _session = None
        return {"status": "denied", "message": "手机端已拒绝，可重新扫码"}
    if err == "expired_token":
        _session = None
        return {"status": "expired", "message": "二维码已过期，请重新获取"}
    if data.get("client_id") and data.get("client_secret"):
        _pending_credentials = (str(data["client_id"]), str(data["client_secret"]))
        _session = None
        logger.info("飞书扫码授权成功（app_id=%s…）", str(data["client_id"])[:10])
        return {"status": "success"}
    if data.get("user_info"):  # 已扫码待确认（poll 响应携带 user_info 但尚无凭证）
        _session["last_status"] = "scanned"
        return {"status": "scanned"}
    return _count_poll_error()


def _count_poll_error() -> dict:
    """poll 异常容错：连续 <5 次按上次状态续等，达阈值报通道故障并收会话。"""
    global _session
    if _session is None:
        return {"status": "expired", "message": "扫码会话不存在，请重新获取二维码"}
    _session["consecutive_errors"] += 1
    if _session["consecutive_errors"] >= _MAX_CONSECUTIVE_ERRORS:
        _session = None
        return {"status": "error",
                "message": "飞书扫码通道异常，请稍后重试或改用手动配置"}
    return {"status": _session["last_status"]}


def consume_result() -> tuple[str, str] | None:
    """取走扫码授权凭证 (app_id, app_secret)；取走即失效，防二次消费。"""
    global _pending_credentials
    creds, _pending_credentials = _pending_credentials, None
    return creds


def cancel_session() -> None:
    """丢弃当前扫码会话与未消费凭证。"""
    global _session, _pending_credentials
    _session = None
    _pending_credentials = None
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_feishu_qr_setup.py tests/test_feishu_ws_client.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add integrations/feishu/qr_setup.py tests/test_feishu_qr_setup.py
git commit -m "feat(feishu): 扫码接入设备授权客户端（init/begin/poll + 内存会话）"
```

---

### Task 5: 插件侧 call_method 白名单 + meta UI 贡献声明

**Files:**
- Modify: `integrations/feishu/main.py`（import 区 + 文件末尾追加）
- Modify: `integrations/feishu/meta.py`（文件末尾追加）
- Test: `tests/test_feishu_qr_methods.py`（新建）

**Interfaces:**
- Consumes: Task 4 的 `qr_setup.start_session/poll_once/consume_result/cancel_session/FeishuQrSetupError`
- Produces: `integrations.feishu.main.async call_method(method: str, params: dict | None) -> dict`（宿主桥接句柄，main.py 启动时经 `getattr(mod, "call_method")` 注册）；方法 `qr_start`/`qr_poll`/`qr_cancel`；`qr_poll` 成功返回 `{status:"success", config_changed: True, app_id_masked}`；`meta.py` 导出 `UI_CONTRIBUTIONS`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_feishu_qr_methods.py`：

```python
"""飞书插件 call_method（qr_start/qr_poll/qr_cancel）测试。"""

import asyncio
from unittest.mock import MagicMock

import pytest

from integrations.feishu import main as feishu_main
from integrations.feishu import qr_setup


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset():
    qr_setup._session = None
    qr_setup._pending_credentials = None
    yield
    qr_setup._session = None
    qr_setup._pending_credentials = None


def test_call_method_unknown_rejected():
    assert _run(feishu_main.call_method("not_a_method", {}))["success"] is False


def test_call_method_framework_blocked():
    assert _run(feishu_main.call_method("sink.speak", {}))["success"] is False


def test_qr_start_proxies_session_fields(monkeypatch):
    async def fake_start():
        return {"qr_url": "https://x", "user_code": "AB12-CD34",
                "expires_in": 3600, "interval": 5}

    monkeypatch.setattr(qr_setup, "start_session", fake_start)
    result = _run(feishu_main.call_method("qr_start", {}))
    assert result["qr_url"] == "https://x"


def test_qr_poll_success_writes_config_and_keeps_other_fields(monkeypatch):
    qr_setup._pending_credentials = ("cli_aaa123", "sec_xyz")
    saved = {}
    monkeypatch.setattr("app.integration.config_helper.get_host_config",
                        lambda pid: {"notify_chat_id": "oc_kept"})

    def fake_set(pid, values):
        saved[pid] = dict(values)

    monkeypatch.setattr("app.integration.config_helper.set_host_config", fake_set)
    result = _run(feishu_main.call_method("qr_poll", {}))
    assert result["status"] == "success"
    assert result["config_changed"] is True
    assert result["app_id_masked"].startswith("cli_aa")
    assert saved["feishu"]["app_id"] == "cli_aaa123"
    assert saved["feishu"]["app_secret"] == "sec_xyz"
    assert saved["feishu"]["notify_chat_id"] == "oc_kept"  # 其余字段保留
    assert qr_setup.consume_result() is None               # 凭证已消费


def test_qr_poll_pending_passthrough(monkeypatch):
    async def fake_poll():
        return {"status": "pending"}

    monkeypatch.setattr(qr_setup, "poll_once", fake_poll)
    assert _run(feishu_main.call_method("qr_poll", {})) == {"status": "pending"}


def test_qr_start_error_becomes_failure_message(monkeypatch):
    async def bad_start():
        raise qr_setup.FeishuQrSetupError("当前飞书环境不支持扫码接入，请改用手动配置")

    monkeypatch.setattr(qr_setup, "start_session", bad_start)
    result = _run(feishu_main.call_method("qr_start", {}))
    assert result["success"] is False
    assert "手动配置" in result["message"]


def test_qr_cancel_clears_session(monkeypatch):
    qr_setup._session = {"device_code": "d"}
    result = _run(feishu_main.call_method("qr_cancel", {}))
    assert result["status"] == "cancelled"
    assert qr_setup._session is None


def test_meta_declares_config_modal_contribution():
    assert feishu_main is not None  # 模块可导入
    from integrations.feishu import meta
    assert meta.UI_CONTRIBUTIONS == [
        {"slot": "plugin_config_modal", "type": "custom_component"}]
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_feishu_qr_methods.py -v`
Expected: FAIL（`call_method`/`UI_CONTRIBUTIONS` 不存在）

- [ ] **Step 3: 实现**

`integrations/feishu/meta.py` 文件末尾追加：

```python
# 插件面板贡献：配置弹窗内渲染扫码接入面板（frontend/FeishuQrSetup.vue，
# 由宿主 PluginSlot 经 import.meta.glob 动态加载）。
UI_CONTRIBUTIONS = [
    {"slot": "plugin_config_modal", "type": "custom_component"},
]
```

`integrations/feishu/main.py`：import 区加 `from . import qr_setup`；文件末尾追加：

```python
# ── 扫码一键接入：宿主 method 桥接入口（管理员经 /api/integrations/feishu/method/*）──

_METHOD_APP_ID_PREFIX_LEN = 6


async def _method_qr_start(params: dict) -> dict:
    """发起扫码会话，返回二维码展示字段。"""
    return await qr_setup.start_session()


async def _method_qr_poll(params: dict) -> dict:
    """轮询扫码结果；success 时凭证落库（保留其余配置字段）并声明热重启。"""
    result = await qr_setup.poll_once()
    if result.get("status") != "success":
        return result
    creds = qr_setup.consume_result()
    if creds is None:  # 理论不可达（poll 成功即置凭证）；防御性兜底
        return {"status": "expired", "message": "凭证已被消费，请重新扫码"}
    app_id, app_secret = creds
    from app.integration.config_helper import get_host_config, set_host_config
    merged = get_host_config("feishu") or {}
    merged["app_id"] = app_id
    merged["app_secret"] = app_secret
    set_host_config("feishu", merged)
    return {
        "status": "success",
        "config_changed": True,  # 宿主约定：据此热重启本集成
        "app_id_masked": f"{app_id[:_METHOD_APP_ID_PREFIX_LEN]}***",
    }


async def _method_qr_cancel(params: dict) -> dict:
    qr_setup.cancel_session()
    return {"status": "cancelled"}


_METHODS = {
    "qr_start": _method_qr_start,
    "qr_poll": _method_qr_poll,
    "qr_cancel": _method_qr_cancel,
}


async def call_method(method: str, params: dict | None = None) -> dict:
    """宿主注入的方法入口（启动时经 getattr 注册进集成层）。

    白名单分发；业务失败返回 {"success": False, "message"}，宿主原样透传。
    """
    handler = _METHODS.get(method)
    if handler is None:
        return {"success": False, "message": f"未知方法: {method}"}
    try:
        return await handler(params or {})
    except qr_setup.FeishuQrSetupError as exc:
        return {"success": False, "message": str(exc)}
    except Exception:  # noqa: BLE001
        logger.exception("飞书扫码方法 %s 执行失败", method)
        return {"success": False, "message": "飞书扫码通道异常，请稍后重试或改用手动配置"}
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_feishu_qr_methods.py tests/test_feishu_qr_setup.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add integrations/feishu/main.py integrations/feishu/meta.py tests/test_feishu_qr_methods.py
git commit -m "feat(feishu): call_method 白名单暴露扫码方法 + meta 声明弹窗 UI 贡献"
```

---

### Task 6: 插件前端 FeishuQrSetup.vue

**Files:**
- Create: `integrations/feishu/frontend/FeishuQrSetup.vue`

**Interfaces:**
- Consumes: Task 3 的 slot `plugin_config_modal`（PluginSlot 按 `integrations/feishu/frontend/*.vue` glob 自动加载）；Task 5 的三个 method；`GET /api/integrations/feishu/config`（已存在，apiGet 已解包 `data`）；窗口事件 `aether:plugins-changed`。

- [ ] **Step 1: 写组件**

新建 `integrations/feishu/frontend/FeishuQrSetup.vue`：

```vue
<template>
  <div class="qr-setup">
    <!-- 折叠态：已接入徽标 + 重扫入口 -->
    <div v-if="!expanded" class="qr-done-row">
      <span class="qr-done-text">⚡ 飞书扫码一键接入</span>
      <span v-if="configured" class="qr-done-badge">已接入</span>
      <button class="action-btn" @click="expand">{{ configured ? '重新扫码' : '扫码接入' }}</button>
    </div>

    <template v-else>
      <div class="qr-title">⚡ 扫码一键接入</div>
      <p class="qr-hint">
        打开飞书 App → 扫一扫，确认后机器人自动创建并接入。
        私聊仅限扫码人本人；全员可用请用下方手动配置。
      </p>

      <div v-if="qrDataUrl" class="qr-img-wrap">
        <img :src="qrDataUrl" class="qr-img" alt="飞书扫码二维码" />
        <div v-if="userCode" class="qr-user-code">
          扫不了码？在飞书 App 输入：<b>{{ userCode }}</b>
        </div>
      </div>

      <div class="qr-status" :data-state="status">{{ statusText }}</div>
      <div v-if="remainingText" class="qr-countdown">{{ remainingText }}</div>

      <div class="qr-actions">
        <button
          v-if="['idle', 'denied', 'expired', 'error'].includes(status)"
          class="btn-primary"
          :disabled="starting"
          @click="startSetup"
        >{{ starting ? '获取二维码中…' : '获取二维码' }}</button>
        <button
          v-if="['ready', 'scanned', 'success'].includes(status)"
          class="action-btn"
          @click="cancelSetup"
        >{{ status === 'success' ? '关闭' : '取消' }}</button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import QRCode from 'qrcode'
import { apiGet, apiPost } from '@/utils/api'

// 本文件位于 integrations/feishu/frontend/，由宿主 PluginSlot 的
// import.meta.glob 动态加载；apiGet/apiPost 已自动解包响应 data 字段。
const METHOD_URL = '/api/integrations/feishu/method'
const POLL_FALLBACK_SEC = 5

const expanded = ref(false)
const configured = ref(false)
const starting = ref(false)
const qrDataUrl = ref('')
const userCode = ref('')
const expiresAt = ref(0)
const remainingText = ref('')
const status = ref('idle') // idle|ready|scanned|success|denied|expired|error
const statusText = ref('')

const STATUS_TEXT = {
  idle: '',
  ready: '等待扫码…',
  scanned: '已扫码，请在手机上确认',
  success: '✓ 已接入',
  denied: '已取消（手机端拒绝）',
  expired: '二维码已过期',
  error: '飞书扫码通道异常，请改用手动配置',
}

let pollTimer = null
let countdownTimer = null

async function checkConfigured() {
  try {
    const values = (await apiGet('/api/integrations/feishu/config'))?.values || {}
    configured.value = !!values.app_id || !!values.app_secret?.is_set
  } catch {
    configured.value = false
  }
}

function expand() {
  expanded.value = true
  if (!configured.value) startSetup()
}

async function startSetup() {
  starting.value = true
  status.value = 'idle'
  statusText.value = ''
  qrDataUrl.value = ''
  userCode.value = ''
  try {
    const data = await apiPost(`${METHOD_URL}/qr_start`, {})
    if (data?.success === false) throw new Error(data.message || '获取二维码失败')
    qrDataUrl.value = await QRCode.toDataURL(data.qr_url, { width: 180, margin: 1 })
    userCode.value = data.user_code || ''
    expiresAt.value = Date.now() + (data.expires_in || 3600) * 1000
    status.value = 'ready'
    statusText.value = STATUS_TEXT.ready
    startPolling(data.interval || POLL_FALLBACK_SEC)
    startCountdown()
  } catch (e) {
    status.value = 'error'
    statusText.value = e?.message || '获取二维码失败，请改用手动配置'
  } finally {
    starting.value = false
  }
}

function startPolling(intervalSec) {
  stopPolling()
  pollTimer = setInterval(pollOnce, Math.max(3, intervalSec) * 1000)
  pollOnce()
}

async function pollOnce() {
  try {
    const data = await apiPost(`${METHOD_URL}/qr_poll`, {})
    if (data?.success === false) {
      // 后端明示故障（HTTP 200 + success:false，apiPost 不抛错）：停轮询报错
      stopPolling(); stopCountdown()
      qrDataUrl.value = ''
      status.value = 'error'
      statusText.value = data.message || STATUS_TEXT.error
      return
    }
    const st = data?.status
    if (st === 'success') {
      stopPolling(); stopCountdown()
      qrDataUrl.value = ''
      status.value = 'success'
      statusText.value = data.applied === 'restarted'
        ? '✓ 已接入，机器人已启动'
        : '✓ 凭证已保存，但插件未在运行（下次启动生效）'
      // 通知管理页刷新列表与存活徽标
      window.dispatchEvent(new CustomEvent('aether:plugins-changed'))
      setTimeout(() => { expanded.value = false; checkConfigured() }, 2500)
    } else if (st === 'scanned') {
      status.value = 'scanned'
      statusText.value = STATUS_TEXT.scanned
    } else if (st === 'denied' || st === 'expired' || st === 'error') {
      stopPolling(); stopCountdown()
      qrDataUrl.value = ''
      status.value = st
      statusText.value = data.message || STATUS_TEXT[st]
    }
    // pending：保持现状继续轮询
  } catch {
    /* 单次网络失败静默重试；连续失败由后端 error 状态回报 */
  }
}

function startCountdown() {
  stopCountdown()
  const tick = () => {
    const ms = expiresAt.value - Date.now()
    if (ms <= 0) { remainingText.value = ''; return }
    const total = Math.floor(ms / 1000)
    remainingText.value =
      `二维码剩余 ${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
  }
  tick()
  countdownTimer = setInterval(tick, 1000)
}

async function cancelSetup() {
  stopPolling(); stopCountdown()
  try { await apiPost(`${METHOD_URL}/qr_cancel`, {}) } catch { /* 忽略 */ }
  expanded.value = false
  status.value = 'idle'
  statusText.value = ''
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function stopCountdown() {
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null }
  remainingText.value = ''
}

onMounted(checkConfigured)
onBeforeUnmount(() => { stopPolling(); stopCountdown() })
</script>

<style scoped>
.qr-setup {
  padding: 12px;
  margin-bottom: 14px;
  border: 1px dashed var(--color-border, #d8dce3);
  border-radius: 10px;
}
.qr-done-row { display: flex; align-items: center; gap: 10px; }
.qr-done-text { font-weight: 600; }
.qr-done-badge {
  font-size: 12px;
  color: #1a7f37;
  background: rgba(26, 127, 55, 0.1);
  border-radius: 999px;
  padding: 1px 8px;
}
.qr-title { font-weight: 600; margin-bottom: 4px; }
.qr-hint { font-size: 12px; color: var(--color-text-secondary, #888); margin: 0 0 10px; }
.qr-img-wrap { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.qr-img { width: 180px; height: 180px; border-radius: 8px; background: #fff; padding: 6px; }
.qr-user-code { font-size: 12px; color: var(--color-text-secondary, #888); }
.qr-status { margin-top: 10px; font-size: 13px; }
.qr-status[data-state='success'] { color: #1a7f37; }
.qr-status[data-state='error'], .qr-status[data-state='denied'] { color: #c0392b; }
.qr-countdown { font-size: 12px; color: var(--color-text-secondary, #888); margin-top: 2px; }
.qr-actions { display: flex; gap: 10px; margin-top: 10px; }
</style>
```

- [ ] **Step 2: 构建验证**

Run: `cd D:/Aether/frontend && npm run build`
Expected: 构建成功（glob 命中 integrations/feishu/frontend/FeishuQrSetup.vue）

- [ ] **Step 3: 提交**

```bash
git add integrations/feishu/frontend/FeishuQrSetup.vue
git commit -m "feat(feishu): 扫码一键接入面板组件（二维码 + 轮询状态机 + 已接入折叠态）"
```

---

### Task 7: 端到端验证 + 文档 + 全量回归

**Files:**
- Modify: `docs/06-集成扩展/飞书机器人接入指南.md`
- Test: 全量 pytest + ruff + 浏览器 GUI 走查

- [ ] **Step 1: 全量回归**

Run: `python -m pytest -x -q && python -m ruff check app integrations tests`
Expected: 全部 PASS / 无 lint 错误

- [ ] **Step 2: 浏览器 GUI 走查**

1. 启动后端与前端（`python -m app.main` / `cd frontend && npm run dev`，或直接用运行中的部署）
2. 登录管理员 → `/plugin` → 飞书机器人卡片「配置」→ 弹窗内出现「⚡ 飞书扫码一键接入」面板
3. 点「扫码接入」→ 二维码渲染、倒计时走动、状态「等待扫码…」
4. 用飞书 App 真实扫码确认 → 状态依次「已扫码，请在手机上确认」→「✓ 已接入，机器人已启动」，面板 2.5 秒后折叠为「已接入」，卡片徽标变「运行中」
5. 在飞书里私聊机器人发「你好」→ 收到 Aether 回复（真实扫码一步需人工）
6. 「取消」路径：重新展开 → 取消 → 定时器停止（无残留轮询）

- [ ] **Step 3: 更新接入文档**

`docs/06-集成扩展/飞书机器人接入指南.md` 在「## Aether 侧：填凭证」一节之前插入：

```markdown
## 扫码一键接入（推荐）

不想手动建应用？管理页 `/plugin` → 「飞书机器人」卡片 → 配置，弹窗顶部就是**扫码一键接入**：

1. 点「扫码接入」，用**飞书 App** 扫二维码；
2. 手机上确认创建，几秒后面板显示「✓ 已接入，机器人已启动」——凭证自动写入并热拉起长连接，全程不用碰 App ID / Secret。

两点须知：

- 这样创建的是**个人代理**型应用，私聊仅对扫码人本人开放；要给全家/全员用，走下面的手动配置。
- 扫码走的是飞书账号体系的设备授权通道（未公开 API，随飞书策略可能调整）。通道不可用时面板会提示，手动配置路径永远可用，不受影响。
```

- [ ] **Step 4: 提交**

```bash
git add docs/06-集成扩展/飞书机器人接入指南.md
git commit -m "docs(feishu): 接入指南新增扫码一键接入章节"
```

---

## 任务依赖

Task 1 → Task 2（同一测试文件递增）→ Task 3 → Task 4 → Task 5（依赖 Task 4）→ Task 6（依赖 Task 3/5）→ Task 7。Task 3 与 Task 4 可并行。
