"""Запросы кабинета: чтение товара, список, признак истёкшей сессии, ключи ограничителя
(Т24-Т30, Т32)."""

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
    LOGIN_API_URL,
    LOGIN_MARKER,
    LOGIN_PAGE_URL,
    MERCHANT_UID,
    PASSWORD_MARKER,
    SESSION_COOKIE_VALUE,
    SKU,
    WRITE_URL,
    FakeTime,
    RecordingLimiter,
    authenticated_session,
    captured_null_stock,
    expired_response,
    load_fixture,
    null_stock_snapshot,
    session_blob,
    synthetic_oauth_start,
    synthetic_redirect_response,
)

from kaspisa.kaspi import (
    KaspiClient,
    KaspiCredentials,
    KaspiSession,
    ResponseFormatError,
    SessionExpiredError,
    SessionStateError,
    UnexpectedStatusError,
)
from kaspisa.kaspi.cabinet import is_session_expired
from kaspisa.security import Secret


async def test_get_product_request_shape(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т24: GET, параметры, заголовки кабинета, cookie сессии; результат - `ProductState`."""
    route = mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("edit-price.json"))
    )
    state = await client.get_product(authenticated_session(), MERCHANT_UID, SKU)
    request = route.calls.last.request
    assert request.method == "GET"
    assert request.url.params["m"] == MERCHANT_UID
    assert request.url.params["s"] == SKU
    assert request.headers["x-auth-version"] == "3"
    assert request.headers["origin"] == "https://kaspi.kz"
    assert request.headers["referer"] == "https://kaspi.kz/"
    assert f"mc-session={SESSION_COOKIE_VALUE}" in request.headers["cookie"]
    assert state.sku == SKU
    assert state.cities[0].id == "710000000"


async def test_get_product_other_sku_is_format_error(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т25: `sku` ответа не равен запрошенному - `ResponseFormatError`."""
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("edit-price.json"))
    )
    with pytest.raises(ResponseFormatError) as info:
        await client.get_product(authenticated_session(), MERCHANT_UID, "other-sku")
    assert info.value.fields == ("sku",)
    assert SKU not in str(info.value)


