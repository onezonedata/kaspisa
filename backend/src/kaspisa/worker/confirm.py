"""Подтверждение записей цены по срокам 30/300/600 с (ADR 0008, раздел 5.5 проекта)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from kaspisa.db import PriceChangeResult, PriceWrite, TenantScope
from kaspisa.kaspi import (
    PRICE_VERIFY_DEADLINE_SECONDS,
    PRICE_VERIFY_FIRST_CHECK_SECONDS,
    PRICE_WRITE_HOLD_SECONDS,
    KaspiError,
    reset_points,
)
from kaspisa.kaspi.models import PriceVerification, ProductSnapshot
from kaspisa.worker.constants import (
    LEASE_SECONDS,
    MAX_VERIFY_PER_SLICE,
    VERIFY_AFTER_DEADLINE_INTERVAL_SECONDS,
    WRITE_ERROR_LIMIT,
)
from kaspisa.worker.context import SliceContext
from kaspisa.worker.snapshot_codec import snapshot_from_json

logger = logging.getLogger(__name__)

# Запись, не проверявшаяся дольше срока запрета и ещё одного интервала, - реальный простой.
EXPIRED_UNCHECKED_SECONDS = PRICE_WRITE_HOLD_SECONDS + VERIFY_AFTER_DEADLINE_INTERVAL_SECONDS


def _next_check(sent_at: datetime, now: datetime) -> datetime:
    """Следующая проверка: до срока подтверждения раз в 30 с, после - раз в 5 минут."""
    elapsed = (now - sent_at).total_seconds()
    if elapsed < PRICE_VERIFY_DEADLINE_SECONDS:
        step = timedelta(seconds=PRICE_VERIFY_FIRST_CHECK_SECONDS)
        return min(now + step, sent_at + timedelta(seconds=PRICE_VERIFY_DEADLINE_SECONDS))
    step = timedelta(seconds=VERIFY_AFTER_DEADLINE_INTERVAL_SECONDS)
    return min(now + step, sent_at + timedelta(seconds=PRICE_WRITE_HOLD_SECONDS))


async def verify_open_writes(ctx: SliceContext) -> bool:
    """Проверяет открытые записи магазина (не больше `MAX_VERIFY_PER_SLICE` за слайс).

    После каждой проверки продлевает аренду; `False` - аренда потеряна, слайс должен
    закончиться. Ошибки доступа (`ShopUnavailable`, 429) идут наверх.
    """
    worker = ctx.worker
    shop_id = ctx.shop.id
    async with worker.database.tenant(ctx.tenant_id) as scope:
        due = list(await scope.price_writes.due_for_verification(shop_id, worker.clock()))
    for write in due[:MAX_VERIFY_PER_SLICE]:
        await _verify_one(ctx, write)
        async with worker.database.tenant(ctx.tenant_id) as scope:
            until = worker.clock() + timedelta(seconds=LEASE_SECONDS)
            renewed = await scope.shops.renew_lease(shop_id, worker.config.worker_id, until)
            await scope.commit()
        if not renewed:
            logger.warning("аренда магазина %s потеряна при подтверждении записей", shop_id)
            return False
    return True


def _has_counted_stock(snapshot: ProductSnapshot) -> bool:
    """Есть ли в чтении снимка точка с остатком не меньше 1: в запись ушёл `stockCount >= 1`."""
    return any(
        point.stock is not None and point.stock >= 1
        for city in snapshot.state.cities
        for point in city.points
    )


def _anomaly_notes(resets: tuple[str, ...], others: tuple[str, ...]) -> list[str]:
    """Примечания журнала: `store_anomalies:<прочие>`, затем `stock_reset:<сбросы>`."""
    notes = [f"store_anomalies:{','.join(others)}"] if others else []
    if resets:
        notes.append(f"stock_reset:{','.join(resets)}")
    return notes


def _split(
    snapshot: ProductSnapshot, verdict: PriceVerification
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Делит аномалии ответа на сброс остатка и прочие: `(resets, others)` (ADR 0037, п. 3в)."""
    anomalies = set(verdict.store_anomalies)
    resets: set[str] = set()
    if verdict.listing is not None:
        resets = set(reset_points(snapshot.state, verdict.listing)) & anomalies
    return tuple(sorted(resets)), tuple(sorted(anomalies - resets))


