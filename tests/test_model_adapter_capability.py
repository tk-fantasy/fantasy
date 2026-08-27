"""model_adapter 进程内能力链路测试。

覆盖：schema 契约（能力类型 + needs_subprocess）、IntegrationLayer 不为
进程内插件 spawn 子进程、Dispatcher 家族适配节点（_inject_family_switch）。
宿主侧只允许通用节点，家族行为必须来自插件——本文件同时守护这一点：
dispatcher/integration_layer 源码里不得出现任何家族名硬编码。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.integration.integration_layer import IntegrationLayer
from app.integration.manifest_loader import load_manifests


def _write_plugin(root: Path, plugin_id: str, cap_type: str,
                  adapters_py: bool = True) -> None:
    pdir = root / plugin_id
    pdir.mkdir(parents=True)
    manifest = {
        "id": plugin_id, "name": plugin_id, "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": cap_type, "id": f"{plugin_id}_cap"}],
    }
    (pdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (pdir / "plugin.py").write_text("", encoding="utf-8")
    if adapters_py:
        (pdir / "adapters.py").write_text(
            "import re\n"
            "from app.agents.model_family_adapters import ModelFamilyAdapter\n\n"
            "class Adapter(ModelFamilyAdapter):\n"
            "    family = 'fake'\n"
            "    _match_re = re.compile(r'fake-model')\n\n"
            "    def no_think(self, s, u):\n"
            "        return s + '|sys', u + '|usr'\n\n"
            "ADAPTERS = [Adapter()]\n",
            encoding="utf-8")


class TestSchemaContract:
    def test_model_adapter_manifest_validates(self, tmp_path):
        """model_adapter 能力声明可通过 manifest 校验（曾被 enum 缺失拒绝）。"""
        _write_plugin(tmp_path, "fake-adapter", "model_adapter")
        manifests = load_manifests(str(tmp_path))
        assert [m.id for m in manifests] == ["fake-adapter"]
        assert manifests[0].capabilities[0].type.value == "model_adapter"

    def test_needs_subprocess_split(self, tmp_path):
        """进程内能力不占子进程宿主；output_sink/inbound_router 需要；
        空能力声明（纯反调型/测试桩插件）维持子进程宿主。"""
        _write_plugin(tmp_path, "fake-adapter", "model_adapter")
        _write_plugin(tmp_path, "a-sink", "output_sink")
        _write_plugin(tmp_path, "a-router", "inbound_router")
        empty_dir = tmp_path / "no-cap"
        empty_dir.mkdir()
        (empty_dir / "manifest.json").write_text(json.dumps({
            "id": "no-cap", "name": "no-cap", "version": "1.0.0",
            "aether_api_version": "1", "entry": "plugin.py",
            "capabilities": [],
        }), encoding="utf-8")
        (empty_dir / "plugin.py").write_text("", encoding="utf-8")
        by_id = {m.id: m for m in load_manifests(str(tmp_path))}
        assert by_id["fake-adapter"].needs_subprocess is False
        assert by_id["a-sink"].needs_subprocess is True
        assert by_id["a-router"].needs_subprocess is True
        assert by_id["no-cap"].needs_subprocess is True


class TestLayerSkipsInProcessPlugins:
    def _make_layer(self, tmp_path) -> tuple[IntegrationLayer, MagicMock]:
        from unittest.mock import AsyncMock
        layer = IntegrationLayer(plugin_dir=str(tmp_path))
        supervisor = MagicMock()
        supervisor.start_all = AsyncMock()
        supervisor.stop_one = AsyncMock(return_value=False)
        supervisor.start_one = AsyncMock(return_value=True)
        layer._supervisor = supervisor
        return layer, supervisor

    def test_start_does_not_spawn_in_process_plugins(self, tmp_path):
        """model_adapter 插件不进 start_all——占位 entry 不是子进程入口，
        spawn 了会立刻退出进入重试熔断循环。"""
        _write_plugin(tmp_path, "fake-adapter", "model_adapter")
        _write_plugin(tmp_path, "a-sink", "output_sink")
        layer, supervisor = self._make_layer(tmp_path)

        async def go():
            await layer.start()

        asyncio.new_event_loop().run_until_complete(go())
        spawned = supervisor.start_all.call_args[0][0]
        assert [m.id for m in spawned] == ["a-sink"]

    def test_restart_in_process_plugin_never_spawns(self, tmp_path):
        _write_plugin(tmp_path, "fake-adapter", "model_adapter")
        layer, supervisor = self._make_layer(tmp_path)

        async def go():
            assert await layer.restart_subprocess_plugin("fake-adapter") is True

        asyncio.new_event_loop().run_until_complete(go())
        supervisor.start_one.assert_not_called()

    def test_enable_in_process_plugin_never_spawns(self, tmp_path, monkeypatch):
        """管理页「启用」toggle 对进程内插件不得 spawn——占位 entry spawn 后
        立刻退出，握手按 rpc_timeout×(max_restarts+1) 重试熔断，请求挂数十秒，
        表现为禁用后再也启用不了（连点还会翻回禁用）。"""
        _write_plugin(tmp_path, "fake-adapter", "model_adapter")
        layer, supervisor = self._make_layer(tmp_path)
        # set_plugin_enabled 会写真实 config.json 并重扫真实插件目录，测试拦截
        monkeypatch.setattr("app.integration.config_helper.set_plugin_disabled",
                            lambda pid, disabled: [])
        monkeypatch.setattr("app.agents.model_family_adapters.refresh_plugin_adapters",
                            lambda *a, **k: 0)

        async def go():
            assert await layer.start_plugin("fake-adapter") is True

        asyncio.new_event_loop().run_until_complete(go())
        supervisor.start_one.assert_not_called()


class _FakeAdapter:
    """测试用假适配器——不 import 插件体系，直接验证节点调用契约。"""

    family = "fake"

    def no_think(self, system_text: str, user_text: str) -> tuple[str, str]:
        return f"{system_text}/SW", f"{user_text}/SW"


class TestDispatcherFamilyNode:
    """Dispatcher._inject_family_switch 是宿主侧唯一节点，须零家族特判。"""

    @staticmethod
    def _messages() -> list:
        return [
            SystemMessage(content="你是助手。"),
            HumanMessage(content="历史问题"),
            AIMessage(content="历史回答"),
            HumanMessage(content="开灯"),
        ]

    def test_hit_adapter_rewrites_system_and_last_only(self, monkeypatch):
        monkeypatch.setattr("app.agents.dispatcher.get_adapter",
                            lambda model: _FakeAdapter() if "fake" in model else None)
        from app.agents.dispatcher import Dispatcher
        msgs = self._messages()
        Dispatcher._inject_family_switch(Dispatcher.__new__(Dispatcher),
                                         msgs, "fake-model")
        assert msgs[0].content == "你是助手。/SW"
        assert msgs[1].content == "历史问题"          # 中间历史不动
        assert msgs[2].content == "历史回答"
        assert msgs[-1].content == "开灯/SW"

    def test_no_adapter_noop(self, monkeypatch):
        monkeypatch.setattr("app.agents.dispatcher.get_adapter",
                            lambda model: None)
        from app.agents.dispatcher import Dispatcher
        msgs = self._messages()
        Dispatcher._inject_family_switch(Dispatcher.__new__(Dispatcher),
                                         msgs, "glm-4-flash")
        assert msgs[0].content == "你是助手。"
        assert msgs[-1].content == "开灯"

    def test_retry_round_only_touches_last_message(self, monkeypatch):
        """include_system=False（重试轮）：system 不叠加开关，只补最后一条。"""
        monkeypatch.setattr("app.agents.dispatcher.get_adapter",
                            lambda model: _FakeAdapter())
        from app.agents.dispatcher import Dispatcher
        msgs = self._messages()
        d = Dispatcher.__new__(Dispatcher)
        d._inject_family_switch(msgs, "fake-model")
        msgs.append(HumanMessage(content="重试：上一步失败了"))
        d._inject_family_switch(msgs, "fake-model", include_system=False)
        assert msgs[0].content == "你是助手。/SW"      # 只注入一次，无叠加
        assert msgs[-1].content == "重试：上一步失败了/SW"

    def test_adapter_lookup_exception_swallowed(self, monkeypatch):
        """注册表查询异常不影响主流程（静默跳过注入）。"""
        def boom(model):
            raise RuntimeError("registry broken")
        monkeypatch.setattr("app.agents.dispatcher.get_adapter", boom)
        from app.agents.dispatcher import Dispatcher
        msgs = self._messages()
        Dispatcher._inject_family_switch(Dispatcher.__new__(Dispatcher),
                                         msgs, "fake-model")
        assert msgs[-1].content == "开灯"


@pytest.mark.parametrize("src_file", [
    Path("app/agents/dispatcher.py"),
    Path("app/agents/model_family_adapters.py"),
    Path("app/integration/integration_layer.py"),
    Path("app/integration/schema.py"),
])
def test_host_source_has_no_family_hardcoding(src_file):
    """守护解耦约束：宿主节点源码不得出现具体模型家族名/家族专属开关字面量。

    家族名、思考开关标记（如 /no_think）只能出现在 integrations/<id>/ 插件里。
    通用契约名 no_think（钩子方法名）不属于家族硬编码。
    """
    root = Path(__file__).resolve().parent.parent
    text = (root / src_file).read_text(encoding="utf-8")
    for banned in ("qwen", "/no_think"):
        assert banned not in text.lower(), (
            f"{src_file} 出现家族硬编码 {banned!r}——家族行为应放 model_adapter 插件")
