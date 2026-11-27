"""Доступ к магазину: сессия кабинета, автовход, ключи, состояние подключения.

Правила (ADR 0003, 0005, 0008):

- состояние подключения меняется только методами `mark_*` с версией, прочитанной до запроса;
  `StaleStateError` - действие отбрасывается;
- `UnknownKeyError` состояние не меняет: магазин откладывается в базе;
- один автовход на одно истечение сессии (`unproven_login_version`);
- сессия сохраняется после операции, если `session.changed`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import NoReturn, cast

from redis.exceptions import RedisError

from kaspisa.db import BotMode, Shop, StaleStateError, TenantId, TenantScope
from kaspisa.kaspi import (
    ConnectionState,
    KaspiCredentials,
    KaspiError,
    KaspiSession,
    LoginRejectedError,
    RateLimitedError,
    SessionBlobError,
    SessionExpiredError,
    SessionStateError,
    ensure_operation_allowed,
    next_connection_state,
)
from kaspisa.kaspi.models import CodeRequired, LoggedIn
from kaspisa.security import DecryptionError, Secret, UnknownKeyError
from kaspisa.worker.constants import (
    LEASE_SECONDS,
    LOGIN_BACKOFF_DEFAULT_SECONDS,
    LOGIN_ERROR_LIMIT,
    LOGIN_LOCK_BUSY_SECONDS,
    LOGIN_LOCK_TTL_SECONDS,
    RATE_LIMIT_PAUSE_MIN_SECONDS,
    UNKNOWN_KEY_SKIP_SECONDS,
)
from kaspisa.worker.context import WorkerContext
from kaspisa.worker.errors import ShopSkipped, ShopThrottled, ShopUnavailable
from kaspisa.worker.redis_limits import LoginLock

logger = logging.getLogger(__name__)

SESSION_EXPIRED = "session_expired"
SESSION_EXPIRED_AFTER_LOGIN = "session_expired_after_login"
SECRET_UNREADABLE = "secret_unreadable"  # noqa: S105 (причина состояния, не пароль)


async def throttle(ctx: WorkerContext, key: str, exc: RateLimitedError) -> NoReturn:
    """429 от Kaspi: пауза в общий ограничитель и конец слайса магазина."""
    pause = max(exc.retry_after or 0, RATE_LIMIT_PAUSE_MIN_SECONDS)
    await ctx.limiter.penalize(key, pause)
    raise ShopThrottled(ctx.clock() + timedelta(seconds=pause))


class ShopAccess:
    """Доступ слайса к одному магазину. Не потокобезопасен: слайс однопоточный."""

    def __init__(self, ctx: WorkerContext, tenant_id: TenantId, shop: Shop) -> None:
        self._ctx = ctx
        self._tenant_id = tenant_id
        self.shop = shop
        self.session: KaspiSession | None = None
        # версия подключения, под которой загружена сессия: после `refresh()` другая версия
        # значит, что продавец переподключил магазин и эти cookie использовать нельзя
        self.session_version = shop.connection_version

    @property
    def session_stale(self) -> bool:
        """Версия подключения сменилась после загрузки сессии."""
        return self.shop.connection_version != self.session_version

    @property
    def auto_login_allowed(self) -> bool:
        """Автовход разрешён: бот не выключен и магазин не остановлен (ADR 0031, ADR 0034)."""
        return self.shop.bot_mode != BotMode.OFF and self.shop.halt_reason is None

    # --- открытие ---

    @classmethod
    async def open(cls, ctx: WorkerContext, tenant_id: TenantId, shop_id: uuid.UUID) -> ShopAccess:
        """Готовит доступ; `ShopSkipped` - магазин в этом слайсе пропускается.

        `connected` - загрузка сессии; `reauth_required('session_expired')` - автовход
        (после него слайс заканчивается); прочие состояния - ни одного запроса.
        """
        async with ctx.database.tenant(tenant_id) as scope:
            shop = await scope.shops.get(shop_id)
        if shop is None or shop.merchant_uid is None:
            raise ShopSkipped
        access = cls(ctx, tenant_id, shop)
        state = shop.connection_state
        if state == "connected":
            blob = await access._read_secret(lambda scope: scope.shops.get_session(shop.id))
            if blob is None:
                raise ShopSkipped
            try:
                access.session = KaspiSession.load(blob)
            except SessionBlobError:
                await access._mark_reauth(SECRET_UNREADABLE, shop.connection_version)
                raise ShopSkipped from None
            return access
        expired = state == "reauth_required" and shop.connection_error == SESSION_EXPIRED
        if expired and access.auto_login_allowed:
            await access.relogin()
        raise ShopSkipped

    def _now(self) -> datetime:
        return self._ctx.clock()

    async def refresh(self) -> None:
        """Перечитывает магазин из базы."""
        async with self._ctx.database.tenant(self._tenant_id) as scope:
            shop = await scope.shops.get(self.shop.id)
        if shop is not None:
            self.shop = shop

    # --- ключи ---

    async def _read_secret[T](self, read: Callable[[TenantScope], Awaitable[T]]) -> T:
        """Чтение с расшифровкой: ошибки ключей превращаются в пропуск магазина."""
        unknown_key = False
        try:
            async with self._ctx.database.tenant(self._tenant_id) as scope:
                return await read(scope)
        except UnknownKeyError:
            unknown_key = True
        except DecryptionError:
            pass
        if unknown_key:
            await self._report_unknown_key()
            until = self._now() + timedelta(seconds=UNKNOWN_KEY_SKIP_SECONDS)
            raise ShopSkipped(until)  # состояние магазина не меняется
        await self._mark_reauth(SECRET_UNREADABLE, self.shop.connection_version)
        raise ShopSkipped

    async def _report_unknown_key(self) -> None:
        """Уровень тревоги: ключ не выдан процессу (много магазинов) или испорчен один блок."""
        spread = 0
        try:
            async with self._ctx.database.system() as system:
                spread = await system.unknown_key_spread(self.shop.id)
        except Exception:  # тревога не должна ломать пропуск магазина
            logger.warning("не удалось посчитать магазины с неизвестным ключом")
        if spread >= 2:
            logger.error(
                "unknown_key_shared: процессу не выдали ключ, магазинов затронуто %d "
                "(магазин %s пропущен)",
                spread,
                self.shop.id,
            )
        else:
            logger.error(
                "unknown_key_isolated: испорчен блок одного магазина %s (магазин пропущен)",
                self.shop.id,
            )

    # --- состояние подключения ---

    async def _mark_reauth(self, error: str, version: int) -> bool:
        """`reauth_required` с версией; False, если версия устарела (действие отброшено)."""
        try:
            async with self._ctx.database.tenant(self._tenant_id) as scope:
                shop = await scope.shops.mark_reauth_required(
                    self.shop.id, error=error, expected_version=version
                )
                await scope.commit()
        except StaleStateError:
            await self.refresh()
            return False
        self.shop = shop
        return True

    # --- запросы кабинета ---

    async def renew_lease(self) -> None:
        """Продлевает аренду магазина; потеряна - `ShopUnavailable`, запрос не отправляется."""
        ctx = self._ctx
        async with ctx.database.tenant(self._tenant_id) as scope:
            until = ctx.clock() + timedelta(seconds=LEASE_SECONDS)
            renewed = await scope.shops.renew_lease(self.shop.id, ctx.config.worker_id, until)
            await scope.commit()
        if not renewed:
            raise ShopUnavailable

    async def cabinet[T](
        self, call: Callable[[KaspiSession], Awaitable[T]], *, renew: bool = True
    ) -> T:
        """Запрос кабинета: аренда, допустимость, сохранение сессии, истечение сессии, 429.

        `renew=False` - когда вызывающий уже продлил аренду и между запросами цепочки
        записи других дел быть не должно.
        """
        if renew:
            await self.renew_lease()
        return await self._run(call, write=False)

    async def cabinet_background[T](self, call: Callable[[KaspiSession], Awaitable[T]]) -> T:
        """Фоновый запрос (загрузка каталога): аренда продлевается, автовхода и паузы 429 нет.

        Истёкшая сессия переводит подключение в `reauth_required` (как у любого запроса
        кабинета), но вход не запускается: серия ошибок входа не растёт. 429 и прочие ошибки
        Kaspi пробрасываются вызывающему.
        """
        await self.renew_lease()
        return await self._run(call, write=True)

    async def persist_session(self) -> None:
        """Сохраняет изменённую сессию (после сбоя вне `_run`: ограничитель, срок вызова)."""
        await self._persist(self.session_version)

    async def cabinet_write[T](self, call: Callable[[KaspiSession], Awaitable[T]]) -> T:
        """Путь записи цены: без повторного входа; `SessionExpiredError` пробрасывается."""
        return await self._run(call, write=True)

    async def _run[T](self, call: Callable[[KaspiSession], Awaitable[T]], *, write: bool) -> T:
        shop = self.shop
        version = self.session_version  # версия, под которой загружена сессия
        try:
            ensure_operation_allowed(cast("ConnectionState", shop.connection_state), "cabinet")
        except SessionStateError:
            raise ShopUnavailable from None
        session = self.session
        if session is None:
            raise ShopUnavailable
        try:
            result = await call(session)
        except SessionExpiredError as exc:
            await self._on_expired(version, exc, write=write)
            raise
        except RateLimitedError as exc:
            await self._persist(version)  # cookie из ответа 429 не теряются
            if write:
                raise
            await throttle(self._ctx, f"mc:{shop.merchant_uid}", exc)
        except KaspiError:
            await self._persist(version)
            raise
        await self._after_success(version)
        return result

    async def _persist(self, version: int) -> None:
        """`session.dump()` в базу, если Kaspi обновил cookie; устаревшая версия не страшна."""
        session = self.session
        if session is None or not session.changed:
            return
        try:
            async with self._ctx.database.tenant(self._tenant_id) as scope:
                await scope.shops.save_session(
                    self.shop.id, session.dump(), expected_version=version
                )
                await scope.commit()
        except StaleStateError:
            logger.warning("сессия магазина %s не сохранена: состояние изменилось", self.shop.id)
            return
        session.changed = False

    async def _after_success(self, version: int) -> None:
        await self._persist(version)
        if self.shop.unproven_login_version == version:
            async with self._ctx.database.tenant(self._tenant_id) as scope:
                await scope.shops.clear_unproven_login(self.shop.id, version)
                await scope.commit()
            await self.refresh()

    async def _on_expired(self, version: int, exc: SessionExpiredError, *, write: bool) -> None:
        """Сессия не принята: `reauth_required`; на пути чтения - один автовход.

        При выключенном боте и у остановленного магазина автовхода нет (ADR 0031, ADR 0034):
        `ShopUnavailable`, как при отброшенной отметке; слайс заканчивается.
        """
        shop = self.shop
        after_login = shop.unproven_login_version == version
        if after_login:
            error = SESSION_EXPIRED_AFTER_LOGIN
        else:
            change = next_connection_state(
                cast("ConnectionState", shop.connection_state), "cabinet", exc
            )
            error = (change.error if change is not None else None) or SESSION_EXPIRED
        marked = await self._mark_reauth(error, version)
        if write:
            return
        # режим и остановка - из строки, перечитанной при отметке: их могли сменить в слайсе
        if not marked or after_login or not self.auto_login_allowed:
            raise ShopUnavailable
        await self.relogin()

    # --- автовход ---

    async def relogin(self) -> NoReturn:
        """Автовход под блокировкой Redis; слайс после него всегда заканчивается."""
        ctx = self._ctx
        until: datetime | None = None
        lock = LoginLock(ctx.redis, prefix=ctx.limiter.prefix)
        try:
            token = await lock.acquire(self.shop.id, LOGIN_LOCK_TTL_SECONDS * 1000)
        except (RedisError, OSError):
            raise ShopUnavailable(
                self._now() + timedelta(seconds=LOGIN_BACKOFF_DEFAULT_SECONDS)
            ) from None
        if token is None:
            raise ShopUnavailable(self._now() + timedelta(seconds=LOGIN_LOCK_BUSY_SECONDS))
        try:
            until = await self._login_locked()
        finally:
            try:
                await lock.release(self.shop.id, token)
            except (RedisError, OSError):  # результат входа уже сохранён; блокировка истечёт сама
                logger.warning("блокировка входа магазина %s не снята", self.shop.id)
        raise ShopUnavailable(until)

    async def _login_locked(self) -> datetime | None:
        """Вход под блокировкой; срок пропуска магазина или None."""
        expected = self.shop.connection_version
        await self.refresh()  # под блокировкой: вход мог сделать другой процесс
        shop = self.shop
        if (
            shop.connection_state != "reauth_required"
            or shop.connection_error != SESSION_EXPIRED
            or shop.connection_version != expected
        ):
            return None
        state = cast("ConnectionState", shop.connection_state)
        ensure_operation_allowed(state, "login")
        credentials = await self._read_secret(self._credentials)
        previous = await self._previous_session()
        try:
            outcome = await self._ctx.client.login(credentials, previous)
        except LoginRejectedError as exc:
            return await self._login_rejected(exc, state, expected)
        except KaspiError:
            await self._record_login(ok=False)
            return self._now() + timedelta(seconds=LOGIN_BACKOFF_DEFAULT_SECONDS)
        return await self._login_outcome(outcome, expected)

    async def _credentials(self, scope: TenantScope) -> KaspiCredentials:
        found = await scope.shops.get_credentials(self.shop.id)
        return KaspiCredentials(login=found.login, password=found.password)

    async def _previous_session(self) -> KaspiSession | None:
        """Прежние cookie (запомненное устройство); нечитаемый блок - как будто его нет."""
        blob: Secret | None = await self._read_secret(
            lambda scope: scope.shops.get_session(self.shop.id)
        )
        if blob is None:
            return None
        try:
            return KaspiSession.load(blob)
        except SessionBlobError:
            return None

    async def _login_rejected(
        self, exc: LoginRejectedError, state: ConnectionState, version: int
    ) -> datetime | None:
        change = next_connection_state(state, "login", exc)
        if change is None:  # временный отказ: состояние не меняется, повтор позже
            await self._record_login(ok=False)
            pause = exc.retry_after_seconds or LOGIN_BACKOFF_DEFAULT_SECONDS
            return self._now() + timedelta(seconds=pause)
        await self._mark_reauth(change.error or SESSION_EXPIRED, version)
        return None

    async def _login_outcome(
        self, outcome: LoggedIn | CodeRequired, version: int
    ) -> datetime | None:
        shop = self.shop
        merchant_uid = shop.merchant_uid
        if merchant_uid is None:  # проверено в open
            return None
        try:
            async with self._ctx.database.tenant(self._tenant_id) as scope:
                if isinstance(outcome, LoggedIn):
                    await scope.shops.mark_connected(
                        shop.id,
                        merchant_uid=merchant_uid,
                        session_state=outcome.session.dump(),
                        expected_version=version,
                        auto_login=True,
                    )
                    await scope.shops.record_login_outcome(
                        shop.id, True, LOGIN_ERROR_LIMIT, self._now()
                    )
                else:  # письмо с кодом одно; код вводит продавец
                    await scope.shops.mark_awaiting_code(
                        shop.id, outcome.session.dump(), expected_version=version
                    )
                await scope.commit()
        except StaleStateError:
            await self.refresh()
        return None

    async def _record_login(self, *, ok: bool) -> None:
        async with self._ctx.database.tenant(self._tenant_id) as scope:
            await scope.shops.record_login_outcome(self.shop.id, ok, LOGIN_ERROR_LIMIT, self._now())
            await scope.commit()
