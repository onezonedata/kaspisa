"""Т41: числа наблюдений товара (страницы карточки - ADR 0016)."""

from __future__ import annotations

import json
from pathlib import Path

from kaspisa.kaspi.models import Offer, OffersPage
from kaspisa.pricing import PricingSettings, Reason, Strategy, decide_price
from kaspisa.worker.observation import more_offers_unseen, observe, to_pricing_offers

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi"


def _offer(merchant: str, price: int, preorder: int = 0) -> Offer:
    return Offer.model_validate(
        {"merchantId": merchant, "merchantName": merchant, "price": price, "preorder": preorder}
    )


def _page(offers: list[Offer], total: int | None = None) -> OffersPage:
    return OffersPage(offers=offers, total=len(offers) if total is None else total)


def test_position_competitors_first_place() -> None:
    offers = [
        _offer("excluded", 80),
        _offer("a", 90),
        _offer("own", 100),
        _offer("b", 100),
        _offer("c", 120),
    ]
    result = observe(_page(offers, 30), own_merchant_id="own", own_price=100)
    assert result.position == 3  # строго дешевле: 90 и 80
    assert result.position_over is None
    assert result.competitors_count == 29  # total без своего магазина
    assert result.first_place_price == 80


def test_alone_in_card() -> None:
    result = observe(_page([_offer("own", 100)]), own_merchant_id="own", own_price=100)
    assert (result.position, result.position_over) == (1, None)
    assert (result.competitors_count, result.first_place_price) == (0, 100)


def test_own_absent_beyond_read_pages_is_over() -> None:
    """Своего магазина нет на двух страницах, карточка длиннее - позиция «20+»."""
    offers = [_offer(f"m{i}", 100 + i) for i in range(20)]
    result = observe(_page(offers, 57), own_merchant_id="own", own_price=500)
    assert (result.position, result.position_over) == (None, 20)
    assert result.competitors_count == 57  # своего магазина в выдаче не видно
    assert result.first_place_price == 100


def test_own_absent_from_whole_card_has_no_position() -> None:
    """Карточка прочитана целиком, своего магазина нет - позиции нет (не «N+»)."""
    result = observe(_page([_offer("a", 120)]), own_merchant_id="own", own_price=100)
    assert (result.position, result.position_over) == (None, None)
    assert (result.competitors_count, result.first_place_price) == (1, 100)


def test_more_offers_unseen() -> None:
    assert more_offers_unseen(_page([_offer("a", 1)], 2))
    assert not more_offers_unseen(_page([_offer("a", 1)], 1))
    assert not more_offers_unseen(_page([], 0))


def test_server_fixture() -> None:
    """Снятый с сервера ответ: свой магазин Magazin5 (2 999) - третий из четырёх."""
    raw = json.loads((FIXTURES / "offers-server-2026-11-15.json").read_text())
    page = OffersPage.model_validate(raw)
    result = observe(page, own_merchant_id="Magazin5", own_price=2999)
    assert (result.position, result.position_over) == (3, None)
    assert (result.competitors_count, result.first_place_price) == (3, 2998)
    assert not more_offers_unseen(page)


def test_pricing_offers_keep_all_sellers() -> None:
    converted = to_pricing_offers([_offer("own", 100), _offer("a", 90)])
    assert [(o.merchant_id, o.price) for o in converted] == [("own", 100), ("a", 90)]


def test_preorder_seller_is_a_competitor() -> None:
    """Продавец с предзаказом - обычный конкурент (решение пользователя 2026-11-17)."""
    offers = [_offer("pre", 900, preorder=7), _offer("own", 1000)]
    settings = PricingSettings(
        strategy=Strategy.BE_FIRST,
        auto_decrease=True,
        auto_raise=True,
        shop_auto_raise=True,
        min_price=500,
        max_price=2000,
        step=1,
        own_merchant_id="own",
    )
    decision = decide_price(1000, to_pricing_offers(offers), settings)
    assert (decision.new_price, decision.reason) == (899, Reason.UNDERCUT_FIRST)
    seen = observe(_page(offers), own_merchant_id="own", own_price=1000)
    assert (seen.position, seen.competitors_count, seen.first_place_price) == (2, 1, 900)


def test_empty_page_with_total_has_no_position() -> None:
    result = observe(_page([], 30), own_merchant_id="own", own_price=100)
    assert (result.position, result.position_over, result.competitors_count) == (None, None, 30)


def test_cheapest_competitor_skips_own_and_excluded() -> None:
    """ADR 0030: самый дешёвый конкурент - без своего магазина и исключённых, первый по порядку."""
    from kaspisa.worker.observation import cheapest_competitor

    offers = [
        _offer("own", 50),
        _offer("excluded", 60),
        _offer("a", 90),
        _offer("b", 90),
        _offer("c", 120),
    ]
    found = cheapest_competitor(offers, "own", frozenset({"excluded"}))
    assert found is not None
    assert (found.merchant_id, found.price) == ("a", 90)
    assert cheapest_competitor([_offer("own", 50)], "own", frozenset()) is None
    assert cheapest_competitor([_offer("x", 5)], "own", frozenset({"x"})) is None
    assert cheapest_competitor([], "own", frozenset()) is None
