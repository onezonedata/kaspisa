"""Зависимости API: настройки, константы, `ApiDeps`, текущий пользователь."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib.metadata import version as app_version
from typing import TYPE_CHECKING, Annotated, Protocol

from fastapi import Depends, Request, Response
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis

from kaspisa.api.catalog_state import ApiSyncLock
from kaspisa.api.errors import ApiError
from kaspisa.db import Database, TenantId
from kaspisa.kaspi.models import (
    CodeRequired,
    KaspiCredentials,
    ListedProduct,
    LoggedIn,
    OffersPage,
)
from kaspisa.kaspi.session import KaspiSession
from kaspisa.worker.catalog import CATALOG_MAX as _CORE_CATALOG_MAX
from kaspisa.worker.clock import utc_now
from kaspisa.worker.redis_limits import LoginLock, PriceReadLock, RedisIntervalLimiter
from kaspisa.worker.runner import REDIS_PREFIX

if TYPE_CHECKING:
    from kaspisa.api.redis_state import RedisState
    from kaspisa.api.telegram import Notifier
    from kaspisa.api.version import ReleaseCheck

SESSION_TTL_SECONDS = 2_592_000  # 30 дней с последнего использования
SESSION_ABSOLUTE_TTL_SECONDS = 7_776_000  # 90 дней с входа (ADR 0012)
# Сессия продлевается, если остаток срока меньше этого порога: не чаще раза в сутки.
SESSION_RENEW_AFTER_SECONDS = SESSION_TTL_SECONDS - 86_400
SESSION_COOKIE = "kaspisa_session"
LOGIN_FAIL_LIMIT = 10  # на пару «e-mail + IP»
LOGIN_FAIL_EMAIL_LIMIT = 100  # на e-mail со всех адресов
LOGIN_FAIL_WINDOW_SECONDS = 900
REGISTER_IP_LIMIT = 20
REGISTER_IP_WINDOW_SECONDS = 3600
RESET_IP_LIMIT = 20
RESET_IP_WINDOW_SECONDS = 900
RESET_TOKEN_TTL_SECONDS = 3600
USERS_ADD_LIMIT = 20  # добавлений пользователей в час на администратора (ADR 0039)
USERS_ADD_WINDOW_SECONDS = 3600
# Повторный ввод пароля при опасной правке (ADR 0024): окно сессии и лимит неверных попыток.
PASSWORD_CONFIRM_TTL_SECONDS = 600
PASSWORD_CONFIRM_FAIL_LIMIT = 5
PASSWORD_CONFIRM_FAIL_WINDOW_SECONDS = 900
RESEND_INTERVAL_SECONDS = 180  # повторная отправка кода (ADR 0005)
RESTART_MARK_TTL_SECONDS = 86_400  # метка «нужен вход заново»
# Предел одного вызова клиента в C1-C4; меньше LOGIN_LOCK_TTL_SECONDS (60): блокировка входа
# гарантированно держится всё время запроса. Читается через модуль (подмена в тестах).
LOGIN_CALL_TIMEOUT_SECONDS = 45
SYNC_LOCK_TTL_SECONDS = 300  # блокировка загрузки каталога
# Предел `list_products`; меньше SYNC_LOCK_TTL_SECONDS. Читается через модуль (подмена в тестах).
SYNC_CALL_TIMEOUT_SECONDS = 240
# Предел товаров магазина в памяти (список, загрузка) - `CATALOG_MAX` ядра каталога: больше -
# 409 `catalog_too_large`. Читается через этот модуль (подмена в тестах).
CATALOG_MAX = _CORE_CATALOG_MAX
# «Узнать цены» (ADR 0030); читаются через модуль (подмена в тестах).
PRICE_READ_REPEAT_SECONDS = 60  # повтор по одному товару
PRICE_READ_TIMEOUT_SECONDS = 30  # предел чтения карточки: до 10 с ожидания ограничителя, 2 страницы
PRICE_READ_LOCK_TTL_SECONDS = 40  # больше таймаута: упавший процесс не держит блокировку долго
PAGE_MAX = 200  # предел `limit`


class ApiSettings(BaseSettings):
    """Настройки, которых нет в общем `Settings`: читаются только API."""

    model_config = SettingsConfigDict(env_prefix="KASPISA_", env_file=".env", extra="ignore")

    # Для разработки по http (Safari не принимает Secure на http://localhost); в бою - true.
    cookie_secure: bool = True
    # Репозиторий GitHub `owner/repo` для проверки новой версии; пусто - проверка выключена.
    update_repo: str = Field(default="", pattern=r"^([A-Za-z0-9-]+/[A-Za-z0-9._-]+)?$")


def _disabled_release_check() -> ReleaseCheck:
    """Проверка версии без репозитория: сети нет (значение по умолчанию `ApiDeps`)."""
    from kaspisa.api.version import ReleaseCheck

    return ReleaseCheck(repo="", current=app_version("kaspisa"))


class KaspiConnectApi(Protocol):
    """Узкий интерфейс клиента Kaspi, нужный API (повторяет `KaspiApi` обработчика)."""

    async def login(
        self, credentials: KaspiCredentials, previous: KaspiSession | None = None
    ) -> LoggedIn | CodeRequired: ...

    async def submit_code(self, session: KaspiSession, code: str) -> LoggedIn: ...

    async def resend_code(self, session: KaspiSession) -> None: ...

    async def list_products(
        self, session: KaspiSession, merchant_uid: str
    ) -> list[ListedProduct]: ...

    async def get_offers(self, master_sku: str, city_id: str, merchant_uid: str) -> OffersPage: ...


@dataclass(frozen=True)
class ApiDeps:
    database: Database
    redis: Redis
    client: KaspiConnectApi
    login_lock: LoginLock
    sync_lock: ApiSyncLock
    state: RedisState
    # Тот же ограничитель, что у клиента: штраф ключа `kaspi.kz` после 429 (ADR 0030).
    limiter: RedisIntervalLimiter
    price_read_lock: PriceReadLock
    clock: Callable[[], datetime] = utc_now
    cookie_secure: bool = True
    # Префикс ключей обработчика в Redis: отсюда API читает флаг `KASPISA_DRY_RUN` обработчика.
    worker_prefix: str = REDIS_PREFIX
    release_check: ReleaseCheck = field(default_factory=lambda: _disabled_release_check())
    notifier: Notifier | None = None  # уведомления в Telegram (ADR 0025)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    tenant_id: TenantId
    # Роль приносит `current_principal` из базы; значение по умолчанию запрещает, а не разрешает.
    is_admin: bool = False


def set_session_cookie(
    response: Response, secure: bool, token: str, max_age: int = SESSION_TTL_SECONDS
) -> None:
    """Cookie сессии; один набор атрибутов для входа и продления (там max_age - остаток)."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


