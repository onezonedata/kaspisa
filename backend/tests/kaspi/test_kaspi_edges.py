"""Граничные значения и ошибки клиента Kaspi, найденные мутациями кода (дополнение к Т12-Т67)."""

# ruff: noqa: F401, F811  (фикстуры импортируются из kaspi_fixtures; conftest.py здесь нельзя)

from __future__ import annotations

import copy
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
from kaspi_testing import (
    EDIT_PRICE_URL,
    LIST_URL,
    MERCHANT_UID,
    MFA_SEND_URL,
    SKU,
    WRITE_URL,
    FakeTime,
    authenticated_session,
    captured_null_stock,
    load_fixture,
    null_stock_snapshot,
    point_off_unspecified,
    session_blob,
    snapshot_from,
    synthetic_offers_page,
)

from kaspisa.kaspi import (
    KaspiClient,
    KaspiSession,
    PriceWriteOutcomeUnknownError,
    RateLimitedError,
    SessionBlobError,
    SessionStateError,
    UnexpectedStatusError,
    WriteRefusal,
    WriteRefusedError,
)
from kaspisa.kaspi.cabinet import build_price_update, is_session_expired
from kaspisa.kaspi.login import is_kaspi_url
from kaspisa.security import Secret

CITY = "710000000"
OFFERS_URL = "https://kaspi.kz/yml/offer-view/offers/900000005"


def _state() -> dict[str, Any]:
    state: dict[str, Any] = load_fixture("edit-price.json")
    return state


@pytest.mark.parametrize(
    ("status", "expected"),
    [(299, False), (300, True), (399, True), (400, False), (401, True), (403, False), (404, False)],
)
def test_session_expired_status_boundaries(status: int, expected: bool) -> None:
    """Границы диапазона перенаправлений: 300 и 399 - истёкшая сессия, 299 и 400 - нет."""
    assert is_session_expired(status, httpx.Headers()) is expected


def test_stock_count_null_with_specified_flag_is_refused() -> None:
    """Т35: `stockCount: null` при `stockSpecified: true` - `UNVERIFIED_STORE_STATE` (ВТн)."""
    row, state = point_off_unspecified()
    for availability in row["availabilities"]:
        if availability["storeId"] == "90100003_PP1":
            availability["stockSpecified"] = True
    with pytest.raises(WriteRefusedError) as info:
        build_price_update(snapshot_from(state, row), {CITY: 1650})
    assert info.value.reason is WriteRefusal.UNVERIFIED_STORE_STATE


def test_duplicated_store_in_listing_is_stores_changed() -> None:
    """Точка, повторённая в строке списка, - `STORES_CHANGED`, тело не собирается."""
    sample = captured_null_stock()
    row = sample["listRow"]
    row["availabilities"].append(copy.deepcopy(row["availabilities"][0]))
    with pytest.raises(WriteRefusedError) as info:
        build_price_update(snapshot_from(sample["editPrice"], row), {CITY: 2600})
    assert info.value.reason is WriteRefusal.STORES_CHANGED


def test_extra_store_in_listing_is_stores_changed() -> None:
    """В строке списка есть точка, которой нет в свежем чтении, - `STORES_CHANGED`."""
    sample = captured_null_stock()
    row = sample["listRow"]
    extra = copy.deepcopy(row["availabilities"][0])
    extra["storeId"] = "90100003_PP99"
    row["availabilities"].append(extra)
    with pytest.raises(WriteRefusedError) as info:
        build_price_update(snapshot_from(sample["editPrice"], row), {CITY: 2600})
    assert info.value.reason is WriteRefusal.STORES_CHANGED


@pytest.mark.parametrize("price", [2600.0, 2600.5, "2600", None])
def test_non_integer_price_is_refused(price: Any) -> None:
    """Цены - целые тенге: `float`, строка и `None` отвергаются до запроса."""
    sample = captured_null_stock()
    with pytest.raises(WriteRefusedError) as info:
        build_price_update(snapshot_from(sample["editPrice"], sample["listRow"]), {CITY: price})
    assert info.value.reason is WriteRefusal.INVALID_PRICES