async def untrack_stock_reset(
    scope: TenantScope, product_id: int, points: tuple[str, ...], now: datetime
) -> bool:
    """Выключает отслеживание товара, у которого сброшен остаток; `False` - уже выключен.

    Предупреждение пишется один раз: повторная обработка выключенного товара ничего не меняет.
    Время и точки сброса (`now`) остаются для уведомления в Telegram (ADR 0038).
    """
    product = await scope.products.get_for_update(product_id)
    if product is None or not product.tracked:
        return False
    product.tracked = False
    product.stock_reset_at = now
    product.stock_reset_points = ",".join(sorted(points))
    logger.warning(
        "товар %s: остаток точек %s стал не указан после записи с остатком, отслеживание выключено",
        product_id,
        ",".join(points),
    )
    return True


async def _verify_stock(ctx: SliceContext, write: PriceWrite) -> None:
    """Вторая проверка остатков подтверждённой записи с остатком (ADR 0037, раздел 3.4).

    Цена уже учтена первой проверкой: серия ошибок и отметки срока не трогаются. Остаток,
    показанный как «не указан» (сброс), выключает товар из отслеживания; прочие аномалии
    останавливают магазин. Журнал получает примечания.
    """
    worker = ctx.worker
    now = worker.clock()
    elapsed = (now - write.sent_at).total_seconds()
    snapshot = snapshot_from_json(write.snapshot)
    expected = {city: int(price) for city, price in write.expected_prices.items()}
    client = worker.client
    try:
        verdict = await ctx.access.cabinet(
            lambda s: client.verify_price(s, snapshot, expected, settled=True)
        )
    except KaspiError as exc:  # проверка не удалась: повтор, пока не прошёл срок запрета
        retry = elapsed < PRICE_WRITE_HOLD_SECONDS
        logger.warning(
            "товар %s: проверка остатков не удалась (%s)%s",
            write.product_id,
            type(exc).__name__,
            "" if retry else ", проверка остатков не выполнена",
        )
        next_at = now + timedelta(seconds=PRICE_VERIFY_FIRST_CHECK_SECONDS) if retry else None
        async with worker.database.tenant(ctx.tenant_id) as scope:
            await scope.price_writes.set_status(
                write.product_id, "confirmed", next_verify_at=next_at
            )
            await scope.commit()
        return
    if verdict.listing is None:
        logger.warning(
            "товар %s: строка товара не найдена, проверка остатков не выполнена",
            write.product_id,
        )
    resets, others = _split(snapshot, verdict)
    notes = _anomaly_notes(resets, others)
    # порядок блокировок: магазин, товар, затем price_write, затем журнал
    async with worker.database.tenant(ctx.tenant_id) as scope:
        if others:
            await scope.shops.halt(write.shop_id, "store_anomalies", now)
        if resets:
            await untrack_stock_reset(scope, write.product_id, resets, worker.clock())
        await scope.price_writes.set_status(write.product_id, "confirmed", next_verify_at=None)
        if notes:
            await scope.price_changes.set_result(
                write.price_change_id, PriceChangeResult.APPLIED, "; ".join(notes)
            )
        await scope.commit()


