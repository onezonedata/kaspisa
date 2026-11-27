"""`get_offers`: запрос, страницы (10 продавцов, не больше двух), ошибки (Т19-Т23)."""

# ruff: noqa: F401, F811  (фикстуры импортируются из kaspi_fixtures; conftest.py здесь нельзя)

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx
import pytest
import respx
from kaspi_fixtures import (
    client,
    fake_time,
    limiter,
    mock_kaspi,
)
from kaspi_testing import FakeTime, RecordingLimiter, load_fixture, synthetic_offers_page

from kaspisa.kaspi import KaspiClient, ResponseFormatError, UnexpectedStatusError

URL = "https://kaspi.kz/yml/offer-view/offers/900000005"


OWN = "90100003"


def _seller(merchant: str, price: int, preorder: int = 0) -> dict[str, Any]:
    """Синтетика, не снято: продавец карточки с нужными полями."""
    return {"merchantId": merchant, "merchantName": merchant, "price": price, "preorder": preorder}


def _sellers(prefix: str, count: int, start: int) -> list[dict[str, Any]]:
    return [_seller(f"{prefix}{i}", start + i) for i in range(count)]


async def test_request_shape(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т19: метод, адрес, тело (страница 10 продавцов), `Referer`; cookie не отправляются."""
    route = mock_kaspi.post(URL).mock(
        return_value=httpx.Response(
            200,
            json=load_fixture("offers.json"),
            headers={"Set-Cookie": "ks.tg=abc-marker; Domain=kaspi.kz; Path=/"},
        )
    )
    await client.get_offers("900000005", "710000000", OWN)
    request = route.calls.last.request
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "cityId": "710000000",
        "id": "900000005",
        "limit": 10,
        "page": 0,
        "sortOption": "PRICE",
    }
    assert request.headers["referer"] == "https://kaspi.kz/shop/p/-900000005/?c=710000000"
    assert "cookie" not in request.headers
    # Cookie из ответа не попадают в следующий запрос.
    await client.get_offers("900000005", "710000000", OWN)
    assert "cookie" not in route.calls.last.request.headers


async def test_own_on_first_page_reads_one_page(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т20: свой магазин на первой странице - один запрос, `total` из ответа, порядок сохранён."""
    page = [*_sellers("a", 9, 100), _seller(OWN, 200)]
    route = mock_kaspi.post(URL).mock(
        return_value=httpx.Response(200, json=synthetic_offers_page(page, 57))
    )
    result = await client.get_offers("900000005", "710000000", OWN)
    assert route.call_count == 1
    assert result.total == 57
    assert [o.merchant_id for o in result.offers] == [o["merchantId"] for o in page]


async def test_own_on_second_page_reads_two_pages(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т20а: своего магазина нет на первой странице - читается вторая, и только она."""
    first = _sellers("a", 10, 100)
    second = [*_sellers("b", 3, 200), _seller(OWN, 300), *_sellers("c", 6, 400)]
    route = mock_kaspi.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=synthetic_offers_page(first, 57)),
            httpx.Response(200, json=synthetic_offers_page(second, 57)),
        ]
    )
    result = await client.get_offers("900000005", "710000000", OWN)
    assert [json.loads(c.request.content)["page"] for c in route.calls] == [0, 1]
    assert [json.loads(c.request.content)["limit"] for c in route.calls] == [10, 10]
    assert [o.merchant_id for o in result.offers] == [o["merchantId"] for o in first + second]
    assert result.total == 57


async def test_own_absent_reads_at_most_two_pages(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т21: своего магазина нет на двух страницах - третья страница не читается."""
    route = mock_kaspi.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=synthetic_offers_page(_sellers("a", 10, 100), 57)),
            httpx.Response(200, json=synthetic_offers_page(_sellers("b", 10, 200), 57)),
            httpx.Response(200, json=synthetic_offers_page(_sellers("c", 10, 300), 57)),
        ]
    )
    result = await client.get_offers("900000005", "710000000", OWN)
    assert route.call_count == 2
    assert len(result.offers) == 20
    assert all(o.merchant_id != OWN for o in result.offers)
    assert result.total == 57


@pytest.mark.parametrize("total", [0, 4, 10])
async def test_whole_card_on_first_page_reads_one_page(
    client: KaspiClient, mock_kaspi: respx.MockRouter, total: int
) -> None:
    """Т21а: вся карточка уместилась на первой странице (или пуста) - второй запрос не нужен,
    даже если своего магазина в ней нет."""
    route = mock_kaspi.post(URL).mock(
        return_value=httpx.Response(
            200, json=synthetic_offers_page(_sellers("a", total, 100), total)
        )
    )
    result = await client.get_offers("900000005", "710000000", OWN)
    assert route.call_count == 1
    assert len(result.offers) == total


