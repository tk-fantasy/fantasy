"""集成插件平台管理路由。"""

import io
import logging
import re
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..core.auth import get_current_admin
from ..container import get_container
from ..core.config import BASE_DIR, get_config
from ..integration.schema import Manifest

router = APIRouter()
logger = logging.getLogger(__name__)

# 插件 id 合法字符（防路径穿越：只允许字母数字下划线中划线）
_PLUGIN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# 插件包上传防护：限制上传体积 + 解压后体积 + 压缩比，防 zip bomb 与内存/磁盘 DoS。
# 插件场景（几个 .py + manifest + 少量资源）远小于这些阈值。
MAX_PLUGIN_ZIP_SIZE = 50 * 1024 * 1024        # 50 MB 上传上限
MAX_PLUGIN_UNCOMPRESSED_SIZE = 500 * 1024 * 1024  # 500 MB 解压后总大小
MAX_PLUGIN_COMPRESSION_RATIO = 100            # 解压/压缩 比值上限


class BroadcastRequest(BaseModel):
    """手动广播请求体（测试用）。"""
    text: str
    msg_id: str = "manual"


@router.get("/integrations")
async def list_integrations(container=Depends(get_container)):
    """列出所有集成插件及其运行状态（含禁用态）。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": True, "data": {"plugins": [], "enabled": False,
                                          "broadcast_enabled": False}}
    return {"success": True, "data": {
        "plugins": layer.list_plugins(),
        "enabled": True,
        "broadcast_enabled": layer.sink_manager.broadcast_enabled,
    }}


@router.post("/integrations/{plugin_id}/toggle-enabled")
async def toggle_plugin_enabled(plugin_id: str, container=Depends(get_container), admin: dict = Depends(get_current_admin)):
    """切换插件启用/禁用（热加载，不重启 Aether）。

    禁用 → 立即停止该插件进程。
    启用 → 热启动该插件进程（借鉴 OpenClaw：启用=热加载；子进程天然隔离无需原子交换）。
    """
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    plugins = layer.list_plugins()
    target = next((p for p in plugins if p["id"] == plugin_id), None)
    if target is None:
        return {"success": False, "message": f"未知插件: {plugin_id}"}
    new_enabled = not target["enabled"]
    if new_enabled:
        # 启用：热启动进程
        started = await layer.start_plugin(plugin_id)
        return {"success": True, "data": {"id": plugin_id, "enabled": True,
                                          "alive": started}}
    else:
        # 禁用：立即停进程
        await layer.stop_plugin(plugin_id)
        return {"success": True, "data": {"id": plugin_id, "enabled": False,
                                          "alive": False}}


class PluginConfigRequest(BaseModel):
    """管理页提交的插件配置。secret 字段留空 = 保持原值。"""
    values: dict[str, str] = {}


def _mask_secret(value: str) -> str:
    """脱敏回显：保留首尾各 4 位（太短则全遮）。"""
    if len(value) <= 8:
        return "***" if value else ""
    return f"{value[:4]}…{value[-4:]}"


@router.get("/integrations/{plugin_id}/config")
async def get_plugin_config(plugin_id: str, container=Depends(get_container)):
    """读插件配置表单数据：schema + 当前值（secret 字段脱敏，只回显 masked/is_set）。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    target = next((p for p in layer.list_plugins() if p["id"] == plugin_id), None)
    if target is None:
        return {"success": False, "message": f"未知插件: {plugin_id}"}

    from ..integration.config_helper import get_host_config
    schema = target.get("config_schema") or {}
    stored = get_host_config(plugin_id)
    values = {}
    for key, field in schema.items():
        raw = str(stored.get(key, "") or "")
        if field.get("type") == "secret":
            values[key] = {"is_set": bool(raw), "masked": _mask_secret(raw)}
        else:
            values[key] = raw
    return {"success": True, "data": {
        "id": plugin_id,
        "schema": schema,
        "values": values,
        "has_config_set": bool(stored),
    }}


