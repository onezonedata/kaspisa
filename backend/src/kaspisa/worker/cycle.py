"""Цикл товара: строка списка, продавцы, решение движка, наблюдения, запись цены."""

from __future__ import annotations

import logging
from datetime import timedelta

from kaspisa.db import Product, SkipReason, TenantScope, pricing_settings_for
from kaspisa.kaspi import PRICE_WRITE_HOLD_SECONDS, RateLimitedError, WriteRefusal
from kaspisa.pricing import Decision, Offer, Reason, decide_price
from kaspisa.worker.access import throttle
from kaspisa.worker.context import SliceContext
from kaspisa.worker.observation import (
    Observation,
    cheapest_competitor,
    more_offers_unseen,
    observe,
    own_price,
    to_pricing_offers,
)
from kaspisa.worker.snapshot_codec import listing_fingerprint
from kaspisa.worker.writer import (
    WriteOutcome,
    WriteResult,
    journal_no_change,
    write_code,
    write_price,
)

logger = logging.getLogger(__name__)

OFFERS_LIMITER_KEY = "kaspi.kz"
HOLDING = ("sending", "receipt", "unknown")


async def _hold_active(ctx: SliceContext, product: Product) -> bool:
    """Запрет новой записи (дешёвая проверка по базе до обращения к Kaspi)."""
    worker = ctx.worker
    async with worker.database.tenant(ctx.tenant_id) as scope:
        write = await scope.price_writes.get(product.id)
    if write is None or write.status not in HOLDING:
        return False
    return worker.clock() < write.sent_at + timedelta(seconds=PRICE_WRITE_HOLD_SECONDS)


type Basis = tuple[int | None, int | None, int, bool, bool, bool]
# итоги записи без отправки в Kaspi: решение при них зависит только от настроек товара
NOT_SENT = frozenset(
    {
        WriteOutcome.DRY_RUN,
        WriteOutcome.BLOCKED_BOUNDS,
        WriteOutcome.SKIPPED,
        WriteOutcome.NOT_SENT_KNOWN,
    }
)


def _basis(product: Product) -> Basis:
    """Настройки товара, от которых зависит решение: границы, шаг, отслеживание, авто."""
    return (
        product.min_price,
        product.max_price,
        product.step,
        product.tracked,
        product.auto_decrease,
        product.auto_raise,
    )


async def _settings_unchanged(scope: TenantScope, product: Product, basis: Basis) -> bool:
    """Под блокировкой товара: настройки те же, по которым считалось решение (ADR 0020).

    Иначе правка через API уже сбросила решение, и старое поверх сброса показало бы неверный текст.
    """
    fresh = await scope.products.get_for_update(product.id)
    return fresh is not None and _basis(fresh) == basis