RENEWED_COOKIE_STATE = "session_cookie"  # ключ в `request.state`: заголовок Set-Cookie продления


def get_deps(request: Request) -> ApiDeps:
    deps: ApiDeps = request.app.state.deps
    return deps


Deps = Annotated[ApiDeps, Depends(get_deps)]


async def current_principal(request: Request, deps: Deps) -> Principal:
    """Пользователь из сессии; арендатор берётся только отсюда, не из запроса."""
    token = request.cookies.get(SESSION_COOKIE)
    principal, renewed = await deps.state.get_session(token) if token else (None, None)
    if principal is None or not token:
        raise ApiError(401, "unauthenticated")
    # Пользователь и роль - из базы: сессия удалённого пользователя не действует, даже если
    # отзыв в Redis не прошёл. Ошибка базы - не 401: интерфейс не должен уводить на вход.
    async with deps.database.tenant(principal.tenant_id) as scope:
        user = await scope.users.get(principal.user_id)
    if user is None:
        await deps.state.delete_session(token)
        raise ApiError(401, "unauthenticated")
    if renewed is not None:
        # Заголовок добавляет `GuardMiddleware`: он работает и для маршрутов, возвращающих Response.
        carrier = Response()
        set_session_cookie(carrier, deps.cookie_secure, token, renewed)
        setattr(request.state, RENEWED_COOKIE_STATE, carrier.headers["set-cookie"])
    return replace(principal, is_admin=user.is_admin)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def require_admin(principal: CurrentPrincipal) -> Principal:
    """Только администратор: 403 `admin_required` (не `unauthenticated`: тот уводит на вход)."""
    if not principal.is_admin:
        raise ApiError(403, "admin_required")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]
