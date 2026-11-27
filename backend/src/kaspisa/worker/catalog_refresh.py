"""Суточная загрузка каталога обработчиком (ADR 0010, Н7).

Шаг слайса после проверки цен. Тот же код слияния и та же блокировка, что у кнопки API.
Сбой пишется в лог (только класс исключения) и ставит паузу `catalog_retry_at`; серию
ошибок, останавливающую бота, он не трогает.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta

from redis.exceptions import RedisError

from kaspisa.db import CATALOG_REFRESH_SECONDS, LOGIN_HALT_REASON, Shop
from kaspisa.kaspi import RateLimitedError
from kaspisa.worker import catalog
from kaspisa.worker.constants import (
    CATALOG_CALL_TIMEOUT_SECONDS,
    CATALOG_LOCK_BUSY_SECONDS,
    CATALOG_LOCK_TTL_SECONDS,
    CATALOG_RETRY_SECONDS,
    LOGIN_BACKOFF_DEFAULT_SECONDS,
    RATE_LIMIT_PAUSE_MIN_SECONDS,
)
from kaspisa.worker.context import SliceContext
from kaspisa.worker.errors import ShopUnavailable
from kaspisa.worker.redis_limits import LimiterBusyError, LimiterUnavailableError, SyncLock

logger = logging.getLogger(__name__)


def catalog_due(shop: Shop, now: datetime) -> bool:
    """Условия загрузки (те же, что у захвата по каталогу в `SystemScope.claim_shop`)."""
    synced, retry = shop.catalog_synced_at, shop.catalog_retry_at
    return (
        shop.connection_state == "connected"
        and shop.merchant_uid is not None
        and shop.halt_reason != LOGIN_HALT_REASON
        and (synced is None or synced <= now - timedelta(seconds=CATALOG_REFRESH_SECONDS))
        and (retry is None or retry <= now)
    )


async def _pause(slice_ctx: SliceContext, seconds: int) -> None:
    ctx = slice_ctx.worker
    async with ctx.database.tenant(slice_ctx.tenant_id) as scope:
        await scope.shops.defer_catalog(slice_ctx.shop.id, ctx.clock() + timedelta(seconds=seconds))
        await scope.commit()


async def refresh_catalog_if_due(slice_ctx: SliceContext, stop: asyncio.Event | None) -> None:
    """Загружает каталог, если пора. Не бросает исключений магазина: слайс не откладывается."""
    ctx = slice_ctx.worker
    if stop is not None and stop.is_set():
        return
    await slice_ctx.access.refresh()
    if slice_ctx.access.session_stale:  # переподключение посреди слайса: сессия устарела
        return
    shop = slice_ctx.shop
    if not catalog_due(shop, ctx.clock()):
        return
    lock = SyncLock(ctx.redis, prefix=ctx.limiter.prefix)
    try:
        token = await lock.acquire(shop.id, CATALOG_LOCK_TTL_SECONDS * 1000)
    except (RedisError, OSError):
        logger.warning("загрузка каталога магазина %s: Redis недоступен", shop.id)
        await _pause(slice_ctx, LOGIN_BACKOFF_DEFAULT_SECONDS)
        return
    if token is None:  # загрузку ведёт кнопка или другой обработчик
        await _pause(slice_ctx, CATALOG_LOCK_BUSY_SECONDS)
        return
    try:
        await _load(slice_ctx)
    finally:
        try:
            await lock.release(shop.id, token)
        except (RedisError, OSError):  # блокировка истечёт сама
            logger.warning("блокировка загрузки каталога магазина %s не снята", shop.id)


async def _load(slice_ctx: SliceContext) -> None:
    ctx = slice_ctx.worker
    shop = slice_ctx.shop
    merchant_uid = shop.merchant_uid
    if merchant_uid is None:  # проверено в catalog_due
        return
    pause = CATALOG_RETRY_SECONDS
    try:
        async with asyncio.timeout(CATALOG_CALL_TIMEOUT_SECONDS):
            listed = await slice_ctx.access.cabinet_background(
                lambda session: ctx.client.list_products(session, merchant_uid)
            )
        async with ctx.database.tenant(slice_ctx.tenant_id) as scope:
            await catalog.merge_catalog(
                scope, shop.id, listed, limit=catalog.CATALOG_MAX, now=ctx.clock()
            )
            await scope.commit()
        return
    except ShopUnavailable:  # аренда потеряна или подключение не позволяет запрос
        return
    except LimiterBusyError as busy:
        pause = max(math.ceil(busy.wait_seconds), 1)
        error: Exception = busy
    except LimiterUnavailableError as exc:
        pause = LOGIN_BACKOFF_DEFAULT_SECONDS
        error = exc
    except RateLimitedError as exc:
        limit_pause = max(exc.retry_after or 0, RATE_LIMIT_PAUSE_MIN_SECONDS)
        await ctx.limiter.penalize(f"mc:{merchant_uid}", limit_pause)
        pause = max(limit_pause, CATALOG_RETRY_SECONDS)
        error = exc
    except Exception as exc:  # без текста: он может содержать данные Kaspi
        error = exc
    logger.warning("загрузка каталога магазина %s: %s", shop.id, type(error).__name__)
    await _pause(slice_ctx, pause)
    try:
        await slice_ctx.access.persist_session()
    except Exception as exc:  # пауза уже стоит; текст исключения не пишем
        logger.warning("сессия магазина %s не сохранена: %s", shop.id, type(exc).__name__)