async def test_list_products_status_201_and_request(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т26: статус 201 - успех; POST с телом `{"available": true}`; `l`, `start`, `sortBy`."""
    route = mock_kaspi.post(url__startswith=LIST_URL).mock(
        return_value=httpx.Response(201, json=load_fixture("product-list.json"))
    )
    # В примере `total` = 998, поэтому ограничиваем обход одной страницей.
    page = load_fixture("product-list.json")
    page["total"] = len(page["data"])
    route.mock(return_value=httpx.Response(201, json=page))
    products = await client.list_products(authenticated_session(), MERCHANT_UID)
    assert len(products) == 2
    request = route.calls.last.request
    assert request.method == "POST"
    assert json.loads(request.content) == {"available": True}
    params = request.url.params
    assert (params["m"], params["l"], params["start"], params["sortBy"]) == (
        MERCHANT_UID,
        "100",
        "0",
        "DATE_DESC",
    )
    assert products[0].availabilities[0].stock_count is None
    assert products[0].archived_at is None


async def test_list_products_all_pages(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т27: `total=250`, страницы по 100 - `start` 0, 100, 200; 250 товаров."""
    template = load_fixture("product-list.json")["data"][0]

    def reply(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        count = min(100, 250 - start)
        rows = []
        for index in range(start, start + count):
            row = copy.deepcopy(template)
            row["sku"] = f"sku-{index}"
            rows.append(row)
        return httpx.Response(201, json={"data": rows, "total": 250})

    route = mock_kaspi.post(url__startswith=LIST_URL).mock(side_effect=reply)
    products = await client.list_products(authenticated_session(), MERCHANT_UID)
    assert [c.request.url.params["start"] for c in route.calls] == ["0", "100", "200"]
    assert len(products) == 250
    assert products[249].sku == "sku-249"


async def test_list_products_too_many_pages(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т27 (предел): больше 200 страниц по 100 строк - `ResponseFormatError`."""
    row = load_fixture("product-list.json")["data"][0]
    route = mock_kaspi.post(url__startswith=LIST_URL).mock(
        return_value=httpx.Response(201, json={"data": [row] * 100, "total": 10**9})
    )
    with pytest.raises(ResponseFormatError):
        await client.list_products(authenticated_session(), MERCHANT_UID)
    assert route.call_count == 200


def _write_snapshot() -> Any:
    return null_stock_snapshot()


async def _invoke(client: KaspiClient, method: str, session: KaspiSession) -> None:
    """Вызывает один из запросов кабинета по имени метода."""
    prices = {"710000000": 2600}
    if method == "get_product":
        await client.get_product(session, MERCHANT_UID, SKU)
    elif method == "list_products":
        await client.list_products(session, MERCHANT_UID)
    elif method == "find_product":
        await client.find_product(session, MERCHANT_UID, SKU)
    elif method == "set_price":
        await client.set_price(session, _write_snapshot(), prices)
    else:
        await client.verify_price(session, _write_snapshot(), prices)


ALL_CABINET_METHODS = ["get_product", "list_products", "find_product", "set_price", "verify_price"]


@pytest.mark.parametrize("status", [401, 302])
@pytest.mark.parametrize("method", ALL_CABINET_METHODS)
async def test_expired_session_in_every_cabinet_call(
    client: KaspiClient, mock_kaspi: respx.MockRouter, method: str, status: int
) -> None:
    """Т28: 401 (проверено, тело пустое) и 302 (синтетика, предположение) - сессия истекла."""
    reply = expired_response() if status == 401 else synthetic_redirect_response()
    mock_kaspi.route(host="mc.shop.kaspi.kz").mock(return_value=reply)
    with pytest.raises(SessionExpiredError) as info:
        await _invoke(client, method, authenticated_session())
    assert info.value.__cause__ is None


@pytest.mark.parametrize("method", ALL_CABINET_METHODS)
async def test_forbidden_is_unexpected_status_not_expired_session(
    client: KaspiClient, mock_kaspi: respx.MockRouter, method: str
) -> None:
    """Т28: 403 приходит и при действующей сессии (чужой `origin`) - `UnexpectedStatusError`.

    У `set_price` запись при 403 отправлена один раз (снимок записи - снятый НО).
    """
    route = mock_kaspi.route(host="mc.shop.kaspi.kz").mock(return_value=httpx.Response(403))
    with pytest.raises(UnexpectedStatusError) as info:
        await _invoke(client, method, authenticated_session())
    assert info.value.status_code == 403
    if method == "set_price":
        assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, False),
        (201, False),
        (301, True),
        (302, True),
        (401, True),
        (403, False),
        (404, False),
        (429, False),
        (500, False),
    ],
)
def test_is_session_expired_table(status: int, expected: bool) -> None:
    """Т29: таблица статусов; 403 - не признак истёкшей сессии."""
    assert is_session_expired(status, httpx.Headers()) is expected


@pytest.mark.parametrize("method", ALL_CABINET_METHODS)
async def test_wrong_phase_refused_without_requests(
    client: KaspiClient, mock_kaspi: respx.MockRouter, method: str
) -> None:
    """Т30: сессия не в фазе `AUTHENTICATED` - `SessionStateError`, запросов нет."""
    route = mock_kaspi.route().mock(return_value=httpx.Response(200))
    for session in (
        KaspiSession(),
        KaspiSession.load(session_blob(phase="awaiting_code", pending_login="m@example.invalid")),
    ):
        with pytest.raises(SessionStateError):
            await _invoke(client, method, session)
    assert route.call_count == 0


async def test_find_product_request_and_row(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т73 (а): один запрос с `searchTerm` и `available`; параметры; строка товара."""
    sample = captured_null_stock()
    route = mock_kaspi.post(url__startswith=LIST_URL).mock(
        return_value=httpx.Response(201, json={"data": [sample["listRow"]], "total": 1})
    )
    row = await client.find_product(authenticated_session(), MERCHANT_UID, SKU)
    assert row is not None
    assert row.sku == SKU
    assert row.availabilities[0].stock_count is None
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.method == "POST"
    assert json.loads(request.content) == sample["listRequestBody"]
    params = request.url.params
    assert (params["m"], params["l"], params["start"], params["sortBy"]) == (
        MERCHANT_UID,
        "100",
        "0",
        "DATE_DESC",
    )


async def test_find_product_selects_by_sku(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т73 (б-д): пусто и чужие строки - `None`; чужая и своя - своя; две своих - ошибка."""
    own = captured_null_stock()["listRow"]
    foreign = load_fixture("product-list.json")["data"]
    session = authenticated_session()
    route = mock_kaspi.post(url__startswith=LIST_URL)

    route.mock(return_value=httpx.Response(201, json={"data": [], "total": 0}))
    assert await client.find_product(session, MERCHANT_UID, SKU) is None
    route.mock(return_value=httpx.Response(201, json={"data": foreign, "total": 2}))
    assert await client.find_product(session, MERCHANT_UID, SKU) is None
    route.mock(return_value=httpx.Response(201, json={"data": [foreign[0], own], "total": 2}))
    found = await client.find_product(session, MERCHANT_UID, SKU)
    assert found is not None
    assert found.sku == SKU
    route.mock(return_value=httpx.Response(201, json={"data": [own, own], "total": 2}))
    with pytest.raises(ResponseFormatError) as info:
        await client.find_product(session, MERCHANT_UID, SKU)
    assert info.value.fields == ("data",)


async def test_find_product_empty_sku_sends_nothing(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т73 (е): пустой `sku` - `ValueError`, запросов нет."""
    route = mock_kaspi.route().mock(return_value=httpx.Response(200))
    with pytest.raises(ValueError, match="sku"):
        await client.find_product(authenticated_session(), MERCHANT_UID, "")
    assert route.call_count == 0


async def test_limiter_keys_per_merchant(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    limiter: RecordingLimiter,
) -> None:
    """Т32: ключи `mc:{merchant_uid}`; запись идёт под ключом магазина из строки списка."""
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("edit-price.json"))
    )
    mock_kaspi.post(WRITE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("price-update-response.json"))
    )
    session = authenticated_session()
    await client.get_product(session, MERCHANT_UID, SKU)
    await client.get_product(session, "99999999", SKU)
    await client.set_price(session, _write_snapshot(), {"710000000": 2600})
    assert limiter.keys == [f"mc:{MERCHANT_UID}", "mc:99999999", f"mc:{MERCHANT_UID}"]
    first, other, third = limiter.passed
    assert other[1] == first[1]  # разные магазины друг друга не ждут
    assert third[1] - first[1] >= 1.0  # один магазин - с шагом не меньше секунды


async def test_deeply_nested_body_is_format_error(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т70: тело из 100 000 скобок - `ResponseFormatError`, а не `RecursionError`."""
    deep = httpx.Response(200, content=b"[" * 100_000)
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(return_value=deep)
    with pytest.raises(ResponseFormatError) as info:
        await client.get_product(authenticated_session(), MERCHANT_UID, SKU)
    assert info.value.fields == ("<body>",)
    write = mock_kaspi.post(WRITE_URL).mock(return_value=deep)
    with pytest.raises(ResponseFormatError):
        await client.set_price(authenticated_session(), null_stock_snapshot(), {"710000000": 2600})
    assert write.call_count == 1


@pytest.mark.parametrize(
    ("status", "expected"), [(200, ResponseFormatError), (400, UnexpectedStatusError)]
)
async def test_deeply_nested_login_body(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    status: int,
    expected: type[Exception],
) -> None:
    """Т70: глубоко вложенное тело POST входа: 200 - формат, 400 - статус; без `RecursionError`."""
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(return_value=httpx.Response(status, content=b"[" * 100_000))
    credentials = KaspiCredentials(login=Secret(LOGIN_MARKER), password=Secret(PASSWORD_MARKER))
    with pytest.raises(expected):
        await client.login(credentials)
