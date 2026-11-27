"""Запрос продавцов публичной карточки (kaspi.kz, без cookie)."""

from __future__ import annotations

import re

from kaspisa.kaspi.models import OffersPage
from kaspisa.kaspi.transport import Transport, classify

OFFERS_PAGE_SIZE = 10
_ID_PATTERN = re.compile(r"[0-9A-Za-z_-]+")


async def get_offers(
    transport: Transport,
    master_sku: str,
    city_id: str,
    merchant_uid: str,
    page_size: int = OFFERS_PAGE_SIZE,
) -> OffersPage:
    """Продавцы карточки в исходном порядке (сортировка по цене) и `total` первой страницы.

    Читается первая страница; вторая - только если своего магазина (`merchant_uid`) на первой
    нет, а карточка на ней не кончилась. Дальше не читается (ADR 0016).
    """
    if not _ID_PATTERN.fullmatch(master_sku):
        raise ValueError("master_sku содержит недопустимые символы")
    if not _ID_PATTERN.fullmatch(city_id):
        raise ValueError("city_id содержит недопустимые символы")
    url = f"https://kaspi.kz/yml/offer-view/offers/{master_sku}"
    referer = f"https://kaspi.kz/shop/p/-{master_sku}/?c={city_id}"

    async def read(page: int) -> OffersPage:
        reply = await transport.send(
            "POST",
            url,
            operation="get_offers",
            retry=True,
            headers={"Referer": referer},
            json={
                "cityId": city_id,
                "id": master_sku,
                "limit": page_size,
                "page": page,
                "sortOption": "PRICE",
            },
        )
        return classify(reply, "get_offers", OffersPage)

    first = await read(0)
    own_found = any(o.merchant_id == merchant_uid for o in first.offers)
    if own_found or not first.offers or len(first.offers) >= first.total:
        return first
    second = await read(1)
    return OffersPage(offers=[*first.offers, *second.offers], total=first.total)