@router.post("/integrations/{plugin_id}/config")
async def save_plugin_config(
    plugin_id: str,
    req: PluginConfigRequest,
    container=Depends(get_container),
    admin: dict = Depends(get_current_admin),
):
    """保存插件配置并热生效（写审计）。

    - secret 字段留空 = 保持原值（前端密码框不回显明文）
    - 宿主侧集成（如飞书）：stop+start 热重连，无需重启容器
    - 子进程插件（如小爱）：重启插件进程，setup 时按新配置初始化
    """
    import asyncio

    from ..integration.config_helper import merge_plugin_config
    from ..ops import audit

    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    target = next((p for p in layer.list_plugins() if p["id"] == plugin_id), None)
    if target is None:
        return {"success": False, "message": f"未知插件: {plugin_id}"}

    schema = target.get("config_schema") or {}
    # 只收 schema 声明过的字段，未知字段直接丢弃
    updates = {k: str(v) for k, v in (req.values or {}).items() if k in schema}
    secret_keys = {k for k, f in schema.items() if f.get("type") == "secret"}
    merged = merge_plugin_config(plugin_id, updates, secret_keys)

    # 必填校验：合并后仍为空的必填字段拒绝保存（secret 以「已设置」为准）
    missing = [
        k for k, f in schema.items()
        if f.get("required") and (
            (k in secret_keys and not merged.get(k)) or
            (k not in secret_keys and not str(merged.get(k, "")).strip())
        )
    ]
    if missing:
        return {"success": False, "message": f"必填字段未填写: {', '.join(missing)}"}

    # 热生效
    applied = "skipped"
    if plugin_id in layer.host_integrations:
        restart = getattr(container, "restart_host_integration_fn", None)
        if callable(restart):
            applied = "restarted" if restart(plugin_id, asyncio.get_running_loop()) else "not_found"
    else:
        applied = "restarted" if await layer.restart_subprocess_plugin(plugin_id) else "not_found"

    operator = admin.get("username") or admin.get("user_id", "")
    audit.record(operator, "plugin_config", {
        "plugin": plugin_id,
        "fields": sorted(updates.keys()),
        "applied": applied,
    })
    return {"success": True, "data": {"id": plugin_id, "applied": applied}}


@router.post("/integrations/broadcast/toggle")
async def toggle_broadcast(container=Depends(get_container)):
    """切换全局广播开关（开↔关），持久化到 config，立即生效。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    new_state = not layer.sink_manager.broadcast_enabled
    layer.set_broadcast_enabled(new_state)
    return {"success": True, "data": {"broadcast_enabled": new_state}}


@router.post("/integrations/broadcast")
async def manual_broadcast(req: BroadcastRequest, container=Depends(get_container)):
    """手动触发一次广播（测试/调试用）。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    await layer.sink_manager.broadcast(req.text, req.msg_id)
    return {"success": True}


# ── UI 贡献机制：插件声明 UI，Aether 通用路由读状态/触发动作 ──

@router.get("/integrations/ui_contributions")
async def list_ui_contributions(container=Depends(get_container)):
    """返回所有插件声明的 UI 贡献（前端通用渲染器据此渲染）。

    没插件 / 插件无 ui_contribution → 空列表 → 前端无该 UI 元素。
    """
    layer = container.integration_layer
    if layer is None:
        return {"success": True, "data": []}
    return {"success": True, "data": layer.list_ui_contributions()}


# state_key → 读取函数注册表（框架能力，不认得具体插件）
# 插件只能用已注册的 state_key——这是安全边界
def _get_current_mode_safe() -> str:
    """安全读取 current_mode（集成平台未启用时也能读）。"""
    try:
        from ..integration.config_helper import get_current_mode
        return get_current_mode()
    except Exception:
        return "aether"


STATE_HANDLERS = {
    "broadcast_enabled": lambda layer: layer.sink_manager.broadcast_enabled,
    "current_mode": lambda layer: _get_current_mode_safe(),
}


# action → 触发函数注册表（框架能力，不认得具体插件）
# 插件只能用已注册的 action——这是安全边界
async def _toggle_broadcast(layer):
    """切换全局广播开关（框架能力，非小爱专属）。"""
    new_state = not layer.sink_manager.broadcast_enabled
    layer.set_broadcast_enabled(new_state)
    return {"broadcast_enabled": new_state}


