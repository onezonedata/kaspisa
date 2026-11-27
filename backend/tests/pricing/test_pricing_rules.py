"""Примеры по правилам расчёта цены из требований к продукту (ADR 0020).

Общие настройки: min 600, max 800, шаг 1, автоснижение и автоповышение включены,
свой магазин `me` в предложениях с текущей ценой, исключений нет.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

import kaspisa.pricing as pricing
from kaspisa.pricing import (
    NO_CHANGE_REASONS,
    Decision,
    Offer,
    PricingSettings,
    Reason,
    Strategy,
    decide_price,
)

ME = "me"


def settings(**kw: object) -> PricingSettings:
    base: dict[str, object] = {
        "strategy": Strategy.BE_FIRST,
        "auto_decrease": True,
        "auto_raise": True,
        "shop_auto_raise": True,
        "min_price": 600,
        "max_price": 800,
        "step": 1,
        "own_merchant_id": ME,
        "excluded_merchant_ids": frozenset[str](),
    }
    base.update(kw)
    return PricingSettings(**base)  # type: ignore[arg-type]


def run(
    current: int,
    prices: list[int],
    *,
    own: bool = True,
    unseen: bool = False,
    **kw: Any,
) -> Decision:
    """Конкуренты A, B, C… по ценам prices плюс свой магазин с текущей ценой."""
    offers = [Offer("ABCDEFGH"[i], p) for i, p in enumerate(prices)]
    if own:
        offers.append(Offer(ME, current))
    return decide_price(current, offers, settings(**kw), more_offers_unseen=unseen)


def test_t1_be_first_undercuts() -> None:
    assert run(750, [700]) == Decision(
        699, Reason.UNDERCUT_FIRST, target_price=699, competitor=Offer("A", 700)
    )


def test_t2_be_first_target_below_min_drops_to_min() -> None:
    assert run(605, [599]) == Decision(
        600, Reason.DROPPED_TO_MIN, target_price=600, competitor=Offer("A", 599)
    )


def test_t3_be_first_raw_one_below_min_drops_to_min() -> None:
    d = run(605, [600])
    assert (d.new_price, d.reason) == (600, Reason.DROPPED_TO_MIN)


def test_t4_at_min_price_when_current_is_min() -> None:
    assert run(600, [599]) == Decision(
        None, Reason.AT_MIN_PRICE, target_price=600, competitor=Offer("A", 599)
    )


def test_t5_match_first() -> None:
    d = run(750, [700], strategy=Strategy.MATCH_FIRST)
    assert (d.new_price, d.reason) == (700, Reason.MATCHED_FIRST)


def test_t6_match_first_below_min_drops_to_min() -> None:
    d = run(650, [550], strategy=Strategy.MATCH_FIRST)
    assert (d.new_price, d.reason, d.target_price) == (600, Reason.DROPPED_TO_MIN, 600)


def test_t7_hold_second_step_above_leader() -> None:
    d = run(750, [700, 760], strategy=Strategy.HOLD_SECOND, step=5)
    assert (d.new_price, d.reason, d.competitor) == (705, Reason.HELD_SECOND, Offer("A", 700))


def test_t8_hold_second_twin_leaders_match_price() -> None:
    d = run(750, [700, 700, 760], strategy=Strategy.HOLD_SECOND, step=5)
    assert (d.new_price, d.reason) == (700, Reason.HELD_SECOND)


def test_t9_hold_second_stays_below_next_price() -> None:
    d = run(750, [700, 703], strategy=Strategy.HOLD_SECOND, step=5)
    assert d.new_price == 702


def test_t10_hold_second_raw_below_min_but_leader_not_below() -> None:
    # c0=590 < min: прижатие к min по букве решения 4.
    d = run(640, [590, 650], strategy=Strategy.HOLD_SECOND, step=5)
    assert (d.new_price, d.reason) == (600, Reason.DROPPED_TO_MIN)


def test_t11_hold_second_leader_below_min_sets_min() -> None:
    d = run(640, [598, 650], strategy=Strategy.HOLD_SECOND, step=5)
    assert d == Decision(600, Reason.DROPPED_TO_MIN, target_price=600, competitor=Offer("A", 598))


def test_t12_hold_second_twins_below_min() -> None:
    d = run(640, [590, 590], strategy=Strategy.HOLD_SECOND, step=5)
    assert (d.new_price, d.reason) == (600, Reason.DROPPED_TO_MIN)


def test_t13_match_first_capped_at_max() -> None:
    d = run(700, [900], strategy=Strategy.MATCH_FIRST)
    assert (d.new_price, d.reason, d.capped_at_max) == (800, Reason.MATCHED_FIRST, True)
    assert d.target_price == 800


def test_t14_no_competitors_raised_to_max() -> None:
    assert run(700, []) == Decision(
        800, Reason.NO_COMPETITORS_RAISED_TO_MAX, target_price=800, competitor=None
    )


def test_t15_decrease_disabled() -> None:
    d = run(750, [700], auto_decrease=False)
    assert (d.new_price, d.reason, d.target_price) == (None, Reason.DECREASE_DISABLED, 699)


def test_t16_decrease_disabled_target_floored_to_min() -> None:
    d = run(650, [599], auto_decrease=False)
    assert (d.new_price, d.reason, d.target_price) == (None, Reason.DECREASE_DISABLED, 600)


def test_t17_raise_disabled_product() -> None:
    d = run(650, [700], strategy=Strategy.MATCH_FIRST, auto_raise=False)
    assert (d.new_price, d.reason, d.target_price) == (None, Reason.RAISE_DISABLED, 700)


def test_t18_raise_disabled_shop() -> None:
    d = run(650, [700], strategy=Strategy.MATCH_FIRST, shop_auto_raise=False)
    assert (d.new_price, d.reason, d.target_price) == (None, Reason.RAISE_DISABLED, 700)


def test_t19_current_below_min_is_out_of_bounds() -> None:
    assert run(590, [700], strategy=Strategy.MATCH_FIRST) == Decision(None, Reason.OUT_OF_BOUNDS)


def test_t20_current_above_max_is_out_of_bounds() -> None:
    assert run(850, [700], strategy=Strategy.MATCH_FIRST) == Decision(None, Reason.OUT_OF_BOUNDS)


def test_t21_out_of_bounds_beats_competitors_unseen() -> None:
    offers = [Offer(ME, 590), Offer("X", 700)]
    s = settings(excluded_merchant_ids=frozenset({"X"}))
    d = decide_price(590, offers, s, more_offers_unseen=True)
    assert d == Decision(None, Reason.OUT_OF_BOUNDS)


def test_t22_own_offer_missing_beats_out_of_bounds() -> None:
    d = run(590, [700], own=False)
    assert d == Decision(None, Reason.OWN_OFFER_MISSING)


def test_t23_competitors_unseen() -> None:
    offers = [Offer(ME, 700), Offer("X", 650)]
    s = settings(excluded_merchant_ids=frozenset({"X"}))
    d = decide_price(700, offers, s, more_offers_unseen=True)
    assert d == Decision(None, Reason.COMPETITORS_UNSEEN)


def test_t24_no_competitors_raise_off() -> None:
    assert run(700, [], auto_raise=False) == Decision(None, Reason.NO_COMPETITORS_RAISE_OFF)


def test_t25_already_at_target() -> None:
    assert run(700, [700], strategy=Strategy.MATCH_FIRST) == Decision(
        None, Reason.ALREADY_AT_TARGET, target_price=700, competitor=Offer("A", 700)
    )


def test_t26_closed_lists_and_removed_fields() -> None:
    assert (
        frozenset(
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
        == NO_CHANGE_REASONS
    )
    assert len(NO_CHANGE_REASONS) == 9
    assert Reason.BOUNDS_MISSING.value == "bounds_missing"
    values = {r.value for r in Reason}
    assert not values & {
        "returned_to_min",
        "returned_to_max",
        "below_min_do_nothing",
        "all_competitors_below_min",
    }
    assert len(values) == 14
    assert not hasattr(pricing, "BelowMinBehavior")
    assert "below_min" not in {f.name for f in dataclasses.fields(PricingSettings)}
    decision_fields = {f.name for f in dataclasses.fields(Decision)}
    assert not decision_fields & {"blocked_by", "skipped_competitors"}


def test_t27_invalid_input_raises() -> None:
    with pytest.raises(ValueError):
        run(700, [], min_price=0)
    with pytest.raises(ValueError):
        run(700, [], min_price=900, max_price=800)
    with pytest.raises(ValueError):
        run(700, [], step=0)
    with pytest.raises(ValueError):
        decide_price(0, [Offer(ME, 700)], settings())
    with pytest.raises(ValueError):
        decide_price(700, [Offer(ME, 700), Offer("A", 0)], settings())


def test_t64_hold_second_leader_at_min_is_not_floored() -> None:
    d = run(640, [600, 650], strategy=Strategy.HOLD_SECOND, step=5)
    assert (d.new_price, d.reason, d.target_price) == (605, Reason.HELD_SECOND, 605)


# --- Граничные случаи и защитные ветки ---


def test_current_at_bounds_is_in_bounds() -> None:
    assert run(600, [700], strategy=Strategy.MATCH_FIRST).reason is not Reason.OUT_OF_BOUNDS
    assert run(800, [700], strategy=Strategy.MATCH_FIRST).reason is not Reason.OUT_OF_BOUNDS


def test_be_first_target_exactly_min_is_not_floored() -> None:
    d = run(650, [601])
    assert (d.new_price, d.reason) == (600, Reason.UNDERCUT_FIRST)


def test_target_exactly_max_is_not_capped() -> None:
    d = run(700, [800], strategy=Strategy.MATCH_FIRST)
    assert (d.new_price, d.capped_at_max) == (800, False)


def test_hold_second_capped_at_max() -> None:
    d = run(700, [795, 900], strategy=Strategy.HOLD_SECOND, step=20)
    assert (d.new_price, d.reason, d.capped_at_max) == (800, Reason.HELD_SECOND, True)


def test_at_min_price_not_used_for_plain_equality() -> None:
    # Цель равна текущей без прижатия к min: already_at_target.
    d = run(650, [651])
    assert d.reason is Reason.ALREADY_AT_TARGET


def test_min_equals_max_current_at_bound() -> None:
    d = run(700, [650], min_price=700, max_price=700)
    assert (d.new_price, d.reason, d.target_price) == (None, Reason.AT_MIN_PRICE, 700)


def test_floored_decrease_disabled_keeps_decrease_reason() -> None:
    d = run(650, [550], strategy=Strategy.MATCH_FIRST, auto_decrease=False)
    assert (d.new_price, d.reason, d.target_price) == (None, Reason.DECREASE_DISABLED, 600)


def test_no_competitors_raise_disabled_has_no_target() -> None:
    d = run(700, [], shop_auto_raise=False)
    assert (d.target_price, d.competitor) == (None, None)


def test_excluded_competitor_is_ignored() -> None:
    d = run(750, [700, 710], excluded_merchant_ids=frozenset({"A"}))
    assert d.competitor == Offer("B", 710)
    assert d.new_price == 709


def test_competitor_first_of_equals_keeps_kaspi_order() -> None:
    offers = [Offer("Z", 700), Offer("A", 700), Offer(ME, 750)]
    d = decide_price(750, offers, settings())
    assert d.competitor == Offer("Z", 700)


def test_hold_second_single_competitor_is_one_step_above() -> None:
    d = run(750, [700], strategy=Strategy.HOLD_SECOND, step=5)
    assert (d.new_price, d.reason, d.target_price) == (705, Reason.HELD_SECOND, 705)


@pytest.mark.parametrize(("next_price", "expected"), [(701, 701), (702, 701), (704, 703)])
def test_hold_second_is_strictly_above_leader_when_gap_is_tiny(
    next_price: int, expected: int
) -> None:
    # Следующая цена вплотную к первой: цена не опускается до цены лидера.
    d = run(750, [700, next_price], strategy=Strategy.HOLD_SECOND, step=5)
    assert d.new_price == expected


def test_unseen_with_competitors_changes_nothing() -> None:
    assert run(750, [700], unseen=True) == run(750, [700])