async def test_write_500_is_unknown_outcome_and_not_repeated(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Статус 500 на записи - неизвестный исход; запись не повторяется."""
    route = mock_kaspi.post(WRITE_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(PriceWriteOutcomeUnknownError):
        await client.set_price(authenticated_session(), null_stock_snapshot(), {CITY: 2600})
    assert route.call_count == 1


async def test_list_products_stops_when_total_reached_exactly(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """`total` равен размеру страницы - ровно один запрос, без лишней пустой страницы."""
    template = load_fixture("product-list.json")["data"][0]
    rows = []
    for index in range(100):
        row = copy.deepcopy(template)
        row["sku"] = f"sku-{index}"
        rows.append(row)
    route = mock_kaspi.post(url__startswith=LIST_URL).mock(
        return_value=httpx.Response(201, json={"data": rows, "total": 100})
    )
    products = await client.list_products(authenticated_session(), MERCHANT_UID)
    assert len(products) == 100
    assert route.call_count == 1


async def test_list_products_empty_page_stops_even_if_total_is_larger(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т27: обход кончается на короткой или пустой странице, хотя `total` обещал больше."""
    row = load_fixture("product-list.json")["data"][0]
    route = mock_kaspi.post(url__startswith=LIST_URL).mock(
        return_value=httpx.Response(201, json={"data": [row], "total": 1000})
    )
    products = await client.list_products(authenticated_session(), MERCHANT_UID)
    assert len(products) == 1
    assert route.call_count == 1  # короткая страница - конец выдачи

    route.mock(
        side_effect=[
            httpx.Response(201, json={"data": [row] * 100, "total": 1000}),
            httpx.Response(201, json={"data": [], "total": 1000}),
        ]
    )
    products = await client.list_products(authenticated_session(), MERCHANT_UID)
    assert len(products) == 100
    assert route.call_count == 3


async def test_get_offers_sends_city_on_every_page(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """`cityId` и `Referer` идут в каждом запросе страницы, не только в первом."""
    offers: list[dict[str, Any]] = load_fixture("offers.json")["offers"]
    route = mock_kaspi.post(OFFERS_URL).mock(
        side_effect=[
            httpx.Response(200, json=synthetic_offers_page(offers[0:3], 7)),
            httpx.Response(200, json=synthetic_offers_page(offers[3:6], 7)),
        ]
    )
    # Своего магазина нет на первой странице - читается вторая.
    await client.get_offers("900000005", "710000000", "absent", page_size=3)
    assert route.call_count == 2
    for call in route.calls:
        assert json.loads(call.request.content)["cityId"] == "710000000"
        assert call.request.headers["referer"].endswith("?c=710000000")


@pytest.mark.parametrize(
    ("status", "retried"),
    [(500, False), (501, False), (502, True), (503, True), (504, True)],
)
async def test_retry_statuses_exact_set(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    fake_time: FakeTime,
    status: int,
    retried: bool,
) -> None:
    """Чтение повторяется только при 502, 503, 504; остальные 5xx - сразу ошибка."""
    route = mock_kaspi.post(OFFERS_URL).mock(
        side_effect=[httpx.Response(status), httpx.Response(200, json=load_fixture("offers.json"))]
    )
    if retried:
        assert len((await client.get_offers("900000005", "710000000", "90100003")).offers) == 7
        assert route.call_count == 2
        assert fake_time.sleeps == [1.0]
    else:
        with pytest.raises(UnexpectedStatusError) as info:
            await client.get_offers("900000005", "710000000", "90100003")
        assert info.value.status_code == status
        assert route.call_count == 1
        assert fake_time.sleeps == []


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("30", 30),
        (" 7 ", 7),
        ("abc", None),
        ("-5", None),
        ("1.5", None),
        ("Wed, 21 Oct 2026", None),
        ("\u00b2", None),
        ("\u0663", None),
    ],
)
async def test_retry_after_parsing(
    client: KaspiClient, mock_kaspi: respx.MockRouter, header: str, expected: int | None
) -> None:
    """`Retry-After` - только целое неотрицательное число секунд; иное - `None`, без падения."""
    mock_kaspi.post(OFFERS_URL).mock(
        return_value=httpx.Response(429, headers=[(b"Retry-After", header.encode("utf-8"))])
    )
    with pytest.raises(RateLimitedError) as info:
        await client.get_offers("900000005", "710000000", "90100003")
    assert info.value.retry_after == expected


@pytest.mark.parametrize("header", ["\u00b2", "\u0663"])
async def test_non_ascii_retry_after_gives_client_error_on_write_and_read(
    client: KaspiClient, mock_kaspi: respx.MockRouter, header: str
) -> None:
    """429 с не-ASCII цифрой в `Retry-After` - `RateLimitedError` без значения, не `ValueError`."""
    reply = httpx.Response(429, headers=[(b"Retry-After", header.encode("utf-8"))])
    mock_kaspi.post(WRITE_URL).mock(return_value=reply)
    with pytest.raises(RateLimitedError) as written:
        await client.set_price(authenticated_session(), null_stock_snapshot(), {CITY: 2600})
    assert written.value.retry_after is None
    mock_kaspi.post(OFFERS_URL).mock(return_value=reply)
    with pytest.raises(RateLimitedError) as read:
        await client.get_offers("900000005", "710000000", "90100003")
    assert read.value.retry_after is None