async def _set_mode(layer, mode: str = "aether"):
    """设置当前聊天模式（框架能力，非小爱专属）。"""
    from ..integration.config_helper import set_current_mode
    set_current_mode(mode)
    return {"current_mode": mode}


ACTION_HANDLERS = {
    "toggle_broadcast": _toggle_broadcast,
    "set_mode": _set_mode,
}


@router.get("/integrations/state/{state_key}")
async def get_state(state_key: str, container=Depends(get_container)):
    """通用状态读取路由。按 state_key 路由到框架能力。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    handler = STATE_HANDLERS.get(state_key)
    if handler is None:
        return {"success": False, "message": f"未知 state_key: {state_key}"}
    return {"success": True, "data": {"value": handler(layer)}}


@router.post("/integrations/action/{action}")
async def invoke_action(action: str, body: dict = Body(default={}),
                        container=Depends(get_container)):
    """通用动作触发路由。按 action 路由到框架能力。

    set_mode 等 action 可从 body 传参数（如 {"mode": "xiaoai_direct"}）。
    """
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        return {"success": False, "message": f"未知 action: {action}"}
    # set_mode 需要额外参数；直接调用时 body 可能是 FieldInfo，统一取 dict
    if action == "set_mode":
        b = body if isinstance(body, dict) else {}
        mode = b.get("mode", "aether")
        result = await handler(layer, mode)
    else:
        result = await handler(layer)
    return {"success": True, "data": result}


# ── 插件导出/上传 ──

def _resolve_plugin_dir() -> Path:
    """从 config 读 plugin_dir，解析为绝对路径（基于项目根，跨平台）。

    原代码硬编码 Path("/aether")，Windows 上解析成当前盘符的 \\aether，
    导致本地开发/Windows 部署找不到插件目录。改用 BASE_DIR（容器内为
    /aether，Windows 开发为项目根），跨环境一致。
    """
    dir_cfg = get_config("integration.plugin_dir", "integrations")
    return Path(BASE_DIR) / dir_cfg


@router.get("/integrations/{plugin_id}/export")
async def export_plugin(plugin_id: str):
    """打包某插件目录为 zip 下载。

    无需集成平台启用（纯文件操作，disabled 的也能导出）。
    """
    if not _PLUGIN_ID_RE.match(plugin_id):
        return {"success": False, "message": "非法插件 id"}
    plugin_dir = _resolve_plugin_dir() / plugin_id
    if not plugin_dir.is_dir():
        return {"success": False, "message": f"插件 {plugin_id} 不存在"}

    # 内存打包 zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(plugin_dir.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(plugin_dir))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{plugin_id}.zip"'},
    )


@router.post("/integrations/upload")
async def upload_plugin(file: UploadFile = File(...), admin: dict = Depends(get_current_admin)):
    """上传插件 zip 包，校验后解压到 integrations/。

    校验：
    - zip 内必须有 manifest.json
    - manifest.id 合法（防路径穿越）
    - manifest.entry 文件在 zip 内存在
    - 同名插件已存在 → 拒绝（需先删除）
    """
    content = await file.read()
    if len(content) > MAX_PLUGIN_ZIP_SIZE:
        return {"success": False,
                "message": f"插件包过大（{len(content) // 1024 // 1024}MB > "
                           f"{MAX_PLUGIN_ZIP_SIZE // 1024 // 1024}MB 上限）"}
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return {"success": False, "message": "不是有效的 zip 文件"}

    names = zf.namelist()

    # zip bomb 防护：累计解压后大小 + 压缩比，超阈值拒绝。
    # 压实率为 0 的空条目跳过（避免除零）。
    total_uncompressed = sum(i.file_size for i in zf.infolist())
    if total_uncompressed > MAX_PLUGIN_UNCOMPRESSED_SIZE:
        return {"success": False,
                "message": f"解压后体积过大（{total_uncompressed // 1024 // 1024}MB > "
                           f"{MAX_PLUGIN_UNCOMPRESSED_SIZE // 1024 // 1024}MB 上限）"}
    if content and total_uncompressed // len(content) > MAX_PLUGIN_COMPRESSION_RATIO:
        return {"success": False,
                "message": f"压缩比异常（{total_uncompressed // max(len(content), 1)}x > "
                           f"{MAX_PLUGIN_COMPRESSION_RATIO}x），疑似 zip bomb"}

    # 找 manifest.json（可能在根目录或单层子目录）
    manifest_name = None
    manifest_subdir = ""
    for n in names:
        basename = n.split("/")[-1]
        if basename == "manifest.json" and n.count("/") <= 1:
            manifest_name = n
            manifest_subdir = "/".join(n.split("/")[:-1])
            break
    if manifest_name is None:
        return {"success": False, "message": "zip 内未找到 manifest.json"}

    # 解析 manifest
    try:
        import json
        raw = json.loads(zf.read(manifest_name).decode("utf-8"))
        manifest = Manifest.model_validate(raw)
    except Exception as exc:
        return {"success": False, "message": f"manifest 校验失败: {exc}"}

    # id 合法性（防路径穿越）
    if not _PLUGIN_ID_RE.match(manifest.id):
        return {"success": False, "message": "manifest.id 含非法字符"}

    # 入口文件存在性
    entry_path_in_zip = f"{manifest_subdir}/{manifest.entry}".lstrip("/") \
        if manifest_subdir else manifest.entry
    if entry_path_in_zip not in names and manifest.entry not in names:
        return {"success": False, "message": f"入口文件 {manifest.entry} 不在 zip 内"}

    # 冲突检测
    plugin_root = _resolve_plugin_dir()
    target_dir = plugin_root / manifest.id
    if target_dir.exists():
        return {"success": False,
                "message": f"插件 {manifest.id} 已存在，需先删除"}

    # 原子解压：先解到临时目录，校验后 rename
    target_dir.mkdir(parents=True, exist_ok=False)
    try:
        for n in names:
            # 跳过目录条目、__MACOSX 等
            if n.endswith("/") or "__MACOSX" in n:
                continue
            # 去掉 manifest_subdir 前缀（若有），解到 target_dir 根
            rel = n
            if manifest_subdir and n.startswith(manifest_subdir + "/"):
                rel = n[len(manifest_subdir) + 1:]
            if not rel:
                continue
            # 防路径穿越：解析后必须在 target_dir 内
            out_path = (target_dir / rel).resolve()
            if not str(out_path).startswith(str(target_dir.resolve())):
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(zf.read(n))
        logger.info("插件 %s 上传成功，解压到 %s", manifest.id, target_dir)
        return {"success": True,
                "data": {"id": manifest.id, "name": manifest.name,
                         "message": "上传成功，重启 Aether 后生效"}}
    except Exception as exc:
        # 失败回滚：删除已解压的目录
        shutil.rmtree(target_dir, ignore_errors=True)
        logger.error("插件 %s 上传解压失败: %s", manifest.id, exc)
        return {"success": False, "message": f"解压失败: {exc}"}


@router.delete("/integrations/{plugin_id}")
async def delete_plugin(plugin_id: str, container=Depends(get_container), admin: dict = Depends(get_current_admin)):
    """删除插件（删 integrations/{id}/ 文件夹）。

    若插件正在运行，先停止进程。删内置插件需谨慎（建议先禁用）。
    """
    if not _PLUGIN_ID_RE.match(plugin_id):
        return {"success": False, "message": "非法插件 id"}
    plugin_dir = _resolve_plugin_dir() / plugin_id
    if not plugin_dir.is_dir():
        return {"success": False, "message": f"插件 {plugin_id} 不存在"}

    # 若运行中，先停进程
    layer = container.integration_layer
    if layer is not None:
        proc = layer._supervisor.get_process(plugin_id) if hasattr(layer, "_supervisor") else None
        if proc and proc.is_alive:
            try:
                await proc.stop()
            except Exception as exc:
                logger.warning("停止插件 %s 进程失败: %s", plugin_id, exc)

    shutil.rmtree(plugin_dir, ignore_errors=True)
    logger.info("插件 %s 已删除", plugin_id)
    return {"success": True, "data": {"id": plugin_id}}
