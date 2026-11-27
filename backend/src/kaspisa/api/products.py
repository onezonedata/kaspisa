"""Товары магазина: загрузка каталога (P1), список (P2), настройки (P3-P5).

Единственное место вызова `list_products` в API. API никогда не вызывает запись цены. Из столбцов
наблюдения обработчика (`current_price`, `position`, `position_over`, `competitors_count`,
`first_place_price`, `last_checked_at`) API пишет их только в «Узнать цены» (ADR 0033);
`refusal_*`, `retry_at`, `skip_reason` и журнал эта кнопка не меняет.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Sequence
from typing import Annotated, NamedTuple, cast

from fastapi import APIRouter, Path, Query, Request

from kaspisa.api import deps as api_deps
from kaspisa.api.auth import client_ip
from kaspisa.api.deps import PAGE_MAX, ApiDeps, CurrentPrincipal, Deps, Principal
from kaspisa.api.errors import ApiError
from kaspisa.api.guard import (
    bulk_is_dangerous,
    ensure_password_confirmed,
    min_lowering_is_dangerous,
    next_anchor,
)
from kaspisa.api.notifications import notify_owner
from kaspisa.api.schemas import (
    ID_MAX,
    BulkProductSettingsIn,
    BulkResult,
    ProductOut,
    ProductPage,
    ProductSettingsIn,
    SyncOutcome,
    SyncResult,
)
from kaspisa.api.shops import build_shop_out
from kaspisa.api.telegram import text_min_lowered
from kaspisa.api.views import observation_from_page, pending_write, product_out
from kaspisa.db import (
    DEFAULT_CITY_ID,
    BotMode,
    ConstraintViolationError,
    OwnerEventKind,
    Product,
    StaleStateError,
    TenantScope,
)
from kaspisa.kaspi import (
    ConnectionState,
    KaspiError,
    KaspiSession,
    KaspiTransportError,
    RateLimitedError,
    SessionBlobError,
    SessionExpiredError,
    SessionStateError,
    ensure_operation_allowed,
    next_connection_state,
)
from kaspisa.kaspi.models import ListedProduct, OffersPage
from kaspisa.security import DecryptionError, UnknownKeyError
from kaspisa.worker.catalog import CatalogTooLargeError, load_catalog, merge_catalog
from kaspisa.worker.constants import RATE_LIMIT_PAUSE_MIN_SECONDS
from kaspisa.worker.redis_limits import LimiterBusyError, LimiterUnavailableError

logger = logging.getLogger("kaspisa.api")

ProductId = Annotated[int, Path(ge=1, le=ID_MAX)]
router = APIRouter(prefix="/api/shops")

SECRET_UNREADABLE = "secret_unreadable"  # noqa: S105 (код причины, не пароль)


async def _catalog(scope: TenantScope, shop_id: uuid.UUID) -> Sequence[Product]:
    """Все товары магазина; больше предела - 409 (неполный список хуже явной ошибки)."""
    try:
        return await load_catalog(scope, shop_id, limit=api_deps.CATALOG_MAX)
    except CatalogTooLargeError:
        raise ApiError(409, "catalog_too_large") from None


async def _result(
    deps: ApiDeps,
    principal: Principal,
    shop_id: uuid.UUID,
    outcome: SyncOutcome,
    *,
    added: int = 0,
    updated: int = 0,
    missing: int = 0,
    retry: int | None = None,
) -> SyncResult:
    """Ответ: магазин читается заново после записи (удалён - 404)."""
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.get(shop_id)
    if shop is None:
        raise ApiError(404, "not_found")
    return SyncResult(
        outcome=outcome,
        added=added,
        updated=updated,
        missing=missing,
        retry_after_seconds=retry,
        shop=await build_shop_out(deps, shop),
    )


def _fail_unavailable(exc: BaseException) -> ApiError:
    logger.error("limiter_unavailable: %s", type(exc).__name__)
    return ApiError(503, "service_unavailable")


@router.post("/{shop_id}/products/sync")
async def sync_products(shop_id: uuid.UUID, principal: CurrentPrincipal, deps: Deps) -> SyncResult:
    # Шаг 1: чтение без открытой транзакции на время запроса к Kaspi.
    unreadable = False
    session: KaspiSession | None = None
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.get(shop_id)
        if shop is None:
            raise ApiError(404, "not_found")
        try:
            ensure_operation_allowed(cast(ConnectionState, shop.connection_state), "cabinet")
        except SessionStateError:
            raise ApiError(409, "operation_not_allowed") from None
        if shop.merchant_uid is None:
            raise ApiError(409, "operation_not_allowed")
        version = shop.connection_version
        merchant_uid = shop.merchant_uid
        try:
            blob = await scope.shops.get_session(shop_id)
            if blob is None:
                raise SessionBlobError("нет блока сессии")
            session = KaspiSession.load(blob)
        except UnknownKeyError:
            logger.error("unknown_key shop_id=%s", shop_id)
            raise ApiError(503, "service_unavailable") from None
        except (DecryptionError, SessionBlobError):
            unreadable = True
    if unreadable or session is None:
        try:
            async with deps.database.tenant(principal.tenant_id) as scope:
                await scope.shops.mark_reauth_required(
                    shop_id, error=SECRET_UNREADABLE, expected_version=version
                )
                await scope.commit()
        except StaleStateError:
            raise ApiError(409, "state_changed") from None
        return await _result(deps, principal, shop_id, "rejected")

    # Шаг 2: блокировка загрузки; снимается в finally.
    token = await deps.sync_lock.acquire(shop_id, api_deps.SYNC_LOCK_TTL_SECONDS * 1000)
    if token is None:
        raise ApiError(409, "sync_in_progress")
    try:
        listed: list[ListedProduct] | None = None
        failure: BaseException | None = None
        try:
            async with asyncio.timeout(api_deps.SYNC_CALL_TIMEOUT_SECONDS):
                listed = await deps.client.list_products(session, merchant_uid)
        except (KaspiError, LimiterBusyError, LimiterUnavailableError) as exc:
            failure = exc
        except TimeoutError:
            failure = KaspiTransportError("list_products", "timeout")
        if isinstance(failure, LimiterUnavailableError):
            if session.changed:
                # Ограничитель отказал на втором запросе цепочки: обновлённая сессия сохраняется.
                try:
                    async with deps.database.tenant(principal.tenant_id) as scope:
                        await scope.shops.save_session(
                            shop_id, session.dump(), expected_version=version
                        )
                        await scope.commit()
                except StaleStateError:
                    raise ApiError(409, "state_changed") from None
            raise _fail_unavailable(failure)
        stored = await _store(deps, principal, shop_id, version, session, listed, failure)
    finally:
        await deps.sync_lock.release(shop_id, token)
    # Ответ строится после снятия блокировки: `catalog_loading` - настоящее состояние Redis.
    return await _result(
        deps,
        principal,
        shop_id,
        stored.outcome,
        added=stored.counts[0],
        updated=stored.counts[1],
        missing=stored.counts[2],
        retry=stored.retry,
    )


class _Stored(NamedTuple):
    """Итог шагов 4-6 загрузки: ответ строится позже, без блокировки."""

    outcome: SyncOutcome
    counts: tuple[int, int, int]
    retry: int | None


async def _store(
    deps: ApiDeps,
    principal: Principal,
    shop_id: uuid.UUID,
    version: int,
    session: KaspiSession,
    listed: list[ListedProduct] | None,
    failure: BaseException | None,
) -> _Stored:
    """Шаги 4-6: сессия, состояние и каталог одним `commit()`."""
    change = next_connection_state("connected", "cabinet", failure if failure else listed)
    try:
        async with deps.database.tenant(principal.tenant_id) as scope:
            if session.changed:
                await scope.shops.save_session(shop_id, session.dump(), expected_version=version)
            if failure is not None or listed is None:
                if change is not None and change.error is not None:
                    await scope.shops.mark_reauth_required(
                        shop_id, error=change.error, expected_version=version
                    )
                await scope.commit()
                outcome, retry = _failure_outcome(failure)
                counts = (0, 0, 0)
            else:
                try:
                    counts = await merge_catalog(
                        scope, shop_id, listed, limit=api_deps.CATALOG_MAX, now=deps.clock()
                    )
                except CatalogTooLargeError:
                    raise ApiError(409, "catalog_too_large") from None
                await scope.commit()
                outcome, retry = "synced", None
    except StaleStateError:
        raise ApiError(409, "state_changed") from None
    except ConstraintViolationError:
        raise ApiError(409, "sync_in_progress") from None
    return _Stored(outcome, counts, retry)


def _failure_outcome(failure: BaseException | None) -> tuple[SyncOutcome, int | None]:
    if isinstance(failure, SessionExpiredError):
        return "rejected", None
    retry = getattr(failure, "retry_after", None)
    if isinstance(failure, LimiterBusyError):
        retry = math.ceil(failure.wait_seconds)
    return "temporary_error", retry


@router.get("/{shop_id}/products")
async def list_products(
    shop_id: uuid.UUID,
    principal: CurrentPrincipal,
    deps: Deps,
    q: Annotated[str | None, Query(max_length=100)] = None,
    tracked: bool | None = None,
    has_bounds: bool | None = None,
    has_refusal: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProductPage:
    async with deps.database.tenant(principal.tenant_id) as scope:
        if await scope.shops.get(shop_id) is None:
            raise ApiError(404, "not_found")
        found = list(await _catalog(scope, shop_id))
    needle = (q or "").strip().casefold()
    if needle:
        found = [p for p in found if needle in p.sku.casefold() or needle in p.title.casefold()]
    if tracked is not None:
        found = [p for p in found if p.tracked == tracked]
    if has_bounds is not None:
        found = [
            p for p in found if (p.min_price is not None and p.max_price is not None) == has_bounds
        ]
    if has_refusal is not None:
        found = [p for p in found if (p.refusal_reason is not None) == has_refusal]
    found.sort(key=lambda p: (not p.tracked, p.id))  # отслеживаемые первыми (ADR 0017)
    page = found[offset : offset + limit]
    return ProductPage(items=[product_out(p) for p in page], total=len(found))


@router.get("/{shop_id}/products/{product_id}")
async def get_product(
    shop_id: uuid.UUID,
    product_id: ProductId,
    principal: CurrentPrincipal,
    deps: Deps,
) -> ProductOut:
    async with deps.database.tenant(principal.tenant_id) as scope:
        product = await scope.products.get(product_id)
        if product is None or product.shop_id != shop_id:
            raise ApiError(404, "not_found")
        write = await scope.price_writes.get(product_id)
        return product_out(product, pending_write(product, write, deps.clock()))


@router.post("/{shop_id}/products/{product_id}/price-read")
async def read_prices(
    shop_id: uuid.UUID,
    product_id: ProductId,
    principal: CurrentPrincipal,
    deps: Deps,
) -> ProductOut:
    """«Узнать цены» (ADR 0030, 0033): разовое чтение публичной карточки без кабинета и записи.

    Пишет поля наблюдения товара (как обработчик) и сбрасывает решение бота по правилу правки
    товара; журнал, пропуск, отказ и настройки не трогает. Одно чтение на магазин (блокировка
    в Redis), повтор по товару - не чаще `PRICE_READ_REPEAT_SECONDS`; запрос идёт через общий
    ограничитель `kaspi.kz`. Таймер повтора остаётся, только если запрос мог уйти в Kaspi.
    Если город сверки магазина не задан, карточка читается в Астане (ADR 0031).
    """
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.get(shop_id)
        if shop is None:
            raise ApiError(404, "not_found")
        product = await scope.products.get(product_id)
        if product is None or product.shop_id != shop_id:
            raise ApiError(404, "not_found")
        if shop.merchant_uid is None:
            raise ApiError(409, "operation_not_allowed")
        master_sku = product.master_sku
        merchant_uid = shop.merchant_uid
        shop_city_id = shop.city_id  # для защиты снимка от смены города во время чтения
        city_id = shop_city_id or DEFAULT_CITY_ID

    token = await deps.price_read_lock.acquire(shop_id, api_deps.PRICE_READ_LOCK_TTL_SECONDS * 1000)
    if token is None:
        raise ApiError(409, "price_read_in_progress")
    try:
        remaining = await deps.state.start_price_read_timer(product_id)
        if remaining is not None:
            raise ApiError(429, "price_read_too_early", remaining)
        page = await _fetch_offers(deps, product_id, master_sku, city_id, merchant_uid)
        seen = observation_from_page(page, merchant_uid)
        async with deps.database.tenant(principal.tenant_id) as scope:
            locked = await scope.products.get_for_update(product_id)
            if locked is None or locked.shop_id != shop_id:
                raise ApiError(404, "not_found")
            current_shop = await scope.shops.get(shop_id)
            if current_shop is None:
                raise ApiError(404, "not_found")
            if locked.master_sku != master_sku or current_shop.city_id != shop_city_id:
                raise ApiError(409, "state_changed")  # карточка или город сменились за чтение
            if seen.current_price is not None:  # своего нет: цена прежняя (опора ADR 0024)
                locked.current_price = seen.current_price
            locked.position = seen.position
            locked.position_over = seen.position_over
            locked.competitors_count = seen.competitors_count
            locked.first_place_price = seen.first_place_price
            locked.last_checked_at = deps.clock()
            _reset_decision(locked, (locked.min_price, locked.max_price))
            await scope.commit()
            write = await scope.price_writes.get(product_id)
            result = product_out(locked, pending_write(locked, write, deps.clock()))
    finally:
        await deps.price_read_lock.release(shop_id, token)
    return result


async def _fetch_offers(
    deps: ApiDeps, product_id: int, master_sku: str, city_id: str, merchant_uid: str
) -> OffersPage:
    """Чтение карточки с разбором ошибок (таблица ADR 0030); наблюдение не меняется."""
    try:
        async with asyncio.timeout(api_deps.PRICE_READ_TIMEOUT_SECONDS):
            return await deps.client.get_offers(master_sku, city_id, merchant_uid)
    except LimiterBusyError as exc:
        await deps.state.clear_price_read_timer(product_id)
        raise ApiError(429, "kaspi_busy", math.ceil(exc.wait_seconds)) from None
    except LimiterUnavailableError as exc:
        await deps.state.clear_price_read_timer(product_id)
        raise _fail_unavailable(exc) from None
    except RateLimitedError as exc:
        pause = max(exc.retry_after or 0, RATE_LIMIT_PAUSE_MIN_SECONDS)
        try:
            await deps.limiter.penalize("kaspi.kz", pause)
        except LimiterUnavailableError as unavailable:
            logger.error("limiter_unavailable: %s", type(unavailable).__name__)
        left = await deps.state.price_read_left(product_id)
        raise ApiError(429, "kaspi_busy", max(pause, left)) from None
    except (KaspiError, TimeoutError) as exc:
        logger.warning("price_read_failed: %s", type(exc).__name__)
        left = await deps.state.price_read_left(product_id)
        raise ApiError(502, "kaspi_unavailable", left) from None
    except ValueError as exc:
        # Недопустимые символы `master_sku` или города: порча данных каталога, запрос не ушёл.
        await deps.state.clear_price_read_timer(product_id)
        logger.error("price_read_invalid_input: %s product_id=%s", type(exc).__name__, product_id)
        raise ApiError(500, "internal_error") from None


def _bounds_text(bounds: tuple[int | None, int | None]) -> str:
    """Границы для журнала владельца: `<min>–<max>`, пустая сторона - граница не задана."""
    low, high = bounds
    return f"{'' if low is None else low}–{'' if high is None else high}"


def _reset_decision(product: Product, bounds: tuple[int | None, int | None]) -> None:
    """Правка товара обнуляет решение бота: до проверки видно «ожидает проверки» (ADR 0020).

    Исключение: отказ записи при прежних границах. Обработчик идёт по пути отпечатка отказа
    и решение не пишет, поэтому итог `refused` остаётся (ADR 0020, «Хранение решения»).
    """
    unchanged = bounds == (product.min_price, product.max_price)
    refused = product.refusal_fingerprint is not None and product.decision_write == "refused"
    if unchanged and refused:
        return
    product.decision_reason = None
    product.decision_target_price = None
    product.decision_write = None


@router.patch("/{shop_id}/products/{product_id}")
async def update_product(
    shop_id: uuid.UUID,
    product_id: ProductId,
    body: ProductSettingsIn,
    request: Request,
    principal: CurrentPrincipal,
    deps: Deps,
) -> ProductOut:
    """Границы меняются под блокировкой строки товара: новая действует со следующей записи.

    Опасное снижение минимума (ADR 0024) требует подтверждённого пароля: проверка идёт по
    значениям заблокированной строки и до любых присваиваний.
    """
    async with deps.database.tenant(principal.tenant_id) as scope:
        product = await scope.products.get_for_update(product_id)
        if product is None or product.shop_id != shop_id:
            raise ApiError(404, "not_found")
        fields = body.model_fields_set
        low = body.min_price if "min_price" in fields else product.min_price
        high = body.max_price if "max_price" in fields else product.max_price
        if low is not None and high is not None and low > high:
            raise ApiError(422, "min_above_max")
        bounds = (product.min_price, product.max_price)
        dangerous = "min_price" in fields and min_lowering_is_dangerous(
            body.min_price, product.min_price, product.min_price_anchor, product.current_price
        )
        if dangerous:
            await ensure_password_confirmed(request, deps)
        if "min_price" in fields:
            product.min_price_anchor = next_anchor(
                body.min_price, product.min_price_anchor, dangerous
            )
        for name in fields:
            setattr(product, name, getattr(body, name))
        product.skip_reason = None  # любая правка товара снимает причину пропуска (ADR 0017)
        product.skipped_at = None
        _reset_decision(product, bounds)
        new_bounds = (product.min_price, product.max_price)
        if new_bounds != bounds and fields & {"min_price", "max_price"}:
            await scope.owner_events.add(
                kind=OwnerEventKind.BOUNDS_CHANGED,
                user_id=principal.user_id,
                ip=client_ip(request),
                shop_id=shop_id,
                sku=product.sku,
                old_value=_bounds_text(bounds),
                new_value=_bounds_text(new_bounds),
            )
        await scope.commit()
        if dangerous and new_bounds[0] is not None:  # опасная правка всегда задаёт минимум
            shop = await scope.shops.get(shop_id)
            notify_owner(
                deps,
                principal.tenant_id,
                text_min_lowered(
                    shop.merchant_uid if shop else None, product.sku, bounds[0], new_bounds[0]
                ),
            )
        write = await scope.price_writes.get(product_id)
        return product_out(product, pending_write(product, write, deps.clock()))


@router.patch("/{shop_id}/products")
async def update_products(
    shop_id: uuid.UUID,
    body: BulkProductSettingsIn,
    request: Request,
    principal: CurrentPrincipal,
    deps: Deps,
) -> BulkResult:
    async with deps.database.tenant(principal.tenant_id) as scope:
        # Блокировка магазина (NotFoundError - 404) раньше товаров, как у обработчика; режим
        # бота берётся по заблокированной строке.
        shop = await scope.shops.lock_for_write(shop_id)
        # Все блокировки - до присваиваний (по возрастанию id): `populate_existing`
        # следующего `get_for_update` затёр бы несохранённые значения.
        locked: list[Product] = []
        for product_id in body.product_ids:
            product = await scope.products.get_for_update(product_id)
            if product is None or product.shop_id != shop_id:
                raise ApiError(404, "not_found")
            locked.append(product)
        if bulk_is_dangerous(body.changes, BotMode(shop.bot_mode)):
            await ensure_password_confirmed(request, deps)
        for product in locked:
            bounds = (product.min_price, product.max_price)
            for name in body.changes.model_fields_set:
                setattr(product, name, getattr(body.changes, name))
            product.skip_reason = None  # правка снимает причину пропуска (ADR 0017)
            product.skipped_at = None
            _reset_decision(product, bounds)
        await scope.commit()
    return BulkResult(updated=len(locked))
