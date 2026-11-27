"""Типы входа и выхода движка цен. Все цены и шаг — целые тенге."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Strategy(StrEnum):
    """Стратегия магазина."""

    BE_FIRST = "be_first"
    MATCH_FIRST = "match_first"
    HOLD_SECOND = "hold_second"


class Reason(StrEnum):
    """Причина решения; называет сработавшее правило product.md."""

    # Цена ставится.
    UNDERCUT_FIRST = "undercut_first"  # правило 2
    MATCHED_FIRST = "matched_first"  # правило 3
    HELD_SECOND = "held_second"  # правило 4
    DROPPED_TO_MIN = "dropped_to_min"  # правила 4 и 5: цель ниже минимальной, ставится минимальная
    NO_COMPETITORS_RAISED_TO_MAX = "no_competitors_raised_to_max"  # правило 1
    # Цена не меняется (NO_CHANGE_REASONS).
    OWN_OFFER_MISSING = "own_offer_missing"  # защита: список недостоверен
    OUT_OF_BOUNDS = "out_of_bounds"  # правило 9: текущая цена вне границ
    # Список продавцов неполный (ADR 0016), прочитанных конкурентов нет: правило 1 не применяется.
    COMPETITORS_UNSEEN = "competitors_unseen"
    NO_COMPETITORS_RAISE_OFF = "no_competitors_raise_off"  # правило 1
    AT_MIN_PRICE = "at_min_price"  # правило 5: цель прижата к минимальной и равна текущей
    ALREADY_AT_TARGET = "already_at_target"  # правило 8
    RAISE_DISABLED = "raise_disabled"  # правило 7
    DECREASE_DISABLED = "decrease_disabled"  # правило 7
    # Границы цены не заданы (ADR 0030). Движок её не возвращает: решение собирает обработчик.
    BOUNDS_MISSING = "bounds_missing"


# Закрытый список причин исхода «не менять» (ADR 0020); у каждой причины new_price is None.
NO_CHANGE_REASONS: frozenset[Reason] = frozenset(
    {
        Reason.OWN_OFFER_MISSING,
        Reason.OUT_OF_BOUNDS,
        Reason.COMPETITORS_UNSEEN,
        Reason.NO_COMPETITORS_RAISE_OFF,
        Reason.AT_MIN_PRICE,
        Reason.ALREADY_AT_TARGET,
        Reason.RAISE_DISABLED,
        Reason.DECREASE_DISABLED,
        Reason.BOUNDS_MISSING,
    }
)


@dataclass(frozen=True, slots=True)
class Offer:
    """Предложение продавца в карточке товара."""

    merchant_id: str
    price: int


@dataclass(frozen=True, slots=True)
class PricingSettings:
    """Настройки расчёта. Повышение разрешено, только когда включены оба выключателя."""

    strategy: Strategy
    auto_decrease: bool
    auto_raise: bool
    shop_auto_raise: bool
    min_price: int
    max_price: int
    step: int
    own_merchant_id: str
    excluded_merchant_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Decision:
    """Решение движка.

    new_price is None означает «не менять»; тогда reason входит в NO_CHANGE_REASONS.
    Если цена задана, она лежит в [min_price, max_price] и не равна текущей: бот никогда
    не ставит цену вне границ. Текущую цену вне границ бот не меняет (OUT_OF_BOUNDS).

    target_price — цель правил 1-8 (в границах); None у OWN_OFFER_MISSING, OUT_OF_BOUNDS,
    COMPETITORS_UNSEEN, NO_COMPETITORS_RAISE_OFF и BOUNDS_MISSING. competitor — самый
    дешёвый конкурент, если решение опирается на конкурентов.
    """

    new_price: int | None
    reason: Reason
    target_price: int | None = None
    competitor: Offer | None = None
    capped_at_max: bool = False
