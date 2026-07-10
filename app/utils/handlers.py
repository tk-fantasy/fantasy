from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..core.api_models import ApiResponse
from ..core.exceptions import AppException

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def handle_app_exception(_: Request, exc: AppException) -> JSONResponse:
        logger.warning("Application exception code=%s detail=%s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content=ApiResponse(code=exc.code, message=exc.message, data=None).model_dump(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception")
        return JSONResponse(
            status_code=500,
            content=ApiResponse(code="internal_error", message="服务器内部错误", data=None).model_dump(),
        )
