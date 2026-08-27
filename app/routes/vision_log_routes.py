"""识别日志路由：VL 识别留痕查询（测试插件面板 / 运维诊断用）+ 服务器目录浏览。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from ..core.api_models import ApiResponse
from ..core.auth import get_current_admin
from ..core.database import Database
from ..core.exceptions import AppException

router = APIRouter()


@router.get("/vision-logs")
async def list_vision_logs(
    camera_id: str = "",
    kind: str = "",
    limit: int = 100,
    admin: dict = Depends(get_current_admin),
):
    """最近识别日志（新在前）。kind: preview(预览分类) / rule_eval(规则判定) / action(动作执行/演练)。"""
    db = Database.get()
    rows = await db.vision_logs_tail(camera_id=camera_id, kind=kind, limit=limit)
    return ApiResponse(data=rows)


@router.delete("/vision-logs")
async def clear_vision_logs(
    camera_id: str = "",
    admin: dict = Depends(get_current_admin),
):
    """清空识别日志（可按 camera_id 过滤）。"""
    db = Database.get()
    deleted = await db.vision_logs_delete_camera(camera_id)
    return ApiResponse(data={"deleted": deleted})


# —— 服务器目录浏览（测试插件面板的文件选择器）——
# 只读列目录 + 按扩展名过滤，管理员鉴权。供"从本地选视频"的前端选择器用
# （浏览器安全模型拿不到本地绝对路径，必须由服务端列目录来选）。

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg"}
_DIR_ENTRY_LIMIT = 500


def _scan_dir_sync(root: Path, want_exts: set[str]) -> tuple[list[dict], list[dict]]:
    """在工作线程里跑的目录扫描（CPU/IO 均可能慢，禁止直接在事件循环调用）。"""
    dirs: list[dict] = []
    files: list[dict] = []
    for entry in root.iterdir():
        if len(dirs) + len(files) >= _DIR_ENTRY_LIMIT:
            break
        try:
            if entry.is_dir() and not entry.name.startswith("$"):
                dirs.append({"name": entry.name, "path": str(entry), "type": "dir"})
            elif entry.is_file():
                if entry.suffix.lower().lstrip(".") in want_exts:
                    files.append({
                        "name": entry.name, "path": str(entry), "type": "file",
                        "size": entry.stat().st_size,
                    })
        except OSError:
            continue  # 无权限/失效符号链接等
    return dirs, files


@router.get("/files/browse")
async def browse_files(
    path: str = Query(default="", description="目录绝对路径；空=列出盘符/根"),
    exts: str = Query(default="", description="逗号分隔扩展名过滤；空=默认视频扩展名"),
    admin: dict = Depends(get_current_admin),
):
    """列目录：子目录 + 匹配扩展名的文件（带大小）。path 为空列 Windows 盘符或 / 根。"""
    if not path.strip():
        # 盘符枚举用 GetLogicalDrives 位掩码：瞬时返回、不触碰介质。绝不能用
        # os.path.isdir 逐盘符探测——断连的网络映射盘会让每次调用阻塞几十秒
        # （SMB 超时），且发生在事件循环线程上，整个后端随之冻结。
        drives = []
        if os.name == "nt":
            import ctypes
            import string
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i, letter in enumerate(string.ascii_uppercase):
                if mask & (1 << i):
                    drive = f"{letter}:\\"
                    drives.append({"name": drive, "path": drive, "type": "dir"})
        else:
            drives = [{"name": "/", "path": "/", "type": "dir"}]
        return ApiResponse(data={"path": "", "entries": drives})

    root = Path(path.strip()).resolve()
    if not await asyncio.to_thread(root.is_dir):
        raise AppException(f"不是有效目录: {root}", code="invalid_path", http_status=400)

    want_exts = ({e.strip().lower() for e in exts.split(",") if e.strip().strip(".")}
                 if exts.strip() else {e.lstrip(".") for e in VIDEO_EXTS})

    # 扫描放工作线程：目标可能是网络盘/慢介质，别占着事件循环
    try:
        dirs, files = await asyncio.to_thread(_scan_dir_sync, root, want_exts)
    except OSError as exc:
        raise AppException(f"读取目录失败: {exc}", code="read_failed", http_status=400)

    # 目录在前，各自按名字排序，方便面板呈现
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    parent = str(root.parent) if str(root.parent) != str(root) else ""
    return ApiResponse(data={
        "path": str(root),
        "parent": parent,
        "entries": dirs + files,
    })
