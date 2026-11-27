"""Наблюдения за товаром по предложениям продавцов и своя цена по строке списка: чистые функции."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from kaspisa.kaspi.models import Offer as KaspiOffer
from kaspisa.kaspi.models import OffersPage
from kaspisa.pricing import Offer


@dataclass(frozen=True)
class Observation:
    """Числа товара для интерфейса.

    position — место своего магазина, если он есть среди прочитанных продавцов.
    position_over — N, если своего магазина нет среди N прочитанных, а карточка длиннее
    (интерфейс показывает «N+»). Оба поля None, если карточка прочитана целиком и своего
    магазина в ней нет или если прочитанных продавцов нет вовсе (ADR 0016).
    """

    position: int | None
    position_over: int | None
    competitors_count: int
    first_place_price: int


def position_of(offers: Sequence[KaspiOffer], own_merchant_id: str, own_price: int) -> int:
    """1 + число чужих предложений строго дешевле своей цены (в том числе исключённых)."""
    return 1 + sum(1 for o in offers if o.merchant_id != own_merchant_id and o.price < own_price)


def first_place_of(offers: Sequence[KaspiOffer], own_merchant_id: str, own_price: int) -> int:
    """Цена первого места: минимум по всем прочитанным предложениям, включая свою цену."""
    return min([own_price, *(o.price for o in offers if o.merchant_id != own_merchant_id)])


def more_offers_unseen(page: OffersPage) -> bool:
    """В карточке есть продавцы за прочитанными страницами."""
    return page.total > len(page.offers)


def observe(
    page: OffersPage,
    *,
    own_merchant_id: str,
    own_price: int,
) -> Observation:
    """Позиция, «N+», число конкурентов из `total` (без своего магазина, если он в выдаче)
    и цена первого места.

    Исключённые продавцы учитываются в позиции и в числе конкурентов: `total` не говорит,
    сколько их на непрочитанных страницах (вопрос пользователю, ADR 0016).
    """
    offers = page.offers
    own_found = any(o.merchant_id == own_merchant_id for o in offers)
    position: int | None = None
    position_over: int | None = None
    if own_found:
        position = position_of(offers, own_merchant_id, own_price)
    elif offers and more_offers_unseen(page):
        position_over = len(offers)
    return Observation(
        position=position,
        position_over=position_over,
        competitors_count=max(0, page.total - (1 if own_found else 0)),
        first_place_price=first_place_of(offers, own_merchant_id, own_price),
    )


def cheapest_competitor(
    offers: Sequence[KaspiOffer], own_merchant_id: str, excluded: Collection[str]
) -> KaspiOffer | None:
    """Самый дешёвый конкурент: без своего магазина и исключённых, первый по порядку Kaspi.

    Определение конкурента то же, что у движка цен (ADR 0030).
    """
    candidates = [
        o for o in offers if o.merchant_id != own_merchant_id and o.merchant_id not in excluded
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda o: o.price)  # min берёт первый из равных


def to_pricing_offers(offers: Sequence[KaspiOffer]) -> list[Offer]:
    """Предложения Kaspi в виде входа движка цен: все продавцы, своя строка и продавцы
    с предзаказом в том числе (ADR 0016)."""
    return [Offer(merchant_id=o.merchant_id, price=o.price) for o in offers]


def own_price(city_prices: Mapping[str, int], shop_city_id: str) -> int | None:
    """Своя цена по городам строки списка (ADR 0015).

    Город сверки есть среди городов строки - его цена; иначе одна цена всех городов строки
    (город склада); иначе (цены разные или городов нет) - `None`, своя цена неизвестна.
    """
    if shop_city_id in city_prices:
        return city_prices[shop_city_id]
    values = set(city_prices.values())
    if len(values) == 1:
        return values.pop()
    return None
