"""Запись цены: единственная точка `set_price` обработчика (ADR 0008, решения 5-7).

Порядок: границы, режим «без записи» (сервер или магазин не в `live`), свежее чтение
(`get_product`, `find_product`), короткая транзакция (блокировка, перепроверка, квота, запрет
и строка журнала - до отправки), отправка, итог. Запрет `price_write` переживает перезапуск;
снимается только при заведомо не отправленной записи (отказ клиента до сети, 429, сбой
ограничителя до отправки).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from kaspisa.db import BotMode, PriceChangeResult, PriceWrite, Product, Shop, TenantScope
from kaspisa.kaspi import (
    PRICE_VERIFY_DEADLINE_SECONDS,
    KaspiError,
    KaspiSession,
    PriceWriteOutcomeUnknownError,
    RateLimitedError,
    SessionExpiredError,
    SessionStateError,
    WriteRefusal,
    WriteRefusedError,
    reset_points,
)
from kaspisa.kaspi.models import ListedProduct, ProductSnapshot
from kaspisa.pricing import Decision, Reason
from kaspisa.worker.access import throttle
from kaspisa.worker.confirm import untrack_stock_reset
from kaspisa.worker.constants import (
    LEASE_SECONDS,
    PERMANENT_REFUSAL_RETRY_SECONDS,
    PRICE_WRITE_LIMIT,
    PRICE_WRITE_WINDOW_SECONDS,
    RATE_LIMIT_PAUSE_MIN_SECONDS,
    SET_PRICE_TIMEOUT_SECONDS,
    WRITE_ERROR_LIMIT,
    WRITE_MAX_LIMITER_WAIT_SECONDS,
)
from kaspisa.worker.context import SliceContext
from kaspisa.worker.errors import ShopUnavailable
from kaspisa.worker.observation import own_price
from kaspisa.worker.redis_limits import LimiterBusyError, LimiterUnavailableError
from kaspisa.worker.snapshot_codec import (
    listing_fingerprint,
    snapshot_from_json,
    snapshot_to_json,
)

logger = logging.getLogger(__name__)

TRANSIENT_REFUSALS = frozenset(
    {WriteRefusal.STALE_LISTING, WriteRefusal.STORES_CHANGED, WriteRefusal.PRICE_MISMATCH}
)
PERMANENT_PREFIX = "permanent:"
# Сбой ограничителя внутри `set_price`: клиент кладёт имя исключения в `kind`.
LIMITER_KINDS = frozenset({"LimiterUnavailableError", "LimiterBusyError"})


class WriteOutcome(StrEnum):
    DRY_RUN = "dry_run"
    SENT = "sent"
    BLOCKED_BOUNDS = "blocked_bounds"
    SKIPPED = "skipped"
    REFUSED = "refused"
    NOT_SENT_KNOWN = "not_sent_known"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    reason: str | None = None


@dataclass(frozen=True)
class PendingCheck:
    """Копия строки `price_write` с ожидающей поздней проверкой остатков (ADR 0037, Н19)."""

    snapshot: dict[str, Any]
    expected: dict[str, int]
    sent_at: datetime
    price_change_id: int
    job_id: str | None
    next_verify_at: datetime
    deadline_reported: bool
    error_counted: bool


@dataclass(frozen=True)
class Reserved:
    change_id: int
    pending: PendingCheck | None


def _pending_check(previous: PriceWrite) -> PendingCheck | None:
    """Копия подтверждённой записи с ожидающей проверкой простыми значениями; иначе `None`."""
    if previous.status != "confirmed" or previous.next_verify_at is None:
        return None
    return PendingCheck(
        snapshot=dict(previous.snapshot),
        expected={city: int(price) for city, price in previous.expected_prices.items()},
        sent_at=previous.sent_at,
        price_change_id=previous.price_change_id,
        job_id=previous.job_id,
        next_verify_at=previous.next_verify_at,
        deadline_reported=previous.deadline_reported,
        error_counted=previous.error_counted,
    )


def _journal_fields(decision: Decision, competitor_name: str | None) -> dict[str, Any]:
    """Поля строки журнала из решения движка; имя конкурента в решении нет, его даёт вызов."""
    competitor = decision.competitor
    return {
        "reason": decision.reason.value,
        "target_price": decision.target_price,
        "competitor_merchant_id": competitor.merchant_id if competitor else None,
        "competitor_name": competitor_name if competitor else None,
        "competitor_price": competitor.price if competitor else None,
        "capped_at_max": decision.capped_at_max,
        "skipped_competitors": 0,
    }


async def write_price(
    ctx: SliceContext,
    product: Product,
    decision: Decision,
    current_price: int,
    *,
    cities_differ: bool = False,
    competitor_name: str | None = None,
) -> WriteResult:
    """Записывает новую цену товара; единственное место вызова `set_price`.

    Цена одна во всех городах строки списка (ADR 0011, Н12); `current_price` - своя цена
    по правилу `own_price` (ADR 0015). `cities_differ` - по прочитанной строке цены городов
    разные (нужен только сухому прогону: при настоящей записи признак берётся из свежей строки).
    """
    new_price = decision.new_price
    low, high = product.min_price, product.max_price
    if new_price is None or low is None or high is None or not low <= new_price <= high:
        return WriteResult(WriteOutcome.BLOCKED_BOUNDS)
    if new_price == current_price:
        return WriteResult(WriteOutcome.SKIPPED, "same_price")
    # Быстрый путь (ADR 0024): запись запрещена серверу или магазин не в режиме `live`.
    # Решающая проверка режима - под блокировкой магазина, в `_blocked_reason`.
    if ctx.worker.config.dry_run or ctx.shop.bot_mode != BotMode.LIVE:
        await _journal_dry_run(
            ctx, product, decision, current_price, new_price, cities_differ, competitor_name
        )
        return WriteResult(WriteOutcome.DRY_RUN)

    client = ctx.worker.client
    shop = ctx.shop
    merchant_uid = shop.merchant_uid
    shop_city = shop.city_id
    if merchant_uid is None:
        raise ShopUnavailable
    sku = product.sku
    # Аренда продлевается один раз перед цепочкой; дальше подряд, без других дел: чтение,
    # строка списка, снимок, отправка. Аренду между ними проверяет транзакция `_reserve`.
    await ctx.access.renew_lease()
    state = await ctx.access.cabinet(
        lambda s: client.get_product(s, merchant_uid, sku), renew=False
    )
    fresh = await ctx.access.cabinet(
        lambda s: client.find_product(s, merchant_uid, sku), renew=False
    )
    if fresh is None:
        await _journal_failed(
            ctx, product, decision, current_price, new_price, "listing_missing", competitor_name
        )
        return WriteResult(WriteOutcome.SKIPPED, "listing_missing")
    fresh_prices = {item.city_id: item.value for item in fresh.city_prices}
    reference = own_price(fresh_prices, shop_city) if shop_city is not None else None
    if reference is None:
        return WriteResult(WriteOutcome.SKIPPED, "own_price_unknown")
    if reference != current_price:
        return WriteResult(WriteOutcome.SKIPPED, "price_changed")
    aligned = len(set(fresh_prices.values())) > 1
    snapshot = ProductSnapshot(state, fresh)
    city_prices = {city_id: new_price for city_id in fresh_prices}

    # Окно между проверкой границ и отправкой не длиннее ожидания ограничителя записи.
    waiting = await ctx.worker.limiter.pending_delay(f"mc:{merchant_uid}")
    if waiting > WRITE_MAX_LIMITER_WAIT_SECONDS:
        return WriteResult(WriteOutcome.SKIPPED, "limiter_busy")
    reserved = await _reserve(
        ctx,
        product,
        decision,
        current_price,
        new_price,
        snapshot,
        city_prices,
        aligned,
        competitor_name,
    )
    if isinstance(reserved, WriteResult):
        return reserved
    change_id = reserved.change_id
    pending = reserved.pending

    async def send(session: KaspiSession) -> Any:
        async with asyncio.timeout(SET_PRICE_TIMEOUT_SECONDS):
            return await client.set_price(session, snapshot, city_prices)

    refusal: WriteRefusal | None = None
    try:
        receipt = await ctx.access.cabinet_write(send)
    except asyncio.CancelledError:
        await asyncio.shield(_mark_unknown(ctx, product, change_id, "cancelled", count=True))
        raise
    except WriteRefusedError as exc:
        refusal = exc.reason
    except RateLimitedError as exc:
        await _discard(
            ctx, product, change_id, None, decision, current_price, new_price, None, pending=pending
        )
        await throttle(ctx.worker, f"mc:{merchant_uid}", exc)
    except (LimiterUnavailableError, LimiterBusyError):
        await _discard(
            ctx, product, change_id, None, decision, current_price, new_price, None, pending=pending
        )
        raise
    except SessionExpiredError:
        # 3xx и 401 на записи: «не применено» не доказано, запрет остаётся; это ошибка доступа
        await _mark_unknown(ctx, product, change_id, "session_expired", count=False)
        return WriteResult(WriteOutcome.UNKNOWN, "session_expired")
    except SessionStateError:  # фаза сессии проверена до отправки: ничего не ушло
        await _discard(
            ctx, product, change_id, None, decision, current_price, new_price, None, pending=pending
        )
        raise ShopUnavailable from None
    except PriceWriteOutcomeUnknownError as exc:
        if exc.kind not in LIMITER_KINDS:  # иначе сбой ограничителя до отправки
            await _mark_unknown(ctx, product, change_id, f"unknown:{exc.kind}", count=True)
            return WriteResult(WriteOutcome.UNKNOWN, exc.kind)
        await _discard(
            ctx, product, change_id, None, decision, current_price, new_price, None, pending=pending
        )
        if exc.kind == "LimiterBusyError":
            raise LimiterBusyError(RATE_LIMIT_PAUSE_MIN_SECONDS) from None
        raise LimiterUnavailableError(exc.kind) from None
    except (KaspiError, TimeoutError) as exc:
        name = type(exc).__name__
        await _mark_unknown(ctx, product, change_id, name, count=True)
        return WriteResult(WriteOutcome.UNKNOWN, name)
    else:
        await _mark_sent(ctx, product, receipt.job_id)
        return WriteResult(WriteOutcome.SENT)

    if refusal is None:  # сюда доходит только отказ клиента
        raise AssertionError
    await _discard(
        ctx,
        product,
        change_id,
        fresh,
        decision,
        current_price,
        new_price,
        refusal,
        competitor_name=competitor_name,
        pending=pending,
    )
    return WriteResult(WriteOutcome.REFUSED, refusal.value)


# --- журнал без отправки ---


_WRITE_CODES = {
    WriteOutcome.SENT: "sent",
    WriteOutcome.DRY_RUN: "dry_run",
    WriteOutcome.REFUSED: "refused",
    WriteOutcome.UNKNOWN: "unknown",
    WriteOutcome.NOT_SENT_KNOWN: "not_sent",
    WriteOutcome.BLOCKED_BOUNDS: "bounds",
}


def write_code(result: WriteResult) -> str:
    """Код итога записи для товара (`decision_write`); у `SKIPPED` - код причины пропуска."""
    if result.outcome is WriteOutcome.SKIPPED:
        return result.reason or "skipped"
    return _WRITE_CODES[result.outcome]


async def journal_no_change(
    scope: TenantScope,
    product_id: int,
    decision: Decision,
    current: int,
    competitor_name: str | None,
) -> bool:
    """Строка `no_change` для решения «не менять»; повтор тех же причины и цифр не пишется.

    Первым делом блокирует товар: два обработчика (или обработчик и правка через API) не
    прочитают одну последнюю строку и не добавят дубль. Возвращает `True`, если строка добавлена.
    """
    await scope.products.get_for_update(product_id)
    competitor = decision.competitor
    competitor_price = competitor.price if competitor else None
    last = await scope.price_changes.list_for_product(product_id, limit=1)
    if last:
        row = last[0]
        if row.result == PriceChangeResult.NO_CHANGE.value and (
            row.reason,
            row.old_price,
            row.target_price,
            row.competitor_price,
        ) == (decision.reason.value, current, decision.target_price, competitor_price):
            return False
        # после изменения цены «уже равна цели» / «на минимальной»: журнал это уже сказал
        if (
            decision.reason in (Reason.ALREADY_AT_TARGET, Reason.AT_MIN_PRICE)
            and row.result in (PriceChangeResult.APPLIED.value, PriceChangeResult.UNCONFIRMED.value)
            and row.new_price == current
            and row.target_price == decision.target_price
        ):
            return False
    await scope.price_changes.add(
        product_id=product_id,
        old_price=current,
        new_price=current,
        result=PriceChangeResult.NO_CHANGE,
        cities_aligned=False,
        **_journal_fields(decision, competitor_name),
    )
    return True


async def _journal_dry_run(
    ctx: SliceContext,
    product: Product,
    decision: Decision,
    current: int,
    new: int,
    aligned: bool,
    competitor_name: str | None,
) -> None:
    """Строка `dry_run`; то же решение подряд не дублируется."""
    async with ctx.worker.database.tenant(ctx.tenant_id) as scope:
        last = await scope.price_changes.list_for_product(product.id, limit=1)
        if last:
            row = last[0]
            same = (row.result, row.old_price, row.new_price, row.reason) == (
                PriceChangeResult.DRY_RUN.value,
                current,
                new,
                decision.reason.value,
            )
            if same:
                return
        await scope.price_changes.add(
            product_id=product.id,
            old_price=current,
            new_price=new,
            result=PriceChangeResult.DRY_RUN,
            cities_aligned=aligned,
            **_journal_fields(decision, competitor_name),
        )
        await scope.commit()


async def _journal_failed(
    ctx: SliceContext,
    product: Product,
    decision: Decision,
    current: int,
    new: int,
    error: str,
    competitor_name: str | None,
) -> None:
    """Строка `failed` без отправки; та же причина подряд не дублируется."""
    async with ctx.worker.database.tenant(ctx.tenant_id) as scope:
        last = await scope.price_changes.list_for_product(product.id, limit=1)
        if last and (last[0].result, last[0].error) == (PriceChangeResult.FAILED.value, error):
            return
        await scope.price_changes.add(
            product_id=product.id,
            old_price=current,
            new_price=new,
            result=PriceChangeResult.FAILED,
            error=error,
            **_journal_fields(decision, competitor_name),
        )
        await scope.commit()


# --- запись-вперёд ---


def _blocked_reason(shop: Shop, product: Product | None, new_price: int) -> str | None:
    """Перепроверка под блокировкой: границы и признаки могли измениться после расчёта."""
    if product is None or not product.tracked:
        return "not_tracked"
    low, high = product.min_price, product.max_price
    if low is None or high is None or not low <= new_price <= high:
        return "bounds"
    if shop.connection_state != "connected":
        return "not_connected"
    if shop.halt_reason is not None:
        return "halted"
    # режим мог смениться после расчёта: решает строка магазина под блокировкой
    if shop.bot_mode == BotMode.OFF:
        return "bot_disabled"
    if shop.bot_mode != BotMode.LIVE:
        return "not_live"
    return None


async def _reserve(
    ctx: SliceContext,
    product: Product,
    decision: Decision,
    current: int,
    new: int,
    snapshot: ProductSnapshot,
    city_prices: dict[str, int],
    aligned: bool,
    competitor_name: str | None,
) -> Reserved | WriteResult:
    """Короткая транзакция: запрет и строка журнала записываются ДО отправки.

    Здесь же продлевается аренда (последняя проверка перед отправкой без лишнего вызова).
    Блокировка магазина и товара снимается коммитом и не держится через сеть (ADR 0008, Н3).
    Возвращает номер строки журнала и копию заменяемой строки с ожидающей поздней проверкой
    (для восстановления при компенсации) или причину пропуска (без записи в базу).
    """
    async with ctx.worker.database.tenant(ctx.tenant_id) as scope:
        shop = await scope.shops.lock_for_write(product.shop_id)
        # аренда потеряна: другой экземпляр уже вправе вести магазин, отправки нет
        until = ctx.worker.clock() + timedelta(seconds=LEASE_SECONDS)
        if not await scope.shops.renew_lease(shop.id, ctx.worker.config.worker_id, until):
            return WriteResult(WriteOutcome.SKIPPED, "lease_lost")
        locked = await scope.products.get_for_update(product.id)
        reason = _blocked_reason(shop, locked, new)
        if reason is not None:
            return WriteResult(WriteOutcome.SKIPPED, reason)
        now = ctx.worker.clock()
        # Прежняя запись с остатком ждёт поздней проверки, а остаток в списке уже сброшен:
        # новая запись затёрла бы эту проверку (ADR 0037, п. 3а).
        previous = await scope.price_writes.get(product.id)
        pending: PendingCheck | None = None
        if previous is not None and previous.next_verify_at is not None:
            points = reset_points(snapshot_from_json(previous.snapshot).state, snapshot.listing)
            if points:  # остаток сброшен: товар выключается, магазин не останавливается
                await untrack_stock_reset(scope, product.id, points, now)
                await scope.commit()
                return WriteResult(WriteOutcome.SKIPPED, "not_tracked")
            pending = _pending_check(previous)
        window_start = now - timedelta(seconds=PRICE_WRITE_WINDOW_SECONDS)
        if await scope.price_changes.count_sent_since(shop.id, window_start) >= PRICE_WRITE_LIMIT:
            return WriteResult(WriteOutcome.SKIPPED, "quota")
        change = await scope.price_changes.add(
            product_id=product.id,
            old_price=current,
            new_price=new,
            result=PriceChangeResult.UNCONFIRMED,
            created_at=now,
            cities_aligned=aligned,
            **_journal_fields(decision, competitor_name),
        )
        opened = await scope.price_writes.open(
            product.id,
            snapshot=snapshot_to_json(snapshot),
            expected=city_prices,
            sent_at=now,
            price_change_id=change.id,
            now=now,
        )
        if not opened:  # запрет ещё действует; транзакция откатывается без commit
            return WriteResult(WriteOutcome.SKIPPED, "hold")
        change_id = change.id
        await scope.commit()
    return Reserved(change_id, pending)


async def _mark_sent(ctx: SliceContext, product: Product, job_id: str) -> None:
    """Квитанция получена: статус `receipt`; запрет держится, строка журнала ждёт подтверждения."""
    try:
        async with ctx.worker.database.tenant(ctx.tenant_id) as scope:
            # Порядок блокировок как в транзакции записи: сначала товар, потом price_write.
            await scope.products.clear_refusal(product.id)
            await scope.price_writes.set_status(product.id, "receipt", job_id=job_id)
            await scope.commit()
    except Exception as exc:  # запись ушла; запрет остаётся в состоянии `sending`
        logger.error("товар %s: статус записи не сохранён (%s)", product.id, type(exc).__name__)


async def _mark_unknown(
    ctx: SliceContext, product: Product, change_id: int, error: str, *, count: bool
) -> None:
    """Исход неизвестен: запрет остаётся; `count` - входит ли случай в серию ошибок записи.

    Учтённая при отправке ошибка помечается `error_counted`: на сроке 300 с её не повторяют.
    """
    try:
        async with ctx.worker.database.tenant(ctx.tenant_id) as scope:
            # Порядок блокировок везде один: магазин, товар, price_write.
            if count:
                await scope.shops.record_write_outcome(
                    product.shop_id, ok=False, limit=WRITE_ERROR_LIMIT, now=ctx.worker.clock()
                )
            await scope.products.clear_refusal(product.id)
            if count:
                await scope.price_writes.set_status(product.id, "unknown", error_counted=True)
            else:
                await scope.price_writes.set_status(product.id, "unknown")
            await scope.price_changes.set_result(change_id, PriceChangeResult.UNCONFIRMED, error)
            await scope.commit()
    except Exception as exc:  # статус останется `sending`: запрет не снимается
        logger.error("товар %s: итог записи не сохранён (%s)", product.id, type(exc).__name__)


async def _discard(
    ctx: SliceContext,
    product: Product,
    change_id: int,
    listing: ListedProduct | None,
    decision: Decision,
    current: int,
    new: int,
    refusal: WriteRefusal | None,
    *,
    competitor_name: str | None = None,
    pending: PendingCheck | None = None,
) -> None:
    """Компенсация заведомо не отправленной записи; при отказе - учёт отказа (5.6).

    Порядок: строка запрета, строка журнала, затем отказ (внешний ключ без каскада).
    Если новая запись заменила строку с ожидающей поздней проверкой, строка возвращается
    (ADR 0037, Н19). Сбой компенсации (база недоступна): запрет остаётся, серия ошибок не растёт.
    """
    try:
        async with ctx.worker.database.tenant(ctx.tenant_id) as scope:
            await scope.products.get_for_update(product.id)  # товар раньше price_write
            await scope.price_writes.delete(product.id)
            await scope.price_changes.discard_unsent(change_id)
            if pending is not None:
                await scope.price_writes.open(
                    product.id,
                    snapshot=pending.snapshot,
                    expected=pending.expected,
                    sent_at=pending.sent_at,
                    price_change_id=pending.price_change_id,
                    now=ctx.worker.clock(),
                )
                await scope.price_writes.set_status(
                    product.id,
                    "confirmed",
                    job_id=pending.job_id,
                    next_verify_at=pending.next_verify_at,
                    deadline_reported=pending.deadline_reported,
                    error_counted=pending.error_counted,
                )
            if refusal is not None and listing is not None:
                await _record_refusal(
                    scope,
                    ctx.worker.clock(),
                    product,
                    refusal,
                    listing,
                    decision,
                    current,
                    new,
                    competitor_name,
                )
            await scope.commit()
    except Exception as exc:
        logger.error(
            "товар %s: компенсация записи не выполнена (%s)", product.id, type(exc).__name__
        )


async def _record_refusal(
    scope: TenantScope,
    now: datetime,
    product: Product,
    refusal: WriteRefusal,
    listing: ListedProduct,
    decision: Decision,
    current: int,
    new: int,
    competitor_name: str | None,
) -> None:
    """Учёт отказа клиента: временные - серия до постоянного, прочие - до смены отпечатка."""
    locked = await scope.products.get_for_update(product.id)
    if locked is None:
        return
    previous = locked.refusal_reason  # UPDATE ниже меняет и загруженный объект
    if refusal in TRANSIENT_REFUSALS:
        since = locked.refusal_since
        if since is not None and await scope.price_changes.has_sent_since(product.id, since):
            since = None  # после начала серии была отправка: серия заново
        since = since or now
        permanent = (now - since).total_seconds() > PRICE_VERIFY_DEADLINE_SECONDS
        label = f"{PERMANENT_PREFIX}{refusal.value}" if permanent else refusal.value
        retry_at = now + timedelta(seconds=PERMANENT_REFUSAL_RETRY_SECONDS) if permanent else None
        await scope.products.set_refusal(
            product.id, reason=label, since=since, fingerprint=None, retry_at=retry_at
        )
    else:
        label = refusal.value
        fingerprint = listing_fingerprint(listing, locked.min_price, locked.max_price)
        await scope.products.set_refusal(
            product.id, reason=label, since=None, fingerprint=fingerprint, retry_at=None
        )
    if previous != label:  # журнал отказов не дублируется
        await scope.price_changes.add(
            product_id=product.id,
            old_price=current,
            new_price=new,
            result=PriceChangeResult.FAILED,
            error=label,
            **_journal_fields(decision, competitor_name),
        )
