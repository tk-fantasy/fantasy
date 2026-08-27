"""integrations 下无测试引用的小模块补测：

- integrations/feishu/meta.py —— 插件管理页展示/配置表单声明
- integrations/qwen-adapter/adapters.py —— Qwen 家族 /no_think 适配器

qwen-adapter 目录名带连字符无法作为包导入（宿主也是按文件路径
importlib 加载的，见 model_family_adapters._load_adapters_module），
这里用同样的方式按路径装载。
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_feishu_meta():
    return _load_module("feishu_meta_under_test", ROOT / "integrations" / "feishu" / "meta.py")


def _load_qwen_adapters():
    return _load_module(
        "qwen_adapters_under_test", ROOT / "integrations" / "qwen-adapter" / "adapters.py"
    )


class TestFeishuMeta:
    """飞书插件元信息：管理页与宿主加载都依赖这些声明。"""

    def test_identity_fields(self):
        meta = _load_feishu_meta()
        assert meta.NAME == "飞书机器人"
        assert isinstance(meta.VERSION, str) and meta.VERSION
        assert "host_integration" in meta.CAPABILITIES

    def test_config_schema_fields_have_type_and_label(self):
        schema = _load_feishu_meta().CONFIG_SCHEMA
        for key, spec in schema.items():
            assert spec.get("type") in {"string", "secret"}, f"{key} type 非法"
            assert spec.get("label"), f"{key} 缺 label"

    def test_auth_required_fields_marked_required_secret(self):
        schema = _load_feishu_meta().CONFIG_SCHEMA
        # 连接飞书至少要 app_id/app_secret；secret 字段由管理页脱敏回显
        assert schema["app_id"]["required"] is True
        assert schema["app_secret"]["required"] is True
        assert schema["app_secret"]["type"] == "secret"


class TestQwenAdapter:
    """Qwen 家族适配器：模型名匹配 + /no_think 双注入。"""

    def test_family_and_registry(self):
        from app.agents.model_family_adapters import ModelFamilyAdapter

        mod = _load_qwen_adapters()
        adapter = mod.ADAPTERS[0]
        assert isinstance(adapter, ModelFamilyAdapter)
        assert adapter.family == "qwen"

    def test_matches_model_names(self):
        qwen_cls = type(_load_qwen_adapters().ADAPTERS[0])

        assert qwen_cls.matches("qwen3.8-27b-mlx") is True
        assert qwen_cls.matches("Qwen2.5-7B-Instruct") is True  # 大小写不敏感
        assert qwen_cls.matches("llama-3-8b") is False
        assert qwen_cls.matches("") is False

    def test_no_think_injected_into_both_texts(self):
        adapter = _load_qwen_adapters().ADAPTERS[0]

        sys_text, user_text = adapter.no_think("你是助手", "你好")
        assert sys_text.endswith("/no_think")
        assert user_text.endswith("/no_think")
        assert "你是助手" in sys_text and "你好" in user_text