async def check_product(ctx: SliceContext, product: Product) -> WriteResult | None:
    """Один товар до решения и записи. Исключения магазина (`ShopUnavailable`...) идут наверх."""
    shop = ctx.shop
    worker = ctx.worker
    client = worker.client
    merchant_uid = shop.merchant_uid
    city_id = shop.city_id
    if merchant_uid is None or city_id is None:  # условия захвата слайса
        raise ValueError("у магазина нет merchant_uid или города")

    async def finish(
        *,
        observation: tuple[int, Observation] | None = None,
        skip: SkipReason | None = None,
        no_change: tuple[Decision, str | None, Basis] | None = None,
    ) -> None:
        """Итог проверки в товаре. `no_change` - решение «не менять» и имя конкурента.

        Для решения «менять» решение в товар не пишется: его пишет `record_write` после записи.
        """
        now = worker.clock()
        async with worker.database.tenant(ctx.tenant_id) as scope:
            if skip is not None:
                await scope.products.record_skip(product.id, skip, now)
            elif observation is not None:
                price, seen = observation
                reason: str | None = None
                target: int | None = None
                if no_change is not None:
                    verdict, competitor_name, basis = no_change
                    # блокировка товара - первой строкой: журнал и решение в одной транзакции
                    if not await _settings_unchanged(scope, product, basis):
                        return  # товар правили: ни решения, ни строки; сброс остаётся
                    await journal_no_change(scope, product.id, verdict, price, competitor_name)
                    reason, target = verdict.reason.value, verdict.target_price
                await scope.products.record_observation(
                    product.id,
                    current_price=price,
                    position=seen.position,
                    position_over=seen.position_over,
                    competitors_count=seen.competitors_count,
                    first_place_price=seen.first_place_price,
                    decision_reason=reason,
                    decision_target_price=target,
                    checked_at=now,
                )
            else:
                await scope.products.touch_checked(product.id, now)
            await scope.commit()

    if await _hold_active(ctx, product):
        await finish()
        return None
    listing = await ctx.access.cabinet(lambda s: client.find_product(s, merchant_uid, product.sku))
    if listing is None:  # причина хранится в товаре (ADR 0017); отказ записи она не затирает
        logger.info("товар %s: строки в списке нет (listing_missing)", product.id)
        await finish(skip=SkipReason.LISTING_MISSING)
        return None
    city_prices = {item.city_id: item.value for item in listing.city_prices}
    price = own_price(city_prices, city_id)
    if price is None:  # цены городов строки разные, города сверки среди них нет
        logger.info("товар %s: своя цена по строке неизвестна (own_price_unknown)", product.id)
        await finish(skip=SkipReason.OWN_PRICE_UNKNOWN)
        return None
    cities_differ = len(set(city_prices.values())) > 1
    fingerprint = listing_fingerprint(listing, product.min_price, product.max_price)
    if product.refusal_reason == WriteRefusal.STOCK_COUNTED.value:
        # клиент больше не отказывает по остатку (ADR 0037): старый отказ снимается сразу
        async with worker.database.tenant(ctx.tenant_id) as scope:
            await scope.products.clear_refusal(product.id)
            await scope.commit()
    elif product.refusal_fingerprint is not None and fingerprint == product.refusal_fingerprint:
        await finish()  # данные товара не менялись с отказа записи
        return None

    await ctx.access.renew_lease()
    try:
        # конкуренты - из карточки города сверки, своя цена - из городов строки (ADR 0015);
        # одна-две страницы по 10 продавцов (ADR 0016)
        page = await client.get_offers(product.master_sku, city_id, merchant_uid)
    except RateLimitedError as exc:
        await throttle(worker, OFFERS_LIMITER_KEY, exc)
    basis = _basis(product)
    if product.min_price is None or product.max_price is None:
        # без границ: цены конкурентов и решение «границы не заданы», движок и запись не вызываются
        seen = observe(page, own_merchant_id=merchant_uid, own_price=price)
        cheapest = cheapest_competitor(
            page.offers, merchant_uid, frozenset(shop.excluded_merchant_ids)
        )
        verdict = Decision(
            new_price=None,
            reason=Reason.BOUNDS_MISSING,
            target_price=None,
            competitor=(
                None
                if cheapest is None
                else Offer(merchant_id=cheapest.merchant_id, price=cheapest.price)
            ),
        )
        await finish(
            observation=(price, seen),
            no_change=(
                verdict,
                None if cheapest is None else cheapest.merchant_name,
                basis,
            ),
        )
        return None
    settings = pricing_settings_for(shop, product)
    if settings is None:  # границы заданы, merchant_uid проверен выше: сюда не попасть
        raise ValueError("настройки цены не собраны при заданных границах")
    try:
        decision = decide_price(
            price,
            to_pricing_offers(page.offers),
            settings,
            more_offers_unseen=more_offers_unseen(page),
        )
    except ValueError:
        logger.warning("решение по товару %s не рассчитано: вход отвергнут движком", product.id)
        await finish(skip=SkipReason.ENGINE_REJECTED)
        return None
    seen = observe(page, own_merchant_id=merchant_uid, own_price=price)
    competitor_name: str | None = None
    if decision.competitor is not None:  # имя - из прочитанных предложений карточки (ADR 0017)
        names = {o.merchant_id: o.merchant_name for o in page.offers}
        competitor_name = names.get(decision.competitor.merchant_id)
    new_price = decision.new_price
    if new_price is None:
        await finish(observation=(price, seen), no_change=(decision, competitor_name, basis))
        return None
    await finish(observation=(price, seen))  # решение «менять» - после записи, с её итогом
    result = await write_price(
        ctx, product, decision, price, cities_differ=cities_differ, competitor_name=competitor_name
    )
    if result.reason == "lease_lost":  # магазин ведёт другой обработчик: его итог не трогаем
        return result
    async with worker.database.tenant(ctx.tenant_id) as scope:
        if result.outcome in NOT_SENT and not await _settings_unchanged(scope, product, basis):
            return result  # товар правили, пока считали: решение без отправки не сохраняем
        await scope.products.record_write(
            product.id,
            decision_reason=decision.reason.value,
            decision_target_price=decision.target_price or new_price,
            decision_write=write_code(result),
            checked_at=worker.clock(),
        )
        await scope.commit()
    return result
