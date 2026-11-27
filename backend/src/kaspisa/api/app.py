"""Сборка приложения FastAPI: роутеры, ошибки, защита от CSRF, жизненный цикл."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version as app_version

from fastapi import FastAPI
from fastapi.routing import APIRoute
from redis.asyncio import Redis
from starlette.types import ASGIApp, Receive, Scope, Send

from kaspisa import __version__
from kaspisa.api import auth, connect, events, journal, notifications, products, shops, version
from kaspisa.api.catalog_state import ApiSyncLock
from kaspisa.api.deps import RENEWED_COOKIE_STATE, ApiDeps, ApiSettings
from kaspisa.api.errors import error_response, install_error_handlers
from kaspisa.api.notifications import background_notifications
from kaspisa.api.redis_state import RedisState
from kaspisa.api.schemas import ErrorOut
from kaspisa.api.telegram import Notifier
from kaspisa.api.version import ReleaseCheck
from kaspisa.config import Settings
from kaspisa.db import create_database
from kaspisa.kaspi import KaspiClient
from kaspisa.worker.redis_limits import LoginLock, PriceReadLock, RedisIntervalLimiter
from kaspisa.worker.runner import REDIS_PREFIX

logger = logging.getLogger("kaspisa.api")

API_LIMITER_MAX_WAIT_SECONDS = 10
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = b"x-requested-with"
CSRF_VALUE = b"kaspisa"


class GuardMiddleware:
    """CSRF-заголовок для изменяющих запросов и 500 без текста исключения.

    Исключение не пробрасывается дальше: иначе сервер напишет его текст в лог.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        is_api = path == "/api" or path.startswith("/api/")
        started = False

        async def tracking_send(message: dict[str, object]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                if is_api:
                    # ответы API (в том числе ошибки и продление cookie) не кэшируются
                    headers = [
                        h
                        for h in message.get("headers", [])  # type: ignore[attr-defined]
                        if h[0].lower() != b"cache-control"
                    ]
                    headers.append((b"cache-control", b"no-store"))
                    message = {**message, "headers": headers}
                renewed = scope.get("state", {}).get(RENEWED_COOKIE_STATE)
                if renewed:
                    # Продление срока сессии: cookie заново на полный срок.
                    headers = list(message["headers"])  # type: ignore[call-overload]
                    headers.append((b"set-cookie", renewed.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        send_out: Send = tracking_send  # type: ignore[assignment]
        if (
            is_api
            and scope["method"] not in SAFE_METHODS
            and dict(scope["headers"]).get(CSRF_HEADER) != CSRF_VALUE
        ):
            await error_response(403, "csrf_required")(scope, receive, send_out)
            return
        try:
            await self.app(scope, receive, send_out)
        except Exception as exc:
            logger.error("internal_error: %s на %s", type(exc).__name__, path)
            if not started:
                await error_response(500, "internal_error")(scope, receive, send_out)


def _unique_id(route: APIRoute) -> str:
    return route.name


@asynccontextmanager
async def _production_deps(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    database = create_database(settings.database_url)
    redis: Redis = Redis.from_url(settings.redis_url)
    limiter = RedisIntervalLimiter(
        redis, prefix=REDIS_PREFIX, max_wait=API_LIMITER_MAX_WAIT_SECONDS
    )
    client = KaspiClient(limiter=limiter)
    api_settings = ApiSettings()
    notifier = Notifier(database)
    app.state.deps = ApiDeps(
        database=database,
        redis=redis,
        client=client,
        login_lock=LoginLock(redis, prefix=REDIS_PREFIX),
        sync_lock=ApiSyncLock(redis, prefix=REDIS_PREFIX),
        state=RedisState(redis, prefix=f"{REDIS_PREFIX}:api"),
        limiter=limiter,
        price_read_lock=PriceReadLock(redis, prefix=REDIS_PREFIX),
        cookie_secure=api_settings.cookie_secure,
        worker_prefix=REDIS_PREFIX,
        release_check=ReleaseCheck(api_settings.update_repo, app_version("kaspisa")),
        notifier=notifier,
    )
    try:
        # Выход из контекста останавливает цикл и закрывает `Notifier`.
        async with background_notifications(app.state.deps):
            yield
    finally:
        await client.aclose()
        await redis.aclose()
        await database.dispose()


def create_app(deps: ApiDeps | None = None) -> FastAPI:
    """Приложение; без `deps` соединения создаются в lifespan, импорт сети не трогает."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            yield
            return
        async with _production_deps(app):
            yield

    app = FastAPI(
        title="kaspisa",
        version=__version__,
        lifespan=lifespan,
        generate_unique_id_function=_unique_id,
    )
    if deps is not None:
        app.state.deps = deps
    install_error_handlers(app)
    app.add_middleware(GuardMiddleware)
    # Ключ `default` описывает ошибки через `ErrorOut` и убирает стандартный 422 FastAPI.
    errors: dict[int | str, dict[str, object]] = {
        "default": {"model": ErrorOut, "description": "Ошибка"}
    }
    for router in (
        auth.router,
        shops.router,
        connect.router,
        products.router,
        journal.router,
        events.router,
        notifications.router,
    ):
        app.include_router(router, responses=errors)
    app.include_router(version.router, responses=errors)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Проверка, что сервис запущен."""
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
