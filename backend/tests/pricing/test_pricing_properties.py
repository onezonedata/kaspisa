"""Свойства движка цен (hypothesis)."""

from __future__ import annotations

import random
from dataclasses import replace

from hypothesis import example, given
from hypothesis import strategies as st

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
IDS = ["me", "A", "B", "C", "X"]
EXCLUDED = frozenset({"X"})


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[int, list[Offer], PricingSettings]:
    """Цены генерируются относительно границ, чтобы попадать в опасные ветки.

    Текущая цена генерируется и вне границ."""
    lo = draw(st.integers(1, 2000))
    hi = draw(st.integers(lo, lo + 2000))
    step = draw(st.integers(1, 50))
    floor = max(1, lo - 3 * step)
    special = [lo, hi, lo - step, lo + step, hi - step, hi + step]
    price = st.one_of(
        st.integers(floor, hi + 3 * step),
        st.sampled_from(special).map(lambda p: max(1, p)),
    )
    prices: list[int] = []
    offers: list[Offer] = []
    for _ in range(draw(st.integers(0, 8))):
        if len(prices) > 0 and draw(st.booleans()):
            p = max(1, draw(st.sampled_from(prices)) + draw(st.sampled_from([0, 0, 1])))
        else:
            p = draw(price)
        prices.append(p)
        offers.append(Offer(draw(st.sampled_from(IDS)), p))
    if draw(st.integers(0, 9)) < 8 and not any(o.merchant_id == ME for o in offers):
        offers.append(Offer(ME, draw(price)))
    current = draw(price)
    settings = PricingSettings(
        strategy=draw(st.sampled_from(Strategy)),
        auto_decrease=draw(st.booleans()),
        auto_raise=draw(st.booleans()),
        shop_auto_raise=draw(st.booleans()),
        min_price=lo,
        max_price=hi,
        step=step,
        own_merchant_id=ME,
        excluded_merchant_ids=EXCLUDED,
    )
    return current, offers, settings


Scenario = tuple[int, list[Offer], PricingSettings]


def _s(strategy: Strategy, lo: int, hi: int, step: int) -> PricingSettings:
    return PricingSettings(strategy, True, True, True, lo, hi, step, ME, EXCLUDED)


def _own_present(offers: list[Offer]) -> bool:
    return any(o.merchant_id == ME for o in offers)


@given(scenarios())
@example(
    (
        1500,
        [Offer("A", 1500), Offer("B", 1500), Offer("C", 1800), Offer(ME, 2000)],
        _s(Strategy.HOLD_SECOND, 1000, 2000, 10),
    )
)
@example(
    (
        1500,
        [Offer("A", 500), Offer("B", 5000), Offer(ME, 1500)],
        _s(Strategy.BE_FIRST, 1000, 2000, 10),
    )
)
@example((1500, [Offer("A", 1000), Offer(ME, 1500)], _s(Strategy.BE_FIRST, 1000, 2000, 1)))
@example(
    (
        1500,
        [Offer("A", 500), Offer("B", 900), Offer(ME, 1500)],
        _s(Strategy.HOLD_SECOND, 1000, 5000, 10),
    )
)
@example(
    (
        1500,
        [Offer("A", 1000), Offer("B", 1001), Offer(ME, 1500)],
        _s(Strategy.HOLD_SECOND, 1000, 5000, 10),
    )
)
@example((500, [Offer("A", 1500), Offer(ME, 500)], _s(Strategy.BE_FIRST, 1000, 2000, 1)))
@example((3000, [Offer("A", 1500), Offer(ME, 3000)], _s(Strategy.BE_FIRST, 1000, 2000, 1)))
@example((1500, [Offer(ME, 1500)], _s(Strategy.BE_FIRST, 1000, 2000, 1)))
def test_t28_set_price_and_target_within_bounds(sc: Scenario) -> None:
    """Бот никогда не ставит цену ниже минимальной и выше максимальной."""
    current, offers, s = sc
    d = decide_price(current, offers, s)
    if d.new_price is not None:
        assert s.min_price <= d.new_price <= s.max_price
        assert d.new_price != current
    if d.target_price is not None:
        assert s.min_price <= d.target_price <= s.max_price


@given(scenarios())
def test_engine_never_returns_bounds_missing(sc: Scenario) -> None:
    """Причину «границы не заданы» собирает обработчик; движок её не возвращает (ADR 0030)."""
    current, offers, s = sc
    assert decide_price(current, offers, s).reason != Reason.BOUNDS_MISSING