async def _verify_one(ctx: SliceContext, write: PriceWrite) -> None:
    """Одна проверка записи по 5.5 проекта.

    Срок 300 с отмечается по времени, а не по успеху проверки; последняя проверка - при
    `600 <= t < 900`; при `t >= 900` (простой обработчика) - `expired` без проверки.
    Подтверждённая запись с остатком получает вторую проверку остатков на 300 с (ADR 0037).
    Порядок блокировок в транзакции: магазин, товар, затем `price_write` и журнал.
    """
    worker = ctx.worker
    now = worker.clock()
    sent_at = write.sent_at
    elapsed = (now - sent_at).total_seconds()
    if write.status == "confirmed":  # только вторая проверка остатков: цена уже подтверждена
        await _verify_stock(ctx, write)
        return
    if elapsed >= EXPIRED_UNCHECKED_SECONDS:
        async with worker.database.tenant(ctx.tenant_id) as scope:
            await scope.price_writes.set_status(write.product_id, "expired", next_verify_at=None)
            await scope.price_changes.set_result(
                write.price_change_id, PriceChangeResult.UNCONFIRMED, "expired_unchecked"
            )
            await scope.commit()
        return
    snapshot = snapshot_from_json(write.snapshot)
    expected = {city: int(price) for city, price in write.expected_prices.items()}
    client = worker.client
    with_stock = _has_counted_stock(snapshot)
    settled = with_stock and elapsed >= PRICE_VERIFY_DEADLINE_SECONDS
    verdict = None
    try:
        verdict = await ctx.access.cabinet(
            lambda s: client.verify_price(s, snapshot, expected, settled=settled)
        )
    except KaspiError as exc:  # проверка не удалась: это не «применено»
        logger.warning(
            "товар %s: проверка записи не удалась (%s)", write.product_id, type(exc).__name__
        )
    # срок отмечается один раз; ошибка, учтённая при отправке (`error_counted`), не удваивается
    mark_deadline = elapsed >= PRICE_VERIFY_DEADLINE_SECONDS and not write.deadline_reported
    count_error = mark_deadline and not write.error_counted
    counted = write.error_counted or count_error
    reported = write.deadline_reported or mark_deadline
    async with worker.database.tenant(ctx.tenant_id) as scope:
        if verdict is not None and verdict.applied:
            resets, others = _split(snapshot, verdict)
            anomalies = (*resets, *others)
            late = elapsed >= PRICE_VERIFY_DEADLINE_SECONDS
            notes = ["late_applied"] if late else []
            notes.extend(_anomaly_notes(resets, others))
            if not late:
                await scope.shops.record_write_outcome(
                    write.shop_id, ok=True, limit=WRITE_ERROR_LIMIT, now=now
                )
            elif count_error:  # срок пропущен: одна ошибка в серию
                await scope.shops.record_write_outcome(
                    write.shop_id, ok=False, limit=WRITE_ERROR_LIMIT, now=now
                )
            if others:  # Kaspi изменил склады или остатки: запись магазина останавливается
                await scope.shops.halt(write.shop_id, "store_anomalies", now)
            if resets:  # остаток сброшен: товар выключается из отслеживания (ADR 0037, п. 3в)
                await untrack_stock_reset(scope, write.product_id, resets, worker.clock())
            # запись с остатком, подтверждённая вовремя и без аномалий: вторая проверка на 300 с
            second = with_stock and not late and not anomalies
            await scope.price_writes.set_status(
                write.product_id,
                "confirmed",
                next_verify_at=(
                    sent_at + timedelta(seconds=PRICE_VERIFY_DEADLINE_SECONDS) if second else None
                ),
                deadline_reported=reported,
                error_counted=counted,
            )
            await scope.price_changes.set_result(
                write.price_change_id,
                PriceChangeResult.APPLIED,
                "; ".join(notes) or None,
            )
        elif elapsed < PRICE_VERIFY_DEADLINE_SECONDS:
            await scope.price_writes.set_status(
                write.product_id, write.status, next_verify_at=_next_check(sent_at, now)
            )
        else:
            if count_error:
                await scope.shops.record_write_outcome(
                    write.shop_id, ok=False, limit=WRITE_ERROR_LIMIT, now=now
                )
            resets = ()
            if settled and verdict is not None and verdict.store_anomalies:
                # цена не подтверждена, но остатки записи с остатком изменились (ADR 0037, Н15)
                resets, others = _split(snapshot, verdict)
                if others:
                    await scope.shops.halt(write.shop_id, "store_anomalies", now)
                    logger.warning(
                        "товар %s: запись не подтверждена, точки %s не согласуются с записью",
                        write.product_id,
                        ",".join(others),
                    )
                if resets:
                    await untrack_stock_reset(scope, write.product_id, resets, worker.clock())
            last = elapsed >= PRICE_WRITE_HOLD_SECONDS  # последняя проверка выполнена
            await scope.price_writes.set_status(
                write.product_id,
                "expired" if last else write.status,
                next_verify_at=None if last else _next_check(sent_at, now),
                deadline_reported=reported,
                error_counted=counted,
            )
            if mark_deadline or resets:
                await scope.price_changes.set_result(
                    write.price_change_id,
                    PriceChangeResult.UNCONFIRMED,
                    f"not_confirmed; stock_reset:{','.join(resets)}" if resets else "not_confirmed",
                )
        await scope.commit()
