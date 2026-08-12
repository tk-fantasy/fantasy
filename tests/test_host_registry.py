"""HostMethodRegistry 单测：注册 / dispatch / 未知方法 / 权限校验。"""

import types

import pytest

from app.integration.host_registry import HostMethodRegistry


def _manifest(perms):
    """registry 只读 manifest.id 与 manifest.permissions，用 SimpleNamespace 即可。"""
    return types.SimpleNamespace(id="p1", permissions=perms)


@pytest.mark.asyncio
async def test_dispatch_calls_handler_and_returns_result():
    reg = HostMethodRegistry()

    async def handler(params):
        return {"echo": params}

    reg.register("foo", handler)
    result = await reg.dispatch(_manifest([]), "foo", {"x": 1})
    assert result == {"echo": {"x": 1}}


@pytest.mark.asyncio
async def test_unknown_method_raises_runtime_error():
    reg = HostMethodRegistry()
    with pytest.raises(RuntimeError):
        await reg.dispatch(_manifest([]), "missing", {})


@pytest.mark.asyncio
async def test_permission_denied_when_manifest_lacks_perm():
    reg = HostMethodRegistry()

    async def handler(params):  # 不应被调用
        return {"ok": True}

    reg.register("ha.call_service", handler, required_permission="ha")
    with pytest.raises(PermissionError):
        await reg.dispatch(_manifest([]), "ha.call_service", {})


@pytest.mark.asyncio
async def test_permission_allowed_when_manifest_has_perm():
    reg = HostMethodRegistry()

    async def handler(params):
        return {"ok": True}

    reg.register("ha.call_service", handler, required_permission="ha")
    result = await reg.dispatch(_manifest(["ha"]), "ha.call_service", {})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_no_required_permission_allows_any_manifest():
    reg = HostMethodRegistry()

    async def handler(params):
        return {"ok": True}

    reg.register("free", handler, required_permission=None)
    result = await reg.dispatch(_manifest([]), "free", {})
    assert result == {"ok": True}
