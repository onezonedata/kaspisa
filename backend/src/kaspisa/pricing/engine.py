"""Движок цен: чистая функция выбора новой цены. Только стандартная библиотека."""

from __future__ import annotations

from collections.abc import Sequence

from kaspisa.pricing.types import (
    Decision,
    Offer,
    PricingSettings,
    Reason,
    Strategy,
)


def _validate(current_price: int, offers: Sequence[Offer], s: PricingSettings) -> None:
    """Неверный вход — ValueError."""
    if s.min_price < 1:
        raise ValueError("min_price должна быть не меньше 1")
    if s.max_price < s.min_price:
        raise ValueError("max_price не может быть меньше min_price")
    if s.step < 1:
        raise ValueError("step должен быть не меньше 1")
    if current_price < 1:
        raise ValueError("current_price должна быть не меньше 1")
    if any(o.price < 1 for o in offers):
        raise ValueError("цена предложения должна быть не меньше 1")


def _strategy_target(competitors: Sequence[Offer], s: PricingSettings) -> tuple[int, Reason]:
    """Цель стратегии без учёта границ; competitors отсортирован по цене и не пуст."""
    c0 = competitors[0].price
    if s.strategy is Strategy.BE_FIRST:
        return c0 - s.step, Reason.UNDERCUT_FIRST
    if s.strategy is Strategy.MATCH_FIRST:
        return c0, Reason.MATCHED_FIRST
    # HOLD_SECOND: несколько конкурентов с самой низкой ценой — цена как у них, шаг не применяется.
    if sum(1 for o in competitors if o.price == c0) >= 2:
        return c0, Reason.HELD_SECOND
    # Один лидер: строго выше c0, ниже следующей цены, если между ними есть место.
    higher = [o.price for o in competitors if o.price > c0]
    if not higher:
        return c0 + s.step, Reason.HELD_SECOND
    return max(c0 + 1, min(c0 + s.step, higher[0] - 1)), Reason.HELD_SECOND


def decide_price(
    current_price: int,
    offers: Sequence[Offer],
    settings: PricingSettings,
    *,
    more_offers_unseen: bool = False,
) -> Decision:
    """Возвращает новую цену или «не менять» с причиной из NO_CHANGE_REASONS.

    Порядок: свой магазин в списке (иначе OWN_OFFER_MISSING, ADR 0004); текущая цена
    в границах (иначе OUT_OF_BOUNDS, правило 9: бот цену вне границ не трогает);
    конкуренты и цель стратегии; прижатие цели к [min, max]; выключатели; равенство.

    more_offers_unseen: в карточке есть продавцы за прочитанными страницами (ADR 0016).
    Если тогда среди прочитанных нет ни одного конкурента, правило 1 не применяется:
    исход COMPETITORS_UNSEEN.
    """
    s = settings
    _validate(current_price, offers, s)

    if not any(o.merchant_id == s.own_merchant_id for o in offers):
        return Decision(None, Reason.OWN_OFFER_MISSING)
    if current_price < s.min_price or current_price > s.max_price:
        return Decision(None, Reason.OUT_OF_BOUNDS)

    # Устойчивая сортировка: при равных ценах остаётся порядок Kaspi.
    competitors = sorted(
        (
            o
            for o in offers
            if o.merchant_id != s.own_merchant_id and o.merchant_id not in s.excluded_merchant_ids
        ),
        key=lambda o: o.price,
    )
    can_raise = s.auto_raise and s.shop_auto_raise

    competitor: Offer | None = None
    floored = False
    capped = False

    if not competitors:
        if more_offers_unseen:
            return Decision(None, Reason.COMPETITORS_UNSEEN)
        if not can_raise:
            return Decision(None, Reason.NO_COMPETITORS_RAISE_OFF)
        target = s.max_price
        reason = Reason.NO_COMPETITORS_RAISED_TO_MAX
    else:
        competitor = competitors[0]
        raw, reason = _strategy_target(competitors, s)
        # «Держать второе»: самый дешёвый ниже минимальной — цель равна минимальной (решение 4).
        below_min = raw < s.min_price or (
            s.strategy is Strategy.HOLD_SECOND and competitor.price < s.min_price
        )
        if below_min:
            target = s.min_price
            reason = Reason.DROPPED_TO_MIN
            floored = True
        elif raw > s.max_price:
            target = s.max_price
            capped = True
        else:
            target = raw

    def no_change(why: Reason) -> Decision:
        return Decision(None, why, target, competitor, capped)

    if target == current_price:
        return no_change(Reason.AT_MIN_PRICE if floored else Reason.ALREADY_AT_TARGET)
    if target > current_price and not can_raise:
        return no_change(Reason.RAISE_DISABLED)
    if target < current_price and not s.auto_decrease:
        return no_change(Reason.DECREASE_DISABLED)
    return Decision(target, reason, target, competitor, capped)
