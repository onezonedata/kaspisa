"""Слайс магазина: один захват, подтверждения, проверка товаров, аренда."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta

from kaspisa.db import BotMode, ClaimedShop, Product, SkipReason, TenantId
from kaspisa.kaspi import KaspiError, RateLimitedError, ResponseFormatError
from kaspisa.worker.access import ShopAccess
from kaspisa.worker.catalog_refresh import refresh_catalog_if_due
from kaspisa.worker.confirm import verify_open_writes
from kaspisa.worker.constants import (
    LEASE_SECONDS,
    LOGIN_BACKOFF_DEFAULT_SECONDS,
    MIN_RECHECK_SECONDS,
    PRICE_WRITE_LIMIT,
    PRICE_WRITE_WINDOW_SECONDS,
    SLICE_MAX_PRODUCTS,
    SLICE_MAX_SECONDS,
    UNBOUNDED_RECHECK_SECONDS,
    UNEXPECTED_ERROR_PAUSE_SECONDS,
)
from kaspisa.worker.context import SliceContext, WorkerContext
from kaspisa.worker.cycle import check_product
from kaspisa.worker.errors import ShopThrottled, ShopUnavailable
from kaspisa.worker.redis_limits import LimiterBusyError, LimiterUnavailableError
from kaspisa.worker.writer import WriteOutcome

logger = logging.getLogger(__name__)

# Пропуск записи, после которого решения по магазину в этом слайсе не принимаются.
STOP_REASONS = frozenset({"halted", "not_connected", "bot_disabled", "not_live"})


async def _redis_alive(ctx: WorkerContext) -> bool:
    try:
        await ctx.redis.ping()
    except Exception:
        return False
    return True


async def run_shop_slice(
    ctx: WorkerContext, claim: ClaimedShop, stop: asyncio.Event | None = None
) -> None:
    """Проверяет товары захваченного магазина. Аренда снимается, если магазин не отложен.

    Событие `stop` не прерывает текущий товар: новые товары после него не берутся.
    """
    tenant_id = TenantId(claim.tenant_id)
    deferred_until: datetime | None = None
    try:
        deferred_until = await _slice(ctx, tenant_id, claim, stop)
    finally:
        async with ctx.database.tenant(tenant_id) as scope:
            worker_id = ctx.config.worker_id
            if deferred_until is not None:
                await scope.shops.defer(claim.shop_id, worker_id, deferred_until)
            else:
                await scope.shops.release_lease(claim.shop_id, worker_id)
            await scope.commit()


async def _slice(
    ctx: WorkerContext, tenant_id: TenantId, claim: ClaimedShop, stop: asyncio.Event | None
) -> datetime | None:
    """Возвращает срок пропуска магазина (`defer`) или None."""
    started = ctx.clock()
    if not await _redis_alive(ctx):
        logger.error("Redis недоступен: магазин %s отложен", claim.shop_id)
        return started + timedelta(seconds=LOGIN_BACKOFF_DEFAULT_SECONDS)
    try:
        access = await ShopAccess.open(ctx, tenant_id, claim.shop_id)
        return await _work(SliceContext(ctx, tenant_id, access), started, stop)
    except ShopUnavailable as stop:  # в том числе ShopSkipped
        return stop.until
    except ShopThrottled as throttled:  # 429: пауза уже в ограничителе, магазин ждёт её конца
        return throttled.until
    except LimiterBusyError as busy:  # ожидание ограничителя дольше предела: не спим в слайсе
        return ctx.clock() + timedelta(seconds=math.ceil(busy.wait_seconds))
    except LimiterUnavailableError:
        return ctx.clock() + timedelta(seconds=LOGIN_BACKOFF_DEFAULT_SECONDS)
    except Exception as exc:  # без текста: он может быть чужим; магазин не крутится в цикле
        logger.error(
            "слайс магазина %s: непредвиденная ошибка %s", claim.shop_id, type(exc).__name__
        )
        return ctx.clock() + timedelta(seconds=UNEXPECTED_ERROR_PAUSE_SECONDS)


async def _quota_free_at(slice_ctx: SliceContext) -> datetime | None:
    """Срок пропуска магазина при исчерпанной квоте или None.

    Квота ограничивает только новые записи: не позже ближайшей проверки открытой записи.
    """
    ctx = slice_ctx.worker
    async with ctx.database.tenant(slice_ctx.tenant_id) as scope:
        free_at = await scope.price_changes.quota_free_at(
            slice_ctx.shop.id,
            now=ctx.clock(),
            limit=PRICE_WRITE_LIMIT,
            window_seconds=PRICE_WRITE_WINDOW_SECONDS,
        )
        if free_at is None:
            return None
        verify_at = await scope.price_writes.next_verify_time(slice_ctx.shop.id)
    return free_at if verify_at is None else min(free_at, verify_at)


async def _work(
    slice_ctx: SliceContext, started: datetime, stop: asyncio.Event | None
) -> datetime | None:
    """Подтверждения, проверка цен, затем суточная загрузка каталога (ADR 0010, Н7).

    Загрузка не зависит от решений (бот, город, остановка не по входу ей не мешают) и идёт
    после проверки цен; её сбои внутри шага магазин не откладывают.
    """
    if not await verify_open_writes(slice_ctx):
        return None  # аренда потеряна
    await slice_ctx.access.refresh()  # подтверждение могло остановить магазин
    shop = slice_ctx.shop
    # при режиме «выключен» не проверяется ни один товар (ADR 0031)
    checks = (
        shop.connection_state == "connected"
        and not slice_ctx.access.session_stale  # переподключение: старая сессия негодна
        and shop.halt_reason is None
        and shop.city_id is not None
        and shop.bot_mode != BotMode.OFF
    )
    deferred_until = await _check_products(slice_ctx, started, stop) if checks else None
    await refresh_catalog_if_due(slice_ctx, stop)
    return deferred_until


async def _next_product(slice_ctx: SliceContext, step: int) -> Product | None:
    """Следующий должный товар: первый шаг слайса - без границ, дальше сначала с границами.

    Так товары с границами не вытесняются, а товары без границ не голодают (ADR 0030).
    """
    ctx = slice_ctx.worker
    order = [False, True] if step == 0 else [True, False]
    async with ctx.database.tenant(slice_ctx.tenant_id) as scope:
        for bounded in order:
            product = await scope.products.next_due(
                slice_ctx.shop.id,
                now=ctx.clock(),
                recheck_seconds=MIN_RECHECK_SECONDS,
                unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
                bounded=bounded,
            )
            if product is not None:
                return product
    return None


async def _check_products(
    slice_ctx: SliceContext,
    started: datetime,
    stop: asyncio.Event | None,
) -> datetime | None:
    ctx = slice_ctx.worker
    shop_id = slice_ctx.shop.id
    # квота записей: исчерпана - магазин ждёт целиком (ADR 0030, Н13)
    free_at = await _quota_free_at(slice_ctx)
    if free_at is not None:
        return free_at

    processed = 0
    while processed < SLICE_MAX_PRODUCTS:
        if stop is not None and stop.is_set():
            break
        if (ctx.clock() - started).total_seconds() >= SLICE_MAX_SECONDS:
            break
        product = await _next_product(slice_ctx, processed)
        if product is None:
            break
        try:
            result = await check_product(slice_ctx, product)
        except RateLimitedError as exc:  # пауза лимита штатная: причины пропуска нет
            logger.warning("товар %s: ошибка Kaspi %s", product.id, type(exc).__name__)
            result = None
            async with ctx.database.tenant(slice_ctx.tenant_id) as scope:
                await scope.products.touch_checked(product.id, ctx.clock())
                await scope.commit()
        except KaspiError as exc:
            # Ошибка одного товара: класс без текста (в тексте могут быть данные Kaspi).
            logger.warning("товар %s: ошибка Kaspi %s", product.id, type(exc).__name__)
            result = None
            reason = (
                SkipReason.RESPONSE_FORMAT
                if isinstance(exc, ResponseFormatError)
                else SkipReason.KASPI_ERROR
            )
            async with ctx.database.tenant(slice_ctx.tenant_id) as scope:
                await scope.products.record_skip(product.id, reason, ctx.clock())
                await scope.commit()
        processed += 1
        async with ctx.database.tenant(slice_ctx.tenant_id) as scope:
            until = ctx.clock() + timedelta(seconds=LEASE_SECONDS)
            renewed = await scope.shops.renew_lease(shop_id, ctx.config.worker_id, until)
            await scope.commit()
        if not renewed:
            logger.warning("аренда магазина %s потеряна: слайс закончен", shop_id)
            break
        if result is not None and result.outcome is WriteOutcome.SKIPPED:
            if result.reason == "quota":
                return await _quota_free_at(slice_ctx)
            if result.reason in STOP_REASONS:
                break
    return None