@given(scenarios())
def test_t29_reason_consistent_with_outcome(sc: Scenario) -> None:
    current, offers, s = sc
    d = decide_price(current, offers, s)
    own = _own_present(offers)
    in_bounds = s.min_price <= current <= s.max_price
    assert (d.new_price is None) == (d.reason in NO_CHANGE_REASONS)
    assert (d.reason is Reason.OWN_OFFER_MISSING) == (not own)
    assert (d.reason is Reason.OUT_OF_BOUNDS) == (own and not in_bounds)
    if d.reason in (Reason.OWN_OFFER_MISSING, Reason.OUT_OF_BOUNDS):
        assert d == Decision(None, d.reason)


@given(scenarios())
def test_t30_no_decrease_when_disabled(sc: Scenario) -> None:
    current, offers, s = sc
    d = decide_price(current, offers, replace(s, auto_decrease=False))
    assert d.new_price is None or d.new_price > current


@given(scenarios(), st.sampled_from(["product", "shop"]))
def test_t31_no_raise_when_disabled(sc: Scenario, which: str) -> None:
    current, offers, s = sc
    s = replace(s, auto_raise=False) if which == "product" else replace(s, shop_auto_raise=False)
    d = decide_price(current, offers, s)
    assert d.new_price is None or d.new_price < current


@given(scenarios(), st.sampled_from(["product", "shop"]))
def test_t32_both_directions_off_never_changes(sc: Scenario, which: str) -> None:
    current, offers, s = sc
    s = replace(s, auto_decrease=False)
    s = replace(s, auto_raise=False) if which == "product" else replace(s, shop_auto_raise=False)
    assert decide_price(current, offers, s).new_price is None


@given(scenarios(), st.randoms(use_true_random=False))
def test_t33_order_does_not_matter(sc: Scenario, rnd: random.Random) -> None:
    current, offers, s = sc
    shuffled = list(offers)
    rnd.shuffle(shuffled)
    a = decide_price(current, offers, s)
    b = decide_price(current, shuffled, s)
    assert (a.new_price, a.reason, a.target_price) == (b.new_price, b.reason, b.target_price)


@given(
    scenarios(),
    st.lists(
        st.tuples(st.sampled_from([ME, "X"]), st.integers(1, 20000)),
        max_size=4,
    ),
)
def test_t34_extra_own_and_excluded_offers_change_nothing(
    sc: Scenario, extra: list[tuple[str, int]]
) -> None:
    current, offers, s = sc
    if not _own_present(offers):
        offers = [*offers, Offer(ME, current)]
    more = [*offers, *(Offer(m, p) for m, p in extra)]
    assert decide_price(current, offers, s) == decide_price(current, more, s)


@given(scenarios())
def test_t35_stable_after_apply(sc: Scenario) -> None:
    current, offers, s = sc
    d = decide_price(current, offers, s)
    if d.new_price is not None:
        again = decide_price(d.new_price, offers, s)
        assert again.new_price is None
        assert again.reason in (Reason.ALREADY_AT_TARGET, Reason.AT_MIN_PRICE)


@given(scenarios())
def test_t36_own_offer_missing(sc: Scenario) -> None:
    """Своего магазина нет: «не менять» целиком, при любой текущей цене и настройках."""
    current, offers, s = sc
    others = [o for o in offers if o.merchant_id != ME]
    assert decide_price(current, others, s) == Decision(None, Reason.OWN_OFFER_MISSING)


@given(scenarios())
def test_t37_unseen_flag_irrelevant_with_competitors_or_out_of_bounds(sc: Scenario) -> None:
    current, offers, s = sc
    a = decide_price(current, offers, s, more_offers_unseen=True)
    b = decide_price(current, offers, s)
    in_bounds = s.min_price <= current <= s.max_price
    competitors = [o for o in offers if o.merchant_id != ME and o.merchant_id not in EXCLUDED]
    if _own_present(offers) and not in_bounds:
        assert a == b == Decision(None, Reason.OUT_OF_BOUNDS)
    if competitors:
        assert a == b


@given(scenarios(), st.booleans())
def test_t38_unseen_offers_keep_bounds(sc: Scenario, unseen: bool) -> None:
    """Неполный список (ADR 0016): повышения «до максимальной без конкурентов» нет,
    цена бота в границах; при наличии конкурентов или без флага решение прежнее."""
    current, offers, s = sc
    d = decide_price(current, offers, s, more_offers_unseen=unseen)
    if d.new_price is not None:
        assert s.min_price <= d.new_price <= s.max_price
    if unseen:
        assert d.reason is not Reason.NO_COMPETITORS_RAISED_TO_MAX
        assert d.reason is not Reason.NO_COMPETITORS_RAISE_OFF
    competitors = [o for o in offers if o.merchant_id != ME and o.merchant_id not in EXCLUDED]
    if competitors or not unseen:
        assert d == decide_price(current, offers, s)
