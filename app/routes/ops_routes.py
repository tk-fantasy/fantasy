"""运维路由（09 清单条目 1 + 运维页 /operations 的全部后端）。

端点一览（均在 /api 下，经 api_token_guard 要求 JWT；写操作叠加 get_current_user）：
- GET  /api/ops/diagnostics      下载脱敏诊断包 zip（操作写审计）
- POST /api/ops/diagnose         跑部署体检，返回结构化报告
- GET  /api/ops/audit            最近运维审计记录
- GET  /api/ops/version          当前版本 + 升级历史
- POST /api/ops/backups          立即备份（应用侧，保留 3 份）
- GET  /api/ops/backups          备份列表
- DELETE /api/ops/backups/{name} 删除备份
- GET  /api/ops/backups/{name}/validate  恢复前预检（内容清单）
- POST /api/ops/backups/{name}/restore   恢复并自动重启（需 confirm=true）
- POST /api/ops/upgrade          上传升级包（multipart），校验+load+自重启
"""
from __future__ import annotations

import logging
import shutil

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.version import get_version
from ..ops import audit, backup, diagnose, upgrade
from ..ops.diag import build_diagnostic_package

logger = logging.getLogger(__name__)

router = APIRouter()


class RestoreRequest(BaseModel):
    confirm: bool = False


@router.get("/ops/diagnostics")
async def export_diagnostics(
    current_user: dict = Depends(get_current_user),
) -> Response:
    data, filename = await build_diagnostic_package(
        operator=current_user.get("username") or current_user["user_id"]
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/ops/audit")
async def recent_audit(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[list[dict]]:
    return ApiResponse(data=audit.tail(limit=50))


# ==================== 部署体检 ====================

@router.post("/ops/diagnose")
async def run_diagnose(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    audit.record(current_user.get("username") or current_user["user_id"], "diagnose_run", {})
    # 检查内部自带并行与超时（run_all 在线程池里跑阻塞 IO，放 executor 避免卡事件循环）
    import asyncio

    report = await asyncio.get_running_loop().run_in_executor(None, diagnose.run_all)
    return ApiResponse(data=report)


# ==================== 版本与升级 ====================

@router.get("/ops/version")
async def version_info(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    return ApiResponse(data={
        "version": get_version(),
        "docker_socket": str(upgrade.DOCKER_SOCK.exists()),
        "history": upgrade.upgrade_history(),
    })


@router.post("/ops/upgrade")
async def upload_upgrade(
    pack: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """上传升级包（tar.gz，含镜像与 manifest）→ 校验 → docker load → 自动重启。"""
    operator = current_user.get("username") or current_user["user_id"]
    tmp_dir = upgrade.make_temp_pack_dir()
    tmp_file = tmp_dir / (pack.filename or "pack.tar.gz")
    try:
        # 流式落盘（镜像包可达 GB 级，不能进内存）
        with tmp_file.open("wb") as f:
            while chunk := await pack.read(8 * 1024 * 1024):
                f.write(chunk)
        result = await upgrade.apply_upgrade(tmp_file, operator)
        return ApiResponse(data=result)
    except (ValueError, RuntimeError) as e:
        logger.warning("Upgrade rejected/failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ==================== 备份与恢复 ====================

@router.post("/ops/backups")
async def create_backup_route(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    operator = current_user.get("username") or current_user["user_id"]
    try:
        result = await _run_in_executor(backup.create_backup, operator)
    except RuntimeError as e:  # 磁盘不足等
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ApiResponse(data=result)


@router.get("/ops/backups")
async def list_backups_route(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[list[dict]]:
    return ApiResponse(data=await _run_in_executor(backup.list_backups))


@router.delete("/ops/backups/{name}")
async def delete_backup_route(
    name: str,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    operator = current_user.get("username") or current_user["user_id"]
    try:
        deleted = await _run_in_executor(backup.delete_backup, name, operator)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not deleted:
        raise HTTPException(status_code=404, detail="备份不存在")
    return ApiResponse(data={"deleted": True, "name": name})


@router.get("/ops/backups/{name}/validate")
async def validate_backup_route(
    name: str,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    try:
        return ApiResponse(data=await _run_in_executor(backup.validate_backup, name))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/ops/backups/{name}/restore")
async def restore_backup_route(
    name: str,
    payload: RestoreRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="需要 confirm=true（恢复会覆盖当前数据并重启服务）")
    operator = current_user.get("username") or current_user["user_id"]
    # 先做只读校验，把「包损坏/非法」类错误以 400 返回，而不是恢复到一半
    try:
        await _run_in_executor(backup.validate_backup, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    # 关数据库连接（恢复要覆写 db 文件；进程即将退出，不再重连）
    from ..core.database import Database

    if Database._instance is not None:
        await Database.close()

    result = await _run_in_executor(backup.restore_backup, name, operator)
    return ApiResponse(data=result)


async def _run_in_executor(fn, *args):
    import asyncio

    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)
