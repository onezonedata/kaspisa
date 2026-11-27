"""Ошибки API: единый вид ответа без значений ввода и без текста исключений."""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from kaspisa.api.passwords import HashBusyError
from kaspisa.api.schemas import ErrorOut
from kaspisa.db import NotFoundError

logger = logging.getLogger("kaspisa.api")


class ApiError(Exception):
    """Ошибка с кодом ответа; `code` и статус задаёт вызывающий."""

    def __init__(
        self,
        status: int,
        code: str,
        retry_after_seconds: int | None = None,
        fields: list[str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.fields = fields


def error_response(
    status: int,
    code: str,
    retry_after_seconds: int | None = None,
    fields: list[str] | None = None,
) -> JSONResponse:
    body = ErrorOut(code=code, retry_after_seconds=retry_after_seconds, fields=fields)
    return JSONResponse(status_code=status, content=body.model_dump())


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    error = cast(ApiError, exc)
    return error_response(error.status, error.code, error.retry_after_seconds, error.fields)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    fields: list[str] = []
    for item in cast(RequestValidationError, exc).errors():
        # Только пути полей: значения ввода и тексты проверок в ответ не попадают.
        path = ".".join(str(part) for part in item["loc"] if isinstance(part, str))
        if path and path not in fields:
            fields.append(path)
    return error_response(422, "validation_error", fields=fields)


async def _not_found(_: Request, __: Exception) -> JSONResponse:
    return error_response(404, "not_found")


async def _unavailable(_: Request, exc: Exception) -> JSONResponse:
    logger.error("service_unavailable: %s", type(exc).__name__)
    return error_response(503, "service_unavailable")


async def _server_busy(_: Request, __: Exception) -> JSONResponse:
    return error_response(503, "server_busy", retry_after_seconds=1)


def install_error_handlers(app: FastAPI) -> None:
    handlers: dict[type[Exception], Any] = {
        ApiError: _api_error,
        RequestValidationError: _validation_error,
        NotFoundError: _not_found,
        HashBusyError: _server_busy,
        RedisError: _unavailable,
        OSError: _unavailable,
    }
    for exc_type, handler in handlers.items():
        app.add_exception_handler(exc_type, handler)
