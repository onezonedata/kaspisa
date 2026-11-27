"""Примеры на очищенном ответе Kaspi docs/fixtures/kaspi/offers.json."""

from __future__ import annotations

import json
from pathlib import Path

from kaspisa.pricing import (
    Offer,
    PricingSettings,
    Reason,
    Strategy,
    decide_price,
)

FIXTURE = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi" / "offers.json"
OWN = "90100001"  # Магазин 3


def load_offers() -> list[Offer]:
    """Разбирает ответ Kaspi в предложения (порядок Kaspi сохраняется)."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [Offer(str(o["merchantId"]), int(o["price"])) for o in data["offers"]]


def make(**kw: object) -> PricingSettings:
    base: dict[str, object] = {
        "strategy": Strategy.BE_FIRST,
        "auto_decrease": True,
        "auto_raise": True,
        "shop_auto_raise": True,
        "min_price": 2000,
        "max_price": 5000,
        "step": 1,
        "own_merchant_id": OWN,
    }
    base.update(kw)
    return PricingSettings(**base)  # type: ignore[arg-type]


def test_f1_be_first_first_of_equals() -> None:
    d = decide_price(2998, load_offers(), make())
    assert d.new_price == 2642
    assert d.competitor is not None
    assert d.competitor.merchant_id == "90100003"


def test_f2_match_first() -> None:
    assert decide_price(2998, load_offers(), make(strategy=Strategy.MATCH_FIRST)).new_price == 2643


def test_f3_hold_second() -> None:
    # Двое по 2643 — цена как у них (ответ пользователя 2026-11-13); следующая цена 2998.
    d = decide_price(2998, load_offers(), make(strategy=Strategy.HOLD_SECOND))
    assert (d.new_price, d.reason) == (2643, Reason.HELD_SECOND)
    assert d.competitor is not None
    assert d.competitor.merchant_id == "90100003"


def test_f4_excluded_and_own_not_counted() -> None:
    s = make(excluded_merchant_ids=frozenset({"90100003", "90100005"}))
    d = decide_price(2998, load_offers(), s)
    assert d.new_price == 2997
    assert d.competitor == Offer("90100004", 2998)


def test_f5_be_first_target_below_min_drops_to_min() -> None:
    # Цель 2642 ниже минимальной 2700: ставится минимальная.
    d = decide_price(2998, load_offers(), make(min_price=2700))
    assert (d.new_price, d.reason) == (2700, Reason.DROPPED_TO_MIN)
    assert d.competitor == Offer("90100003", 2643)


def test_f7_hold_second_leaders_below_min_sets_min() -> None:
    # Двое по 2643 ниже минимальной 2700: ставится минимальная (решение 4).
    d = decide_price(2998, load_offers(), make(strategy=Strategy.HOLD_SECOND, min_price=2700))
    assert (d.new_price, d.reason) == (2700, Reason.DROPPED_TO_MIN)


def test_f8_hold_second_leaders_below_min_equal_to_next_price() -> None:
    # Двое по 2643 ниже минимальной 2998, текущая 3200 в границах: ставится минимальная.
    s = make(strategy=Strategy.HOLD_SECOND, min_price=2998)
    d = decide_price(3200, load_offers(), s)
    assert (d.new_price, d.reason) == (2998, Reason.DROPPED_TO_MIN)
