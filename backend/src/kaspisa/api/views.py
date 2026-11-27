"""Сборка ответов из моделей базы. Секретных полей нет, расшифровки нет."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import NamedTuple

from kaspisa.api.schemas import PendingWrite, PriceChangeOut, ProductOut, ShopOut
from kaspisa.db import BotMode, PriceChange, PriceWrite, PriceWriteStatus, Product, Shop
from kaspisa.kaspi import PRICE_WRITE_HOLD_SECONDS
from kaspisa.kaspi.models import OffersPage
from kaspisa.worker.observation import more_offers_unseen, position_of

logger = logging.getLogger("kaspisa.api")

_HOLDING = frozenset(
    {
        PriceWriteStatus.SENDING.value,
        PriceWriteStatus.RECEIPT.value,
        PriceWriteStatus.UNKNOWN.value,
    }
)


def shop_out(
    shop: Shop,
    *,
    resend_available_in_seconds: int | None,
    restart_required: bool,
    catalog_loading: bool,
    server_dry_run: bool | None,
) -> ShopOut:
    return ShopOut(
        id=shop.id,
        merchant_uid=shop.merchant_uid,
        connection_state=shop.connection_state,
        connection_error=shop.connection_error,
        connection_changed_at=shop.connection_changed_at,
        resend_available_in_seconds=resend_available_in_seconds,
        restart_required=restart_required,
        halt_reason=shop.halt_reason,
        halted_at=shop.halted_at,
        strategy=shop.strategy,
        city_id=shop.city_id,
        excluded_merchant_ids=list(shop.excluded_merchant_ids),
        default_step=shop.default_step,
        auto_raise_enabled=shop.auto_raise_enabled,
        bot_mode=BotMode(shop.bot_mode),
        server_dry_run=server_dry_run,
        created_at=shop.created_at,
        catalog_synced_at=shop.catalog_synced_at,
        catalog_loading=catalog_loading,
    )


def pending_write(product: Product, write: PriceWrite | None, now: datetime) -> PendingWrite | None:
    """Открытая запись цены: тот же предикат, что у `PriceWriteRepository.open`."""
    if write is None or write.status not in _HOLDING:
        return None
    hold_until = write.sent_at + timedelta(seconds=PRICE_WRITE_HOLD_SECONDS)
    if now >= hold_until:
        return None
    prices = dict(write.expected_prices)
    outside = any(
        (product.min_price is not None and price < product.min_price)
        or (product.max_price is not None and price > product.max_price)
        for price in prices.values()
    )
    return PendingWrite(
        status=write.status,
        sent_at=write.sent_at,
        hold_until=hold_until,
        prices=prices,
        outside_bounds=outside,
    )


class PageObservation(NamedTuple):
    """Поля наблюдения по публичной карточке: те же формулы, что у обработчика (ADR 0033)."""

    current_price: int | None
    position: int | None
    position_over: int | None
    competitors_count: int
    first_place_price: int | None


def observation_from_page(page: OffersPage, merchant_uid: str) -> PageObservation:
    """Своя цена - первое предложение своего магазина; нет своего - цена и позиция пусты.

    Кнопка «Узнать цены» при пустой цене прежнюю цену товара не стирает (ADR 0033).
    """
    own = next((o for o in page.offers if o.merchant_id == merchant_uid), None)
    own_price = own.price if own is not None else None
    position = None
    if own_price is not None:
        position = position_of(page.offers, merchant_uid, own_price)
    position_over = None
    if own is None and page.offers and more_offers_unseen(page):
        position_over = len(page.offers)
    return PageObservation(
        current_price=own_price,
        position=position,
        position_over=position_over,
        competitors_count=max(0, page.total - (1 if own is not None else 0)),
        first_place_price=min((o.price for o in page.offers), default=None),
    )


def product_out(product: Product, pending: PendingWrite | None = None) -> ProductOut:
    return ProductOut(
        id=product.id,
        shop_id=product.shop_id,
        sku=product.sku,
        master_sku=product.master_sku,
        title=product.title,
        tracked=product.tracked,
        auto_decrease=product.auto_decrease,
        auto_raise=product.auto_raise,
        min_price=product.min_price,
        max_price=product.max_price,
        step=product.step,
        current_price=product.current_price,
        position=product.position,
        position_over=product.position_over,
        competitors_count=product.competitors_count,
        first_place_price=product.first_place_price,
        last_checked_at=product.last_checked_at,
        refusal_reason=product.refusal_reason,
        refusal_since=product.refusal_since,
        retry_at=product.retry_at,
        skip_reason=product.skip_reason,
        skipped_at=product.skipped_at,
        decision_reason=product.decision_reason,
        decision_target_price=product.decision_target_price,
        decision_write=product.decision_write,
        pending_write=pending,
    )


def price_change_out(change: PriceChange, *, sku: str, title: str) -> PriceChangeOut:
    return PriceChangeOut(
        id=change.id,
        product_id=change.product_id,
        sku=sku,
        title=title,
        created_at=change.created_at,
        old_price=change.old_price,
        new_price=change.new_price,
        reason=change.reason,
        target_price=change.target_price,
        competitor_merchant_id=change.competitor_merchant_id,
        competitor_name=change.competitor_name,
        competitor_price=change.competitor_price,
        capped_at_max=change.capped_at_max,
        skipped_competitors=change.skipped_competitors,
        cities_aligned=change.cities_aligned,
        result=change.result,
        error=change.error,
    )
