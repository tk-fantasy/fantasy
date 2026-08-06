"""插件导出/上传/删除路由测试。

用临时目录模拟 integrations/，验证 zip 打包、校验、解压、删除。
不依赖真实运行中的 IntegrationLayer（部分路由纯文件操作）。
"""

import asyncio
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch


def _make_manifest_bytes(plugin_id="testplug"):
    return json.dumps({
        "id": plugin_id, "name": "测试", "version": "1.0.0",
        "aether_api_version": "1", "entry": "plugin.py",
        "capabilities": [{"type": "output_sink", "id": "t1"}],
    }).encode("utf-8")


def _make_plugin_zip(plugin_id="testplug", include_entry=True):
    """构造一个合法插件 zip（内存）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", _make_manifest_bytes(plugin_id))
        if include_entry:
            zf.writestr("plugin.py", b"print('hello')\n")
    buf.seek(0)
    return buf


def test_export_plugin_packs_zip(tmp_path, monkeypatch):
    """export 把插件目录打包成 zip。"""
    # 造一个插件目录
    plugin_root = tmp_path / "integrations"
    (plugin_root / "testplug").mkdir(parents=True)
    (plugin_root / "testplug" / "manifest.json").write_bytes(_make_manifest_bytes())
    (plugin_root / "testplug" / "plugin.py").write_text("print('hi')")

    monkeypatch.setattr("app.routes.integration_routes.get_config",
                        lambda path, default=None: str(plugin_root) if path == "integration.plugin_dir" else default)

    from app.routes.integration_routes import export_plugin
    result = asyncio.new_event_loop().run_until_complete(export_plugin("testplug"))
    # StreamingResponse
    assert result.media_type == "application/zip"
    # 读 zip 内容验证
    buf = io.BytesIO()
    async def collect():
        async for chunk in result.body_iterator:
            buf.write(chunk)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(collect())
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        assert "manifest.json" in zf.namelist()
        assert "plugin.py" in zf.namelist()


def test_export_nonexistent_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routes.integration_routes.get_config",
                        lambda path, default=None: str(tmp_path) if path == "integration.plugin_dir" else default)
    from app.routes.integration_routes import export_plugin
    result = asyncio.new_event_loop().run_until_complete(export_plugin("nope"))
    assert result["success"] is False


def test_upload_valid_plugin_extracts(tmp_path, monkeypatch):
    """合法 zip 上传后解压到 integrations/。"""
    plugin_root = tmp_path / "integrations"
    plugin_root.mkdir()
    monkeypatch.setattr("app.routes.integration_routes.get_config",
                        lambda path, default=None: str(plugin_root) if path == "integration.plugin_dir" else default)

    from app.routes.integration_routes import upload_plugin

    class FakeUpload:
        async def read(self):
            return _make_plugin_zip("newplug").read()

    result = asyncio.new_event_loop().run_until_complete(upload_plugin(file=FakeUpload()))
    assert result["success"] is True
    assert result["data"]["id"] == "newplug"
    assert (plugin_root / "newplug" / "manifest.json").exists()
    assert (plugin_root / "newplug" / "plugin.py").exists()


def test_upload_missing_manifest_rejected(tmp_path, monkeypatch):
    """zip 内无 manifest.json 被拒。"""
    plugin_root = tmp_path / "integrations"
    plugin_root.mkdir()
    monkeypatch.setattr("app.routes.integration_routes.get_config",
                        lambda path, default=None: str(plugin_root) if path == "integration.plugin_dir" else default)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("random.txt", b"no manifest here")
    buf.seek(0)

    from app.routes.integration_routes import upload_plugin

    class FakeUpload:
        async def read(self):
            return buf.read()

    result = asyncio.new_event_loop().run_until_complete(upload_plugin(file=FakeUpload()))
    assert result["success"] is False
    assert "manifest" in result["message"]


def test_upload_missing_entry_rejected(tmp_path, monkeypatch):
    """manifest 声明的 entry 文件不在 zip 内被拒。"""
    plugin_root = tmp_path / "integrations"
    plugin_root.mkdir()
    monkeypatch.setattr("app.routes.integration_routes.get_config",
                        lambda path, default=None: str(plugin_root) if path == "integration.plugin_dir" else default)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", _make_manifest_bytes())  # entry=plugin.py 但没放
    buf.seek(0)

    from app.routes.integration_routes import upload_plugin

    class FakeUpload:
        async def read(self):
            return buf.read()

    result = asyncio.new_event_loop().run_until_complete(upload_plugin(file=FakeUpload()))
    assert result["success"] is False
    assert "入口" in result["message"] or "entry" in result["message"].lower()


def test_upload_duplicate_rejected(tmp_path, monkeypatch):
    """同名插件已存在被拒。"""
    plugin_root = tmp_path / "integrations"
    (plugin_root / "testplug").mkdir(parents=True)
    (plugin_root / "testplug" / "manifest.json").write_bytes(_make_manifest_bytes())
    monkeypatch.setattr("app.routes.integration_routes.get_config",
                        lambda path, default=None: str(plugin_root) if path == "integration.plugin_dir" else default)

    from app.routes.integration_routes import upload_plugin

    class FakeUpload:
        async def read(self):
            return _make_plugin_zip("testplug").read()  # 同名

    result = asyncio.new_event_loop().run_until_complete(upload_plugin(file=FakeUpload()))
    assert result["success"] is False
    assert "已存在" in result["message"]


def test_upload_invalid_id_rejected(tmp_path, monkeypatch):
    """manifest.id 含路径穿越字符（如 ../）被拒。"""
    plugin_root = tmp_path / "integrations"
    plugin_root.mkdir()
    monkeypatch.setattr("app.routes.integration_routes.get_config",
                        lambda path, default=None: str(plugin_root) if path == "integration.plugin_dir" else default)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "id": "../evil", "name": "x", "version": "1", "aether_api_version": "1",
            "entry": "plugin.py", "capabilities": [],
        }).encode())
        zf.writestr("plugin.py", b"x")
    buf.seek(0)

    from app.routes.integration_routes import upload_plugin

    class FakeUpload:
        async def read(self):
            return buf.read()

    result = asyncio.new_event_loop().run_until_complete(upload_plugin(file=FakeUpload()))
    assert result["success"] is False
    assert "非法" in result["message"] or "illegal" in result["message"].lower()


def test_delete_plugin_removes_dir(tmp_path, monkeypatch):
    """删除插件移除目录。"""
    plugin_root = tmp_path / "integrations"
    (plugin_root / "gone").mkdir(parents=True)
    (plugin_root / "gone" / "manifest.json").write_bytes(_make_manifest_bytes("gone"))
    monkeypatch.setattr("app.routes.integration_routes.get_config",
                        lambda path, default=None: str(plugin_root) if path == "integration.plugin_dir" else default)

    from unittest.mock import MagicMock
    container = MagicMock()
    container.integration_layer = None  # 未运行

    from app.routes.integration_routes import delete_plugin
    result = asyncio.new_event_loop().run_until_complete(
        delete_plugin("gone", container=container)
    )
    assert result["success"] is True
    assert not (plugin_root / "gone").exists()