async def test_empty_first_page_with_positive_total_stops(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т21б: пустая первая страница при `total` > 0 - обход завершается без второго запроса."""
    route = mock_kaspi.post(URL).mock(
        return_value=httpx.Response(200, json=synthetic_offers_page([], 30))
    )
    result = await client.get_offers("900000005", "710000000", OWN)
    assert route.call_count == 1
    assert (result.offers, result.total) == ([], 30)


async def test_preorder_sellers_returned(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Продавцы с предзаказом возвращаются как обычные (решение пользователя 2026-11-17)."""
    page = [_seller("pre", 90, preorder=5), _seller(OWN, 100)]
    mock_kaspi.post(URL).mock(return_value=httpx.Response(200, json=synthetic_offers_page(page, 2)))
    result = await client.get_offers("900000005", "710000000", OWN)
    assert [(o.merchant_id, o.preorder) for o in result.offers] == [("pre", 5), (OWN, 0)]


async def test_403_is_unexpected_status_and_bad_sku(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т22: 403 - не истёкшая сессия; недопустимый `master_sku` - `ValueError` без запроса."""
    route = mock_kaspi.post(URL).mock(return_value=httpx.Response(403))
    with pytest.raises(UnexpectedStatusError) as info:
        await client.get_offers("900000005", "710000000", "90100003")
    assert info.value.status_code == 403
    calls = route.call_count
    with pytest.raises(ValueError, match="master_sku"):
        await client.get_offers("1/../x", "710000000", "90100003")
    with pytest.raises(ValueError, match="city_id"):
        await client.get_offers("900000005", "5910?10000", "90100003")
    assert route.call_count == calls


async def test_calls_and_pages_separated_by_one_second(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    limiter: RecordingLimiter,
    fake_time: FakeTime,
) -> None:
    """Т23: два вызова подряд и страницы одного вызова разделены паузой 1 с."""
    offers: list[dict[str, Any]] = load_fixture("offers.json")["offers"]
    mock_kaspi.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=synthetic_offers_page(offers[:4], 7)),
            httpx.Response(200, json=synthetic_offers_page(offers[4:], 7)),
            httpx.Response(200, json=load_fixture("offers.json")),
        ]
    )
    # Своего магазина ("Magazin5", пятый) на первой странице нет - читается вторая.
    await client.get_offers("900000005", "710000000", "Magazin5", page_size=4)
    await client.get_offers("900000005", "710000000", OWN)
    moments = [t for key, t in limiter.passed if key == "kaspi.kz"]
    assert len(moments) == 3
    assert all(b - a >= 1.0 for a, b in itertools.pairwise(moments))


async def test_server_response_float_prices(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Ответ, снятый с сервера (цены `2998.0`), разбирается: цены - целые `int`."""
    mock_kaspi.post(URL).mock(
        return_value=httpx.Response(200, json=load_fixture("offers-server-2026-11-15.json"))
    )
    result = await client.get_offers("900000005", "710000000", "90100003")
    assert [o.price for o in result.offers] == [2998, 2998, 2999, 4990]
    assert all(type(o.price) is int for o in result.offers)
    ids = [o.merchant_id for o in result.offers]
    assert ids == ["90100003", "90100006", "Magazin5", "90100002"]
    assert all(type(o.merchant_id) is str for o in result.offers)


def _page_with_price_text(price_json: str) -> bytes:
    """Тело ответа с одним продавцом; цена подставляется как сырой текст JSON."""
    return (
        '{"total":1,"offers":[{"merchantId":"1","merchantName":"M","price":'
        + price_json
        + ',"preorder":0}]}'
    ).encode()


@pytest.mark.parametrize(
    ("price_json", "expected"), [("2998", 2998), ("2998.0", 2998), ("1.0", 1), ("2.998e3", 2998)]
)
async def test_offer_price_accepted(
    client: KaspiClient, mock_kaspi: respx.MockRouter, price_json: str, expected: int
) -> None:
    """Целая цена и float с целым значением принимаются и дают `int`."""
    mock_kaspi.post(URL).mock(
        return_value=httpx.Response(
            200,
            content=_page_with_price_text(price_json),
            headers={"content-type": "application/json"},
        )
    )
    result = await client.get_offers("900000005", "710000000", "90100003")
    assert result.offers[0].price == expected
    assert type(result.offers[0].price) is int


@pytest.mark.parametrize(
    "price_json",
    ["2998.5", "NaN", "Infinity", "-Infinity", "true", '"2998"', "0", "0.0", "-5", "-5.0", "null"],
)
async def test_offer_price_rejected(
    client: KaspiClient, mock_kaspi: respx.MockRouter, price_json: str
) -> None:
    """Дробная цена, NaN, бесконечность, bool, строка, 0 и отрицательные - `ResponseFormatError`."""
    mock_kaspi.post(URL).mock(
        return_value=httpx.Response(
            200,
            content=_page_with_price_text(price_json),
            headers={"content-type": "application/json"},
        )
    )
    with pytest.raises(ResponseFormatError):
        await client.get_offers("900000005", "710000000", "90100003")