async def test_requests_carry_user_agent_and_accept(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Каждый запрос несёт постоянные `User-Agent` браузера и `Accept: application/json`."""
    route = mock_kaspi.post(OFFERS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("offers.json"))
    )
    await client.get_offers("900000005", "710000000", "90100003")
    headers = route.calls.last.request.headers
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert "httpx" not in headers["user-agent"]
    assert headers["accept"] == "application/json"


async def test_unchanged_cookies_do_not_set_changed(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """`changed` ставится только при реальной смене cookie: тот же `Set-Cookie` повторно - нет."""
    session = authenticated_session()
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        return_value=httpx.Response(200, json=_state())
    )
    await client.get_product(session, MERCHANT_UID, SKU)
    assert session.changed is False  # без `Set-Cookie`
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        return_value=httpx.Response(200, json=_state(), headers={"Set-Cookie": "mc-new=v1; Path=/"})
    )
    await client.get_product(session, MERCHANT_UID, SKU)
    assert session.changed is True
    session.changed = False
    await client.get_product(session, MERCHANT_UID, SKU)  # то же значение второй раз
    assert session.changed is False


@pytest.mark.parametrize("domain", ["evilkaspi.kz", "notkaspi.kz", "kaspi.kz.evil.example"])
def test_blob_cookie_domain_must_be_kaspi_or_subdomain(domain: str) -> None:
    """Домен cookie в блоке: только `kaspi.kz` и его поддомены; суффикс без точки не годится."""
    cookie = {
        "name": "c",
        "value": "v",
        "domain": domain,
        "path": "/",
        "secure": True,
        "expires": None,
        "host_only": True,
    }
    with pytest.raises(SessionBlobError):
        KaspiSession.load(session_blob(cookies=[cookie]))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://kaspi.kz/mc/", True),
        ("https://idmc.shop.kaspi.kz/x", True),
        ("https://kaspi.kz:443/mc/", True),
        ("https://kaspi.kz:8443/mc/", False),
        ("https://user:pw@kaspi.kz/mc/", False),
        ("https://user@kaspi.kz/mc/", False),
        ("http://kaspi.kz/mc/", False),
        ("https://evilkaspi.kz/mc/", False),
        ("https://kaspi.kz.evil.example/", False),
        ("https://kaspi.kz:abc/", False),
        ("https://xn--kaspi-.kaspi.kz/", False),
        ("https://a\tb.kaspi.kz/", False),
        ("https://a_b.kaspi.kz/", False),
        ("https://a%2fb.kaspi.kz/", False),
        ("https://-a.kaspi.kz/", False),
        ("https://idmc.shop.kaspi.kz/login", True),
        ("https://KASPI.KZ/mc/", True),
        ("https://kaspi.kz:443/", True),
        ("", False),
    ],
)
def test_is_kaspi_url_table(url: str, expected: bool) -> None:
    """Проверка адреса перенаправления входа: схема, хост, порт, логин в адресе."""
    assert is_kaspi_url(url) is expected


async def test_resend_code_429_is_rate_limited(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """429 на повторной отправке кода - `RateLimitedError` с `Retry-After`; фаза не меняется."""
    session = KaspiSession.load(session_blob(phase="awaiting_code", pending_login="m@e.invalid"))
    route = mock_kaspi.post(MFA_SEND_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "180"})
    )
    with pytest.raises(RateLimitedError) as info:
        await client.resend_code(session)
    assert info.value.retry_after == 180
    assert route.call_count == 1


async def test_authenticated_session_with_stale_pending_cannot_submit_code(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Сессия в фазе `authenticated` не отправляет код, даже если в блоке остался адрес."""
    route = mock_kaspi.route().mock(return_value=httpx.Response(200))
    session = KaspiSession.load(session_blob(phase="authenticated", pending_login="m@e.invalid"))
    with pytest.raises(SessionStateError):
        await client.submit_code(session, "000000")
    assert route.call_count == 0


async def test_only_foreign_cookie_leaves_session_unchanged(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """`Set-Cookie` только постороннего домена: сессия не меняется и не помечается `changed`."""
    session = authenticated_session()
    before = repr(session)
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        return_value=httpx.Response(
            200,
            json=_state(),
            headers={"Set-Cookie": "foreign=marker-foreign; Domain=evil.example; Path=/"},
        )
    )
    await client.get_product(session, MERCHANT_UID, SKU)
    assert session.changed is False
    assert repr(session) == before
