"""Запись цены: сборка тела, отказы, неизвестный исход, проверка (Т33-Т39, Т67)."""

# ruff: noqa: F401, F811  (фикстуры импортируются из kaspi_fixtures; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import copy
import json
import traceback
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st
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
    SESSION_COOKIE_VALUE,
    SKU,
    WRITE_URL,
    authenticated_session,
    captured_null_stock,
    captured_point_off,
    captured_stock_counted,
    load_fixture,
    null_stock_snapshot,
    point_off_unspecified,
    snapshot_from,
)

from kaspisa.kaspi import (
    KaspiClient,
    PriceWriteOutcomeUnknownError,
    RateLimitedError,
    ResponseFormatError,
    UnexpectedStatusError,
    WriteRefusal,
    WriteRefusedError,
)
from kaspisa.kaspi.cabinet import build_price_update
from kaspisa.kaspi.transport import Transport

CITY = "710000000"


def _state() -> dict[str, Any]:
    state: dict[str, Any] = load_fixture("edit-price.json")
    return state


def _write_route(mock_kaspi: respx.MockRouter) -> respx.Route:
    return mock_kaspi.post(WRITE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("price-update-response.json"))
    )


async def test_body_equals_captured_request_and_returns_job_id(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т33: товар с неуказанным остатком записывается; тело равно снятому запросу кабинета."""
    sample = captured_null_stock()
    route = mock_kaspi.post(WRITE_URL).mock(
        return_value=httpx.Response(
            sample["priceUpdateResponse"]["status"], json=sample["priceUpdateResponse"]["body"]
        )
    )
    receipt = await client.set_price(authenticated_session(), null_stock_snapshot(), {CITY: 2998})
    assert receipt.job_id == "9f0000000000000000000004"
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == sample["priceUpdateRequest"]
    assert [a["storeId"] for a in body["availabilities"]] == ["90100003_PP3", "90100003_PP1"]
    assert "stockCount" not in body["availabilities"][1]  # у PP1 ключа нет совсем
    assert route.call_count == 1
    assert request.headers["x-auth-version"] == "3"
    await client.set_price(authenticated_session(), null_stock_snapshot(), {CITY: 2997})
    changed = json.loads(route.calls.last.request.content)
    assert changed == {
        **sample["priceUpdateRequest"],
        "cityPrices": [{"cityId": CITY, "value": 2997}],
    }


async def test_all_points_unspecified_on_real_listing_row(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т34: строка `product-list.json` (PP1 и PP3 не указаны, цена 931) записывается без отказа.

    Чтение дополнено, не снято: взято `edit-price.json` с `sku` и ценой строки и остатками `null`.
    """
    route = _write_route(mock_kaspi)
    row = load_fixture("product-list.json")["data"][0]
    state = _state()
    state["sku"] = row["sku"]
    state["cities"][0]["price"] = 931
    for point in state["cities"][0]["points"]:
        point["stock"] = None
    await client.set_price(authenticated_session(), snapshot_from(state, row), {CITY: 900})
    body = json.loads(route.calls.last.request.content)
    assert body["availabilities"] == [
        {"available": "yes", "storeId": "90100003_PP3"},
        {"available": "yes", "storeId": "90100003_PP1"},
    ]
    assert body["cityPrices"] == [{"cityId": CITY, "value": 900}]
    assert route.call_count == 1


def _unspecified() -> tuple[dict[str, Any], dict[str, Any]]:
    """ВТн: строка списка и чтение (правка снятого примера, не снято)."""
    return point_off_unspecified()


def _list_point(row: dict[str, Any], store: str) -> dict[str, Any]:
    point: dict[str, Any] = next(
        a for a in row["availabilities"] if a["storeId"] == f"90100003_{store}"
    )
    return point


def _read_point(state: dict[str, Any], store: str) -> dict[str, Any]:
    point: dict[str, Any] = next(
        p for c in state["cities"] for p in c["points"] if p["id"] == f"90100003_{store}"
    )
    return point


def _pp1_preorder(value: int | None) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def edit(row: dict[str, Any], state: dict[str, Any]) -> None:
        _list_point(row, "PP1")["preOrder"] = value

    return edit


def _read_pp1_stock(value: int | None) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def edit(row: dict[str, Any], state: dict[str, Any]) -> None:
        _read_point(state, "PP1")["stock"] = value

    return edit


def _pp1_specified_with_null(row: dict[str, Any], state: dict[str, Any]) -> None:
    _list_point(row, "PP1")["stockSpecified"] = True


def _archived(row: dict[str, Any], state: dict[str, Any]) -> None:
    row["archivedAt"] = "2026-01-01T00:00:00Z"


def _extra_read_point(row: dict[str, Any], state: dict[str, Any]) -> None:
    state["cities"][0]["points"].append(
        {"id": "90100003_PP9", "address": None, "displayName": "PP9", "stock": 1}
    )


def _title(value: str | None) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def edit(row: dict[str, Any], state: dict[str, Any]) -> None:
        state["title"] = value

    return edit


_GOOD = {CITY: 1650}
_NO_EDIT: Callable[[dict[str, Any], dict[str, Any]], None] = lambda row, state: None  # noqa: E731


@pytest.mark.parametrize(
    ("reason", "edit", "prices"),
    [
        (WriteRefusal.PREORDER, _pp1_preorder(3), _GOOD),
        (WriteRefusal.PREORDER, _pp1_preorder(None), _GOOD),
        (WriteRefusal.STALE_LISTING, _read_pp1_stock(7), _GOOD),
        (WriteRefusal.UNVERIFIED_STORE_STATE, _pp1_specified_with_null, _GOOD),
        (WriteRefusal.ARCHIVED, _archived, _GOOD),
        (WriteRefusal.STALE_LISTING, _extra_read_point, _GOOD),
        (WriteRefusal.INVALID_PRICES, _NO_EDIT, {CITY: 0}),
        (WriteRefusal.INVALID_PRICES, _NO_EDIT, {CITY: -5}),
        (WriteRefusal.INVALID_PRICES, _NO_EDIT, {CITY: True}),
        (WriteRefusal.INVALID_PRICES, _NO_EDIT, {"111": 1650}),
        (WriteRefusal.INVALID_PRICES, _NO_EDIT, {}),
    ],
)
async def test_refusals_send_nothing(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    reason: WriteRefusal,
    edit: Callable[[dict[str, Any], dict[str, Any]], None],
    prices: dict[str, Any],
) -> None:
    """Т35: на базе ВТн каждый отказ с причиной, запрос записи не отправлен."""
    route = _write_route(mock_kaspi)
    row, state = _unspecified()
    edit(row, state)
    with pytest.raises(WriteRefusedError) as info:
        await client.set_price(authenticated_session(), snapshot_from(state, row), prices)
    assert info.value.reason is reason
    assert info.value.sku == "900000001_900000021"
    assert route.call_count == 0


@pytest.mark.parametrize("title", ["", "   "])
async def test_blank_title_sent_verbatim(
    client: KaspiClient, mock_kaspi: respx.MockRouter, title: str
) -> None:
    """ADR 0019 (Т2): пустой `title` и `title` из пробелов уходят в `model` как есть, без отказа."""
    route = _write_route(mock_kaspi)
    row, state = _unspecified()
    _title(title)(row, state)
    await client.set_price(authenticated_session(), snapshot_from(state, row), _GOOD)
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == title
    assert body["cityPrices"] == [{"cityId": CITY, "value": 1650}]
    assert body["sku"] == "900000001_900000021"


def _null_title_snapshot() -> Any:
    """Чтение `edit-price-null-title.json` и строка списка, собранная здесь (не снято)."""
    state = load_fixture("edit-price-null-title.json")
    row = {
        "sku": "900000003_900001",
        "masterSku": "900000003",
        "merchantUid": "90100003",
        "available": True,
        "cityPrices": [{"cityId": CITY, "value": 600}],
        "availabilities": [
            {
                "available": "yes",
                "preOrder": 0,
                "stockCount": None,
                "stockSpecified": False,
                "storeId": f"90100003_{store}",
            }
            for store in ("PP3", "PP1")
        ],
    }
    return snapshot_from(state, row)


def _without_comment(name: str) -> dict[str, Any]:
    data: dict[str, Any] = load_fixture(name)
    return {k: v for k, v in data.items() if k != "_comment"}


async def test_null_title_sends_model_null(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """ADR 0019 (Т1): `title: null` - запись уходит, в теле ключ `model` со значением `null`."""
    route = _write_route(mock_kaspi)
    await client.set_price(authenticated_session(), _null_title_snapshot(), {CITY: 605})
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body == _without_comment("price-update-request-null-model.json")
    assert "model" in body
    assert body["model"] is None
    assert all("stockCount" not in a for a in body["availabilities"])


async def test_unmodified_point_off_product_sends_counted_stock(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т35, этап 23 (Т3): снятый ВТ без правок (PP1 `yes` 24, в чтении 24) - запись с 24."""
    route = _write_route(mock_kaspi)
    row, state = captured_point_off()
    await client.set_price(authenticated_session(), snapshot_from(state, row), _GOOD)
    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent["availabilities"] == [
        {"available": "yes", "storeId": "90100003_PP3", "stockCount": 0},
        {"available": "yes", "storeId": "90100003_PP1", "stockCount": 24},
    ]


async def test_counted_stock_sent_from_fresh_read(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Этап 23, Т1: строка PP1 100, чтение PP1 94 - в теле 94 из чтения, порядок точек из чтения."""
    route = _write_route(mock_kaspi)
    row, state = captured_stock_counted()
    await client.set_price(authenticated_session(), snapshot_from(state, row), {CITY: 480})
    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    read_points = [p for c in state["cities"] for p in c["points"]]
    assert [a["storeId"] for a in sent["availabilities"]] == [p["id"] for p in read_points]
    for item, point in zip(sent["availabilities"], read_points, strict=True):
        assert item["available"] == "yes"
        assert item["stockCount"] == point["stock"]
        assert type(item["stockCount"]) is int
    assert {a["storeId"]: a["stockCount"] for a in sent["availabilities"]} == {
        "90100003_PP3": 0,
        "90100003_PP1": 94,
    }
    assert _null_paths(sent) == []
    assert sent["cityPrices"] == [{"cityId": CITY, "value": 480}]
    assert (sent["sku"], sent["merchantUid"], sent["model"]) == (
        state["sku"],
        row["merchantUid"],
        state["title"],
    )


@pytest.mark.parametrize(
    ("read", "outcome"),
    [
        (100, 100),
        (94, 94),
        (1, 1),
        (101, 101),
        (1000, 1000),
        (0, WriteRefusal.STALE_LISTING),
        (None, WriteRefusal.STALE_LISTING),
    ],
    ids=["equal", "less", "one", "more", "far-more", "zero", "none"],
)
async def test_counted_stock_stale_rule(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    read: int | None,
    outcome: int | WriteRefusal,
) -> None:
    """Этап 23, Т5: строка PP1 `yes` 100; запись при `R >= 1` (`L` не предел), иначе отказ."""
    route = _write_route(mock_kaspi)
    row, state = captured_stock_counted()
    _read_point(state, "PP1")["stock"] = read
    session = authenticated_session()
    if isinstance(outcome, WriteRefusal):
        with pytest.raises(WriteRefusedError) as info:
            await client.set_price(session, snapshot_from(state, row), {CITY: 480})
        assert info.value.reason is outcome
        assert route.call_count == 0
        return
    await client.set_price(session, snapshot_from(state, row), {CITY: 480})
    sent = json.loads(route.calls.last.request.content)
    pp1 = next(a for a in sent["availabilities"] if a["storeId"] == "90100003_PP1")
    assert pp1["stockCount"] == outcome


def _with_pp2(row: dict[str, Any], state: dict[str, Any], listed: int, read: int) -> None:
    """Добавляет точку PP2 в строку списка (`yes`, `listed`) и в чтение (`read`)."""
    row["availabilities"].append(_availability("PP2", "yes", listed))
    state["cities"][0]["points"].append(
        {"id": "90100003_PP2", "address": None, "displayName": "PP2", "stock": read}
    )


@pytest.mark.parametrize(
    ("pp1", "pp2", "bodies"),
    [
        (94, 60, {"90100003_PP1": 94, "90100003_PP2": 60}),
        (94, 0, None),
        (None, 50, None),
        (1, 1, {"90100003_PP1": 1, "90100003_PP2": 1}),
    ],
    ids=["both-counted", "pp2-zero", "pp1-unspecified", "both-one"],
)
async def test_counted_stale_rule_each_point(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    pp1: int | None,
    pp2: int,
    bodies: dict[str, int] | None,
) -> None:
    """Этап 23, Т15: одна плохая точка отказывает всей записи; `L` не предел (PP1 100, PP2 50)."""
    route = _write_route(mock_kaspi)
    row, state = captured_stock_counted()
    _with_pp2(row, state, 50, pp2)
    _read_point(state, "PP1")["stock"] = pp1
    session = authenticated_session()
    if bodies is None:
        with pytest.raises(WriteRefusedError) as info:
            await client.set_price(session, snapshot_from(state, row), {CITY: 480})
        assert info.value.reason is WriteRefusal.STALE_LISTING
        assert route.call_count == 0
        return
    await client.set_price(session, snapshot_from(state, row), {CITY: 480})
    sent = json.loads(route.calls.last.request.content)
    assert [a["storeId"] for a in sent["availabilities"]] == [
        "90100003_PP3",
        "90100003_PP1",
        "90100003_PP2",
    ]
    assert {a["storeId"]: a["stockCount"] for a in sent["availabilities"]} == {
        "90100003_PP3": 0,
        **bodies,
    }


async def test_snapshot_mismatch_refused(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т35: разные `sku` в чтении и строке списка - `SNAPSHOT_MISMATCH`."""
    route = _write_route(mock_kaspi)
    sample = captured_null_stock()
    row = sample["listRow"]
    row["sku"] = "another-sku"
    with pytest.raises(WriteRefusedError) as info:
        await client.set_price(
            authenticated_session(), snapshot_from(sample["editPrice"], row), _GOOD
        )
    assert info.value.reason is WriteRefusal.SNAPSHOT_MISMATCH
    assert route.call_count == 0


def test_missing_store_in_listing_is_stale_listing() -> None:
    """Т35: из строки списка убрана PP1 (в чтении `null`) - `STALE_LISTING`, ждали 0."""
    row, state = _unspecified()
    row["availabilities"] = [a for a in row["availabilities"] if a["storeId"] != "90100003_PP1"]
    with pytest.raises(WriteRefusedError) as info:
        build_price_update(snapshot_from(state, row), _GOOD)
    assert info.value.reason is WriteRefusal.STALE_LISTING


def _pp7_point() -> dict[str, Any]:
    """Синтетика, не снято: точка PP7 с неуказанным остатком."""
    return {"id": "90100003_PP7", "address": None, "displayName": "PP7", "stock": None}


def _second_city(stock: int | None) -> dict[str, Any]:
    """Синтетика, не снято: второй город, в котором есть точка `PP1` из первого города."""
    return {
        "id": "750000000",
        "name": "Второй город",
        "price": 2700,
        "points": [
            {"id": "90100003_PP1", "address": None, "displayName": "PP1", "stock": stock},
        ],
    }


def _with_second_city_price(row: dict[str, Any], price: int = 2700) -> None:
    row["cityPrices"].append({"cityId": "750000000", "oldprice": 0, "value": price})


def test_same_store_in_two_cities_with_different_stock_is_stores_changed() -> None:
    """Т35: одна точка в двух городах с разным остатком - `STORES_CHANGED`."""
    sample = captured_null_stock()
    state, row = sample["editPrice"], sample["listRow"]
    state["cities"].append(_second_city(stock=7))  # у PP1 в первом городе `null`
    _with_second_city_price(row)
    with pytest.raises(WriteRefusedError) as info:
        build_price_update(snapshot_from(state, row), _GOOD)
    assert info.value.reason is WriteRefusal.STORES_CHANGED


def test_same_store_in_two_cities_with_same_stock_is_listed_once() -> None:
    """Т38: точка, общая для двух городов, с одним остатком попадает в тело один раз."""
    sample = captured_null_stock()
    state, row = sample["editPrice"], sample["listRow"]
    state["cities"].append(_second_city(stock=None))
    _with_second_city_price(row)
    body = build_price_update(snapshot_from(state, row), {CITY: 2998})
    assert [a.store_id for a in body.availabilities] == ["90100003_PP3", "90100003_PP1"]
    assert [c.city_id for c in body.city_prices] == ["710000000", "750000000"]


# --- Т36: свойства ---

_STORE_PREFIX = "90100003_"
_SPOILS = [
    "stale_unspecified_read_changed",
    "stale_off_read_changed",
    "stale_absent_read_changed",
    "stale_removed",
    "unverified_marked_no",
    "unverified_absent_added_yes_zero",
    "unverified_flipped_specified",
    "unverified_off_with_stock",
    "preorder",
    "stores_changed",
    "price_mismatch",
    "stale_counted_read_zero",
    "stale_counted_read_null",
]


@st.composite
def _scenario(draw: st.DrawFn) -> dict[str, Any]:
    """Синтетика, не снято: чтение, согласованная строка списка и одна порча (или ничего)."""
    no_points = draw(st.integers(min_value=0, max_value=5)) == 0
    city_ids = draw(
        st.lists(st.sampled_from(["c1", "c2", "c3"]), min_size=1, max_size=3, unique=True)
    )
    stores = draw(
        st.lists(st.sampled_from(["A", "B", "C", "D"]), min_size=1, max_size=4, unique=True)
    )
    stocks = {s: draw(st.sampled_from([None, 0])) for s in stores}
    prices = {c: draw(st.integers(min_value=1, max_value=10**7)) for c in city_ids}
    cities: list[dict[str, Any]] = []
    for city_id in city_ids:
        chosen = [] if no_points else [s for s in stores if draw(st.booleans())] or [stores[0]]
        cities.append(
            {
                "id": city_id,
                "price": prices[city_id],
                "points": [{"id": _STORE_PREFIX + s, "stock": stocks[s]} for s in chosen],
            }
        )
    read_stores = sorted({p["id"] for c in cities for p in c["points"]})
    in_list = {sid: draw(st.booleans()) for sid in read_stores}
    chosen_cities = draw(st.lists(st.sampled_from(city_ids), min_size=1, unique=True))
    return {
        "state": {
            "sku": "S1",
            "masterSku": "M1",
            "title": draw(st.sampled_from([None, "", "   ", "Товар"])),
            "available": True,
            "cities": cities,
        },
        "in_list": in_list,
        "city_prices": {c: draw(st.integers(min_value=1, max_value=10**7)) for c in chosen_cities},
        "action": draw(st.sampled_from(["none", "counted", "spoil"])),
        "spoil": draw(st.sampled_from(_SPOILS)),
        "pick": draw(st.integers(min_value=0, max_value=10)),
        "shift": draw(st.integers(min_value=1, max_value=50)),
        "extra": draw(st.integers(min_value=1, max_value=50)),
        "variant": draw(st.booleans()),
    }


def _consistent_row(state: dict[str, Any], in_list: dict[str, bool]) -> dict[str, Any]:
    """Согласованная строка списка: `null` -> `yes`, не указан; 0 -> `no`, 0 или точки нет."""
    availabilities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for city in state["cities"]:
        for point in city["points"]:
            if point["id"] in seen:
                continue
            seen.add(point["id"])
            if point["stock"] is None:
                availabilities.append(_avail(point["id"], "yes", None))
            elif in_list[point["id"]]:
                availabilities.append(_avail(point["id"], "no", 0))
    return {
        "sku": state["sku"],
        "masterSku": state["masterSku"],
        "merchantUid": MERCHANT_UID,
        "available": True,
        "archivedAt": None,
        "cityPrices": [
            {"cityId": c["id"], "oldprice": 0, "value": c["price"]} for c in state["cities"]
        ],
        "availabilities": availabilities,
    }


def _avail(store: str, available: str, count: int | None) -> dict[str, Any]:
    return {
        "available": available,
        "preOrder": 0,
        "stockCount": count,
        "stockSpecified": count is not None,
        "storeId": store,
    }


def _set_read_stock(state: dict[str, Any], store: str, value: int | None) -> None:
    for city in state["cities"]:
        for point in city["points"]:
            if point["id"] == store:
                point["stock"] = value


def _apply_spoil(
    scenario: dict[str, Any], state: dict[str, Any], row: dict[str, Any]
) -> WriteRefusal | None:
    """Применяет порчу к подходящей точке или городу; `None`, если применять не к чему."""
    kind, pick, shift = scenario["spoil"], scenario["pick"], scenario["shift"]
    listed = row["availabilities"]
    read: dict[str, int | None] = {
        p["id"]: p["stock"] for c in state["cities"] for p in c["points"]
    }

    def choose(items: list[Any]) -> Any:
        return items[pick % len(items)] if items else None

    unspecified = [a for a in listed if a["available"] == "yes"]
    unspecified_yes = [a for a in unspecified if a["stockCount"] is None]
    off = [a for a in listed if a["available"] == "no"]
    absent = [
        sid
        for sid, stock in read.items()
        if stock == 0 and sid not in {a["storeId"] for a in listed}
    ]
    if kind == "stale_unspecified_read_changed" and (a := choose(unspecified)):
        _set_read_stock(state, a["storeId"], 0 if scenario["variant"] else shift)
        return WriteRefusal.STALE_LISTING
    if kind == "stale_off_read_changed" and (a := choose(off)):
        _set_read_stock(state, a["storeId"], None if scenario["variant"] else shift)
        return WriteRefusal.STALE_LISTING
    if kind == "stale_absent_read_changed" and (sid := choose(absent)):
        _set_read_stock(state, sid, None if scenario["variant"] else shift)
        return WriteRefusal.STALE_LISTING
    if kind == "stale_removed" and len(listed) > 1 and (a := choose(unspecified)):
        listed.remove(a)
        return WriteRefusal.STALE_LISTING
    if kind == "unverified_marked_no" and (a := choose(unspecified)):
        a["available"] = "no"
        return WriteRefusal.UNVERIFIED_STORE_STATE
    if kind == "unverified_absent_added_yes_zero" and (sid := choose(absent)):
        listed.append(_avail(sid, "yes", 0))
        return WriteRefusal.UNVERIFIED_STORE_STATE
    if kind == "unverified_flipped_specified" and (a := choose(listed)):
        a["stockSpecified"] = not a["stockSpecified"]
        return WriteRefusal.UNVERIFIED_STORE_STATE
    if kind == "unverified_off_with_stock" and (a := choose(off)):
        a["stockCount"] = shift
        return WriteRefusal.UNVERIFIED_STORE_STATE
    if kind == "preorder" and (a := choose(listed)):
        a["preOrder"] = 5
        return WriteRefusal.PREORDER
    if kind == "stores_changed":
        listed.append(_avail(_STORE_PREFIX + "ZZ", "yes", None))
        return WriteRefusal.STORES_CHANGED
    if kind.startswith("stale_counted_read_") and (a := choose(unspecified_yes)):
        a.update(stockCount=scenario["extra"], stockSpecified=True)
        read_value = {
            "stale_counted_read_zero": 0,
            "stale_counted_read_null": None,
        }[kind]
        _set_read_stock(state, a["storeId"], read_value)
        return WriteRefusal.STALE_LISTING
    if kind == "price_mismatch":
        outside = [p for p in row["cityPrices"] if p["cityId"] not in scenario["city_prices"]]
        if p := choose(outside):
            p["value"] += shift
            return WriteRefusal.PRICE_MISMATCH
        if scenario["variant"]:
            row["cityPrices"].append({"cityId": "c9", "oldprice": 0, "value": shift})
            return WriteRefusal.PRICE_MISMATCH
    return None


@settings(max_examples=300, deadline=None)
@given(_scenario())
def test_body_property(scenario: dict[str, Any]) -> None:
    """Т36: тело повторяет свежее чтение; любая порча или учтённый остаток - отказ."""
    state = scenario["state"]
    row = _consistent_row(state, scenario["in_list"])
    expected_reason: WriteRefusal | None = None
    if scenario["action"] == "counted":
        unspecified = [a for a in row["availabilities"] if a["available"] == "yes"]
        if unspecified:
            # этап 23: указанный остаток, `R >= 1` при любом `L` - тело записывается, отказа нет
            target = unspecified[scenario["pick"] % len(unspecified)]
            _set_read_stock(state, target["storeId"], scenario["shift"])
            target.update(stockCount=scenario["extra"], stockSpecified=True)
    elif scenario["action"] == "spoil":
        expected_reason = _apply_spoil(scenario, state, row)
    read_points = [p for c in state["cities"] for p in c["points"]]
    if not read_points or not row["availabilities"]:
        expected_reason = WriteRefusal.NO_STORES
    city_prices = scenario["city_prices"]
    before = copy.deepcopy((state, row, city_prices))
    snapshot = snapshot_from(state, row)
    if expected_reason is not None:
        with pytest.raises(WriteRefusedError) as refused:
            build_price_update(snapshot, city_prices)
        assert refused.value.reason is expected_reason
        assert (state, row, city_prices) == before
        return
    body = build_price_update(snapshot, city_prices)
    dumped = body.to_json_body()
    assert (state, row, city_prices) == before
    assert "model" in dumped
    assert dumped["model"] == state["title"]
    assert _null_paths(dumped) == ([("model",)] if state["title"] is None else [])
    first_seen = list(dict.fromkeys(p["id"] for p in read_points))
    stocks = {p["id"]: p["stock"] for p in read_points}
    assert dumped["availabilities"]
    assert [a["storeId"] for a in dumped["availabilities"]] == first_seen
    for item in dumped["availabilities"]:
        assert item["available"] == "yes"
        read_stock = stocks[item["storeId"]]
        if read_stock is None:
            assert "stockCount" not in item
        else:
            assert item["stockCount"] == read_stock  # остаток чтения, не строки списка
            assert type(item["stockCount"]) is int
    assert [c["cityId"] for c in dumped["cityPrices"]] == [c["id"] for c in state["cities"]]
    for city, written in zip(state["cities"], dumped["cityPrices"], strict=True):
        assert written["value"] == city_prices.get(city["id"], city["price"])
    assert (dumped["merchantUid"], dumped["sku"], dumped["model"]) == (
        row["merchantUid"],
        state["sku"],
        state["title"],
    )


def _bad_gzip() -> httpx.Response:
    return httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not-gzip")
    )


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (httpx.ReadTimeout("t"), "ReadTimeout"),
        (httpx.ConnectError("c"), "ConnectError"),
        (httpx.Response(503), "status_503"),
        (_bad_gzip(), "DecodingError"),
        (httpx.InvalidURL("x"), "InvalidURL"),
        (RuntimeError("boom-marker"), "RuntimeError"),
    ],
    ids=["read-timeout", "connect-error", "503", "decoding", "invalid-url", "runtime-error"],
)
async def test_write_not_repeated_on_failure(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    failure: Exception | httpx.Response | Callable[[], httpx.Response],
    kind: str,
) -> None:
    """Т37: сбой после отправки - неизвестный исход и ровно один запрос."""
    snapshot = null_stock_snapshot()
    if callable(failure):
        failure = failure()
    route = mock_kaspi.post(WRITE_URL).mock(side_effect=[failure, failure])
    with pytest.raises(PriceWriteOutcomeUnknownError) as info:
        await client.set_price(authenticated_session(), snapshot, _GOOD)
    assert route.call_count == 1
    assert info.value.sku == SKU
    if kind != "status_503":
        assert info.value.kind == kind
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    rendered = "".join(traceback.format_exception(info.value))
    assert SESSION_COOKIE_VALUE not in rendered
    assert "boom-marker" not in rendered


async def test_write_other_replies(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т37: 400, 429 и ответ без `id`."""
    snapshot = null_stock_snapshot()
    session = authenticated_session()
    mock_kaspi.post(WRITE_URL).mock(return_value=httpx.Response(400, json={"error": "x"}))
    with pytest.raises(UnexpectedStatusError) as unexpected:
        await client.set_price(session, snapshot, _GOOD)
    assert unexpected.value.status_code == 400
    mock_kaspi.post(WRITE_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitedError):
        await client.set_price(session, snapshot, _GOOD)
    mock_kaspi.post(WRITE_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(ResponseFormatError):
        await client.set_price(session, snapshot, _GOOD)


async def test_two_cities_order_and_inputs_untouched(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т38: порядок городов и точек, прежняя цена нетронутого города, входы не изменены.

    Основа - снятый НО; второй город `750000000` с точкой PP7 - синтетика, не снято.
    """
    route = _write_route(mock_kaspi)
    sample = captured_null_stock()
    state, row = sample["editPrice"], sample["listRow"]
    state["cities"].append(
        {
            "id": "750000000",
            "name": "Другой город",
            "price": 2700,
            "points": [_pp7_point()],
        }
    )
    row["availabilities"].append(_avail("90100003_PP7", "yes", None))
    _with_second_city_price(row)
    before = copy.deepcopy((state, row))
    snapshot = snapshot_from(state, row)
    prices = {"750000000": 2690}
    await client.set_price(authenticated_session(), snapshot, prices)
    body = json.loads(route.calls.last.request.content)
    assert body["cityPrices"] == [
        {"cityId": CITY, "value": 2998},
        {"cityId": "750000000", "value": 2690},
    ]
    assert body["availabilities"] == [
        {"available": "yes", "storeId": "90100003_PP3", "stockCount": 0},
        {"available": "yes", "storeId": "90100003_PP1"},
        {"available": "yes", "storeId": "90100003_PP7"},
    ]
    assert (state, row) == before
    assert prices == {"750000000": 2690}


def _null_stock_product() -> tuple[Any, dict[str, Any]]:
    """Снимок записи НО и образец целиком (`null-stock-product.json`, снято)."""
    sample = captured_null_stock()
    return snapshot_from(sample["editPrice"], sample["listRow"]), sample


def _row_after_write(sample: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Строка списка НО «после записи»: дополнено, не снято.

    Состояние, наблюдённое 2026-11-14: PP3 стала `no`, 0, `stockSpecified: true`;
    PP1 осталась `yes`, остаток не указан. `changes` - правки конкретных тестов (синтетика).
    """
    row: dict[str, Any] = copy.deepcopy(sample["listRow"])
    row["availabilities"] = [
        {
            "available": "no",
            "preOrder": 0,
            "stockCount": 0,
            "stockSpecified": True,
            "storeId": "90100003_PP3",
        },
        {
            "available": "yes",
            "preOrder": 0,
            "stockCount": None,
            "stockSpecified": False,
            "storeId": "90100003_PP1",
        },
    ]
    row.update(changes)
    return row


def _list_reply(rows: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(201, json={"data": rows, "total": len(rows)})


async def test_verify_price(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т39: проверка по списку: совпало; не совпало; города нет; строки нет; пустой `expected`."""
    product, sample = _null_stock_product()
    row = _row_after_write(sample)
    edit_route = mock_kaspi.get(url__startswith=EDIT_PRICE_URL)
    route = mock_kaspi.post(url__startswith=LIST_URL).mock(return_value=_list_reply([row]))
    session = authenticated_session()

    ok = await client.verify_price(session, product, {CITY: 2998})
    assert ok.applied is True
    assert ok.mismatches == {}
    assert ok.store_anomalies == ()
    assert ok.listing is not None
    assert ok.listing.sku == SKU
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == sample["listRequestBody"]
    assert edit_route.call_count == 0

    bad = await client.verify_price(session, product, {CITY: 2997})
    assert bad.applied is False
    assert bad.mismatches == {CITY: (2997, 2998)}
    assert bad.store_anomalies == ()

    missing_city = await client.verify_price(session, product, {"111": 1000})
    assert missing_city.mismatches == {"111": (1000, None)}

    route.mock(return_value=_list_reply([]))
    absent = await client.verify_price(session, product, {CITY: 2997})
    assert absent.applied is False
    assert absent.listing is None
    assert absent.mismatches == {CITY: (2997, None)}
    assert absent.store_anomalies == ()

    calls_before = route.call_count
    with pytest.raises(ValueError, match="expected"):
        await client.verify_price(session, product, {})
    assert route.call_count == calls_before

    # (е) строка без правок (PP3 нет, PP1 `yes`, не указан) - тоже без аномалий
    route.mock(return_value=_list_reply([sample["listRow"]]))
    untouched = await client.verify_price(session, product, {CITY: 2998})
    assert untouched.applied is True
    assert untouched.store_anomalies == ()


def _availability(store: str, available: str, count: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "available": available,
        "preOrder": 0,
        "stockCount": count,
        "stockSpecified": count is not None,
        "storeId": f"90100003_{store}",
        **extra,
    }


@pytest.mark.parametrize(
    ("availabilities", "price", "anomalies"),
    [
        # (а) PP3 включена при остатке 0
        ([_availability("PP3", "yes", 0), _availability("PP1", "yes", None)], 2998, ("PP3",)),
        # (б) PP1 выключена
        ([_availability("PP3", "no", 0), _availability("PP1", "no", 0)], 2998, ("PP1",)),
        # (в) у PP1 появился остаток 5
        ([_availability("PP3", "no", 0), _availability("PP1", "yes", 5)], 2998, ("PP1",)),
        # (г) точка PP9, которой нет в чтении
        (
            [
                _availability("PP3", "no", 0),
                _availability("PP1", "yes", None),
                _availability("PP9", "yes", None),
            ],
            2998,
            ("PP9",),
        ),
        # (д) предзаказ у PP1
        (
            [_availability("PP3", "no", 0), _availability("PP1", "yes", None, preOrder=3)],
            2998,
            ("PP1",),
        ),
        # (е) PP1 убрана из строки
        ([_availability("PP3", "no", 0)], 2998, ("PP1",)),
        # (ж) цена не применилась: точки не проверяются
        ([_availability("PP3", "yes", 0), _availability("PP1", "yes", None)], 2997, ()),
        # (з) две аномалии, идентификаторы по возрастанию
        (
            [_availability("PP3", "yes", 0), _availability("PP1", "yes", None, preOrder=3)],
            2998,
            ("PP1", "PP3"),
        ),
    ],
    ids=["a", "b", "v", "g", "d", "e", "zh", "z"],
)
async def test_verify_price_store_anomalies(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    availabilities: list[dict[str, Any]],
    price: int,
    anomalies: tuple[str, ...],
) -> None:
    """Т78: точки после записи против чтения снимка; строки собраны в тесте (синтетика)."""
    product, sample = _null_stock_product()
    row = _row_after_write(
        sample,
        availabilities=availabilities,
        cityPrices=[{"cityId": CITY, "oldprice": 0, "value": price}],
    )
    mock_kaspi.post(url__startswith=LIST_URL).mock(return_value=_list_reply([row]))
    result = await client.verify_price(authenticated_session(), product, {CITY: 2998})
    assert result.applied is (price == 2998)
    assert result.store_anomalies == tuple(f"90100003_{name}" for name in anomalies)


def _pp3_absent(row: dict[str, Any], state: dict[str, Any]) -> None:
    row["availabilities"] = [a for a in row["availabilities"] if a["storeId"] != "90100003_PP3"]


def _list_edit(store: str, **fields: Any) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def edit(row: dict[str, Any], state: dict[str, Any]) -> None:
        _list_point(row, store).update(fields)

    return edit


def _read_edit(store: str, stock: int | None) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def edit(row: dict[str, Any], state: dict[str, Any]) -> None:
        _read_point(state, store)["stock"] = stock

    return edit


def _pp1_absent_in_read(row: dict[str, Any], state: dict[str, Any]) -> None:
    state["cities"][0]["points"] = [
        p for p in state["cities"][0]["points"] if p["id"] != "90100003_PP1"
    ]


def _all(
    *edits: Callable[[dict[str, Any], dict[str, Any]], None],
) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def edit(row: dict[str, Any], state: dict[str, Any]) -> None:
        for each in edits:
            each(row, state)

    return edit


_PP1_YES_24 = _list_edit("PP1", stockCount=24, stockSpecified=True)
_BODY = "body"


def _counted(stock: int) -> tuple[str, int]:
    """Исход матрицы: тело, в котором у PP1 `stockCount` равен `stock` (этап 23)."""
    return (_BODY, stock)


@pytest.mark.parametrize(
    ("edit", "outcome"),
    [
        (_NO_EDIT, _BODY),  # а
        (_pp3_absent, _BODY),  # б
        (_all(_pp3_absent, _read_edit("PP3", 5)), WriteRefusal.STALE_LISTING),  # в
        (_all(_pp3_absent, _read_edit("PP3", None)), WriteRefusal.STALE_LISTING),  # г
        (
            _all(_list_edit("PP3", stockCount=5)),
            WriteRefusal.UNVERIFIED_STORE_STATE,
        ),  # д
        (
            _all(_list_edit("PP3", stockCount=5), _read_edit("PP3", 5)),
            WriteRefusal.UNVERIFIED_STORE_STATE,
        ),  # е
        (
            _all(_list_edit("PP3", stockCount=None, stockSpecified=False), _read_edit("PP3", None)),
            WriteRefusal.UNVERIFIED_STORE_STATE,
        ),  # ж
        (_list_edit("PP3", available="yes"), WriteRefusal.UNVERIFIED_STORE_STATE),  # з
        (_read_edit("PP1", 24), WriteRefusal.STALE_LISTING),  # и
        (_read_edit("PP1", 0), WriteRefusal.STALE_LISTING),  # к
        (_all(_PP1_YES_24, _read_edit("PP1", 24)), _counted(24)),  # л
        (_PP1_YES_24, WriteRefusal.STALE_LISTING),  # м
        (_all(_PP1_YES_24, _read_edit("PP1", 23)), _counted(23)),  # н
        (_read_edit("PP3", 3), WriteRefusal.STALE_LISTING),  # о
        (_read_edit("PP3", None), WriteRefusal.STALE_LISTING),  # п
        (_pp1_absent_in_read, WriteRefusal.STORES_CHANGED),  # р
        (
            _all(_list_edit("PP3", stockCount=5), _list_edit("PP1", preOrder=3)),
            WriteRefusal.UNVERIFIED_STORE_STATE,
        ),  # с
        (
            _all(_list_edit("PP3", stockCount=5), _pp1_absent_in_read),
            WriteRefusal.UNVERIFIED_STORE_STATE,
        ),  # т
        (
            _all(_PP1_YES_24, _list_edit("PP1", preOrder=3), _read_edit("PP1", 24)),
            WriteRefusal.PREORDER,
        ),  # у
        (
            _all(_list_edit("PP3", stockCount=5), _PP1_YES_24, _read_edit("PP1", 24)),
            WriteRefusal.UNVERIFIED_STORE_STATE,
        ),  # ф
        (_all(_PP1_YES_24, _read_edit("PP1", 25)), _counted(25)),  # х
    ],
    ids=list("абвгдежзиклмнопрстуфх"),
)
async def test_store_matrix(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    edit: Callable[[dict[str, Any], dict[str, Any]], None],
    outcome: WriteRefusal | str | tuple[str, int],
) -> None:
    """Т67, этап 23 (Т4): таблица случаев на базе ВТн; тело `[{yes, PP3, 0}, {yes, PP1}]` или отказ.

    У точки PP1 с указанным остатком в теле - число из чтения (`_counted`).
    """
    route = _write_route(mock_kaspi)
    row, state = _unspecified()
    edit(row, state)
    session = authenticated_session()
    if outcome == _BODY or isinstance(outcome, tuple):
        await client.set_price(session, snapshot_from(state, row), _GOOD)
        sent = json.loads(route.calls.last.request.content)
        pp1: dict[str, Any] = {"available": "yes", "storeId": "90100003_PP1"}
        if isinstance(outcome, tuple):
            pp1["stockCount"] = outcome[1]
        assert sent["availabilities"] == [
            {"available": "yes", "storeId": "90100003_PP3", "stockCount": 0},
            pp1,
        ]
        assert sent["cityPrices"] == [{"cityId": CITY, "value": 1650}]
        assert route.call_count == 1
        return
    with pytest.raises(WriteRefusedError) as info:
        await client.set_price(session, snapshot_from(state, row), _GOOD)
    assert info.value.reason is outcome
    assert route.call_count == 0


def _no_points_state() -> dict[str, Any]:
    state: dict[str, Any] = captured_null_stock()["editPrice"]
    state["cities"][0]["points"] = []
    return state


@pytest.mark.parametrize("case", ["empty-row", "row-unspecified", "row-point-off"])
async def test_no_stores_refused(
    client: KaspiClient, mock_kaspi: respx.MockRouter, case: str
) -> None:
    """Т68: в свежем чтении нет точек - `NO_STORES`, раньше остальных правил; запись не ушла."""
    route = _write_route(mock_kaspi)
    row = captured_null_stock()["listRow"]
    if case == "empty-row":
        row["availabilities"] = []
    elif case == "row-point-off":
        row["availabilities"] = [_avail("90100003_PP3", "no", 0)]
    with pytest.raises(WriteRefusedError) as info:
        await client.set_price(
            authenticated_session(), snapshot_from(_no_points_state(), row), _GOOD
        )
    assert info.value.reason is WriteRefusal.NO_STORES
    assert route.call_count == 0


async def test_archived_goes_before_no_stores(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т68: порядок правил - архивный товар без точек отказывает как `ARCHIVED`."""
    route = _write_route(mock_kaspi)
    row = captured_null_stock()["listRow"]
    row["archivedAt"] = "2026-01-01T00:00:00Z"
    with pytest.raises(WriteRefusedError) as info:
        await client.set_price(
            authenticated_session(), snapshot_from(_no_points_state(), row), _GOOD
        )
    assert info.value.reason is WriteRefusal.ARCHIVED
    assert route.call_count == 0


async def test_empty_listing_with_fresh_stores_is_no_stores(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т68: точки в чтении есть, а в строке списка пусто - `NO_STORES`; запись не ушла."""
    route = _write_route(mock_kaspi)
    row, state = _unspecified()
    row["availabilities"] = []
    with pytest.raises(WriteRefusedError) as info:
        await client.set_price(authenticated_session(), snapshot_from(state, row), _GOOD)
    assert info.value.reason is WriteRefusal.NO_STORES
    assert route.call_count == 0


def _two_cities(
    row: dict[str, Any], state: dict[str, Any], list_price: int, read_price: int
) -> None:
    """Т74: чтение ВТн плюс город `750000000` (цена 1700, PP7 `null`); в списке - PP7 и цена."""
    state["cities"].append(
        {
            "id": "750000000",
            "name": "Второй город",
            "price": read_price,
            "points": [_pp7_point()],
        }
    )
    row["availabilities"].append(_avail("90100003_PP7", "yes", None))
    row["cityPrices"].append({"cityId": "750000000", "oldprice": 0, "value": list_price})


def _list_city_price(row: dict[str, Any], city: str, value: int) -> None:
    for entry in row["cityPrices"]:
        if entry["cityId"] == city:
            entry["value"] = value


def _case_a(row: dict[str, Any], state: dict[str, Any]) -> None:
    _list_city_price(row, CITY, 1650)  # в списке 1650, в чтении 1660


def _case_b(row: dict[str, Any], state: dict[str, Any]) -> None:
    state["cities"][0]["price"] = 1650  # в списке 1660, в чтении 1650


def _case_v(row: dict[str, Any], state: dict[str, Any]) -> None:
    row["cityPrices"].append({"cityId": "750000000", "oldprice": 0, "value": 1700})


def _case_g(row: dict[str, Any], state: dict[str, Any]) -> None:
    row["cityPrices"] = []


def _case_d(row: dict[str, Any], state: dict[str, Any]) -> None:
    row["cityPrices"].append({"cityId": CITY, "oldprice": 0, "value": 1660})


def _case_e(row: dict[str, Any], state: dict[str, Any]) -> None:
    _two_cities(row, state, list_price=1690, read_price=1700)


_TWO = {CITY: 1650, "750000000": 1695}


@pytest.mark.parametrize(
    ("edit", "prices", "outcome"),
    [
        (_case_a, _GOOD, "sent"),  # а
        (_case_b, _GOOD, "sent"),  # б
        (_case_v, _GOOD, WriteRefusal.PRICE_MISMATCH),  # в
        (_case_g, _GOOD, "sent"),  # г
        (_case_d, _GOOD, WriteRefusal.PRICE_MISMATCH),  # д
        (_case_e, _GOOD, WriteRefusal.PRICE_MISMATCH),  # е
        (_case_e, _TWO, "sent-two"),  # ж
        (_all(_case_e, _read_edit("PP1", 7)), _GOOD, WriteRefusal.STALE_LISTING),  # з
        (_all(_case_e, _title(None)), _GOOD, WriteRefusal.PRICE_MISMATCH),  # и
    ],
    ids=list("абвгдежзи"),
)
async def test_price_mismatch_rule(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    edit: Callable[[dict[str, Any], dict[str, Any]], None],
    prices: dict[str, int],
    outcome: WriteRefusal | str,
) -> None:
    """Т74 (Р32): цены сравниваются только у городов, которых нет в `city_prices`."""
    route = _write_route(mock_kaspi)
    row, state = _unspecified()
    edit(row, state)
    session = authenticated_session()
    if outcome in ("sent", "sent-two"):  # `WriteRefusal` тоже `str`: сравниваем значения
        await client.set_price(session, snapshot_from(state, row), prices)
        assert route.call_count == 1
        sent = json.loads(route.calls.last.request.content)
        if outcome == "sent":
            assert sent["cityPrices"] == [{"cityId": CITY, "value": 1650}]
        else:
            assert sent["cityPrices"] == [
                {"cityId": CITY, "value": 1650},
                {"cityId": "750000000", "value": 1695},
            ]
        return
    with pytest.raises(WriteRefusedError) as info:
        await client.set_price(session, snapshot_from(state, row), prices)
    assert info.value.reason is outcome
    assert route.call_count == 0


async def test_set_price_second_line_of_defence(
    client: KaspiClient, mock_kaspi: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т71: любое `Exception` из транспорта - неизвестный исход; отмена и отказы - нет."""
    calls: list[int] = []
    error: BaseException = RuntimeError("boom-marker")

    async def fake_send(self: Transport, *args: Any, **kwargs: Any) -> None:
        calls.append(1)
        raise error

    monkeypatch.setattr(Transport, "send", fake_send)
    snapshot = null_stock_snapshot()
    with pytest.raises(PriceWriteOutcomeUnknownError) as info:
        await client.set_price(authenticated_session(), snapshot, _GOOD)
    assert info.value.sku == SKU
    assert info.value.kind == "RuntimeError"
    assert "boom-marker" not in str(info.value)
    assert "boom-marker" not in repr(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client.set_price(authenticated_session(), snapshot, _GOOD)
    calls.clear()
    sample = captured_null_stock()
    sample["listRow"]["availabilities"][0]["preOrder"] = 3
    refused = snapshot_from(sample["editPrice"], sample["listRow"])
    with pytest.raises(WriteRefusedError) as control:
        await client.set_price(authenticated_session(), refused, _GOOD)
    assert control.value.reason is WriteRefusal.PREORDER
    assert calls == []


async def test_set_price_parse_failure_is_unknown_outcome(
    client: KaspiClient, mock_kaspi: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т72: не-`KaspiError` при разборе ответа записи - неизвестный исход без цепочки."""
    _write_route(mock_kaspi)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom-marker")

    monkeypatch.setattr("kaspisa.kaspi.cabinet._parse_cabinet", broken)
    with pytest.raises(PriceWriteOutcomeUnknownError) as info:
        await client.set_price(authenticated_session(), null_stock_snapshot(), _GOOD)
    assert info.value.kind == "RuntimeError"
    assert "boom-marker" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_write_refusal_values_are_stable() -> None:
    """Дополнено, не снято: значения причин отказа попадают в журнал и к продавцу - не меняются."""
    assert {r.name: r.value for r in WriteRefusal} == {
        "STOCK_COUNTED": "stock_counted",
        "UNVERIFIED_STORE_STATE": "unverified_store_state",
        "PRICE_MISMATCH": "price_mismatch",
        "PREORDER": "preorder",
        "ARCHIVED": "archived",
        "NO_STORES": "no_stores",
        "STALE_LISTING": "stale_listing",
        "STORES_CHANGED": "stores_changed",
        "MODEL_MISSING": "model_missing",
        "INVALID_PRICES": "invalid_prices",
        "SNAPSHOT_MISMATCH": "snapshot_mismatch",
    }


def test_partly_unknown_city_in_prices_is_invalid_prices() -> None:
    """Дополнено, не снято: один известный и один неизвестный город - `INVALID_PRICES`."""
    row, state = _unspecified()
    with pytest.raises(WriteRefusedError) as info:
        build_price_update(snapshot_from(state, row), {CITY: 1650, "111": 5})
    assert info.value.reason is WriteRefusal.INVALID_PRICES


def test_second_city_missing_in_listing_prices_is_price_mismatch() -> None:
    """Дополнено, не снято: город чтения вне `city_prices` без цены в списке - `PRICE_MISMATCH`."""
    row, state = _unspecified()
    _two_cities(row, state, list_price=1700, read_price=1700)
    row["cityPrices"] = [e for e in row["cityPrices"] if e["cityId"] != "750000000"]
    with pytest.raises(WriteRefusedError) as info:
        build_price_update(snapshot_from(state, row), _GOOD)
    assert info.value.reason is WriteRefusal.PRICE_MISMATCH


def _null_paths(value: Any, path: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    """Пути всех значений `null` в JSON, рекурсивно."""
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _null_paths(v, (*path, k))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _null_paths(v, (*path, i))]
    return []


async def test_body_keys_and_nulls_only_at_model(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """ADR 0019 (Т4): набор ключей равен снятому; `null` в JSON только у `model`.

    Ключа `stockCount` нет при остатке `None`; остаток 0 уходит целым числом.
    """
    route = _write_route(mock_kaspi)
    sample = captured_null_stock()
    session = authenticated_session()
    await client.set_price(
        session, snapshot_from(sample["editPrice"], sample["listRow"]), {CITY: 2998}
    )
    body = json.loads(route.calls.last.request.content)
    captured = sample["priceUpdateRequest"]
    assert set(body) == set(captured)
    assert [set(a) for a in body["availabilities"]] == [set(a) for a in captured["availabilities"]]
    assert [set(c) for c in body["cityPrices"]] == [set(c) for c in captured["cityPrices"]]
    assert _null_paths(body) == []

    await client.set_price(session, _null_title_snapshot(), {CITY: 605})
    body = json.loads(route.calls.last.request.content)
    assert set(body) == {"merchantUid", "availabilities", "cityPrices", "sku", "model"}
    assert _null_paths(body) == [("model",)]
    assert all("stockCount" not in a for a in body["availabilities"])

    row, state = _unspecified()
    await client.set_price(session, snapshot_from(state, row), _GOOD)
    body = json.loads(route.calls.last.request.content)
    assert _null_paths(body) == []
    by_store = {a["storeId"]: a for a in body["availabilities"]}
    assert by_store["90100003_PP3"]["stockCount"] == 0
    assert type(by_store["90100003_PP3"]["stockCount"]) is int
    assert "stockCount" not in by_store["90100003_PP1"]


def test_unspecified_stock_row_has_no_stock_count_key_even_when_all_unspecified() -> None:
    """Дополнено, не снято: остаток 0 передаётся как `0`, не как `None` и не пропускается."""
    row, state = _unspecified()
    body = build_price_update(snapshot_from(state, row), _GOOD).model_dump(
        by_alias=True, mode="json", exclude_none=True
    )
    by_store = {a["storeId"]: a for a in body["availabilities"]}
    assert by_store["90100003_PP3"]["stockCount"] == 0
    assert "stockCount" not in by_store["90100003_PP1"]


_MORE_CASES = [
    # read None: `yes`, остаток null, но признак `stockSpecified` выставлен
    (
        [_availability("PP3", "no", 0), _availability("PP1", "yes", None, stockSpecified=True)],
        ("PP1",),
    ),
    # read 0: `no` с остатком 5 и с остатком не указан
    ([_availability("PP3", "no", 5), _availability("PP1", "yes", None)], ("PP3",)),
    ([_availability("PP3", "no", None), _availability("PP1", "yes", None)], ("PP3",)),
    # read 0: точки нет или `no`, 0 - не аномалия (отправленная `yes`, 0 стала `no`, 0)
    ([_availability("PP1", "yes", None)], ()),
    ([_availability("PP3", "no", 0), _availability("PP1", "yes", None)], ()),
    # две строки одной точки: плохая строка не прячется за хорошей, в любом порядке
    (
        [
            _availability("PP3", "yes", 0),
            _availability("PP3", "no", 0),
            _availability("PP1", "yes", None),
        ],
        ("PP3",),
    ),
    (
        [
            _availability("PP3", "no", 0),
            _availability("PP1", "no", 0),
            _availability("PP1", "yes", None),
        ],
        ("PP1",),
    ),
    (
        [
            _availability("PP3", "no", 0),
            _availability("PP1", "yes", None),
            _availability("PP1", "no", 0),
        ],
        ("PP1",),
    ),
]
_MORE_IDS = [
    "none-specified-flag",
    "zero-no-5",
    "zero-no-null",
    "zero-absent",
    "zero-no-0",
    "dup-pp3",
    "dup-pp1-a",
    "dup-pp1-b",
]


@pytest.mark.parametrize(("availabilities", "anomalies"), _MORE_CASES, ids=_MORE_IDS)
async def test_verify_price_store_anomalies_more(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    availabilities: list[dict[str, Any]],
    anomalies: tuple[str, ...],
) -> None:
    """Т78 (дополнено, не снято): граничные состояния точек; строки собраны в тесте."""
    product, sample = _null_stock_product()
    row = _row_after_write(sample, availabilities=availabilities)
    mock_kaspi.post(url__startswith=LIST_URL).mock(return_value=_list_reply([row]))
    result = await client.verify_price(authenticated_session(), product, {CITY: 2998})
    assert result.applied is True
    assert result.store_anomalies == tuple(f"90100003_{name}" for name in anomalies)


def _counted_row_after_write(
    row: dict[str, Any], rows: list[dict[str, Any]], price: int = 480
) -> dict[str, Any]:
    """Строка списка «после записи» для фикстуры с указанным остатком (синтетика).

    PP3 - `no`, 0, как в снимке; точки PP1 и далее - `rows`; цена `price`.
    """
    after = copy.deepcopy(row)
    after["cityPrices"] = [{"cityId": CITY, "oldprice": 0, "value": price}]
    after["availabilities"] = [_availability("PP3", "no", 0), *rows]
    return after


def _anomalies(*names: str) -> tuple[str, ...]:
    return tuple(f"90100003_{name}" for name in names)


@pytest.mark.parametrize(
    ("listed", "read", "rows", "anomalies"),
    [
        # снимок «строка 100, чтение 94»
        (100, 94, [_availability("PP1", "yes", 100)], ()),  # строка ещё не обновилась
        (100, 94, [_availability("PP1", "yes", 94)], ()),
        (100, 94, [_availability("PP1", "yes", 90)], ()),
        (100, 94, [_availability("PP1", "yes", 0)], ()),
        (100, 94, [_availability("PP1", "yes", 101)], ()),  # Н4: рост не аномалия
        (100, 94, [_availability("PP1", "yes", 1000)], ()),
        (100, 94, [_availability("PP1", "yes", None)], ("PP1",)),  # Н14: сброс остатка
        (100, 94, [_availability("PP1", "no", 0)], ()),  # Н5
        (100, 94, [_availability("PP1", "no", 94)], ("PP1",)),
        (100, 94, [_availability("PP1", "no", None)], ("PP1",)),
        (100, 94, [], ("PP1",)),
        # снимок «строка 24, чтение 24»
        (24, 24, [_availability("PP1", "yes", 24)], ()),
        (24, 24, [_availability("PP1", "yes", 25)], ()),
    ],
    ids=[
        "100-still",
        "100-written",
        "100-sold",
        "100-zero",
        "100-up",
        "100-far-up",
        "100-lost-count",
        "100-no-0",
        "100-no-94",
        "100-no-null",
        "100-absent",
        "24-same",
        "24-up",
    ],
)
@pytest.mark.parametrize("settled", [False, True])
async def test_verify_price_store_anomalies_for_counted_read(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    settled: bool,
    listed: int | None,
    read: int,
    rows: list[dict[str, Any]],
    anomalies: tuple[str, ...],
) -> None:
    """Этап 23, Т7: точка с записанным остатком `N >= 1`, ожидания не зависят от `settled`.

    Допустимы «yes» с любым целым и «no, 0» (ADR 0037, Н4, Н5); аномалия - «yes, не указан»
    (Н14), «no» с остатком, точки нет. Синтетика по ADR 0036.
    """
    list_row, state = captured_stock_counted()
    _read_point(state, "PP1")["stock"] = read
    _list_point(list_row, "PP1").update(stockCount=listed, stockSpecified=listed is not None)
    product = snapshot_from(state, list_row)
    after = _counted_row_after_write(list_row, rows)
    mock_kaspi.post(url__startswith=LIST_URL).mock(return_value=_list_reply([after]))
    result = await client.verify_price(
        authenticated_session(), product, {CITY: 480}, settled=settled
    )
    assert result.applied is True
    assert result.store_anomalies == _anomalies(*anomalies)


async def test_counted_write_then_both_checks(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Этап 23, Т8: запись 94 по фикстуре, затем первая проверка и две поздние."""
    write = _write_route(mock_kaspi)
    list_row, state = captured_stock_counted()
    product = snapshot_from(state, list_row)
    session = authenticated_session()
    await client.set_price(session, product, {CITY: 480})
    sent = json.loads(write.calls.last.request.content)
    assert {a["storeId"]: a["stockCount"] for a in sent["availabilities"]} == {
        "90100003_PP3": 0,
        "90100003_PP1": 94,
    }
    route = mock_kaspi.post(url__startswith=LIST_URL)

    def reply(count: int | None) -> None:
        after = _counted_row_after_write(list_row, [_availability("PP1", "yes", count)])
        route.mock(return_value=_list_reply([after]))

    reply(100)  # строка ещё не показала записанное
    first = await client.verify_price(session, product, {CITY: 480})
    assert first.applied is True
    assert first.store_anomalies == ()

    reply(None)  # Kaspi стёр остаток
    erased = await client.verify_price(session, product, {CITY: 480}, settled=True)
    assert erased.store_anomalies == _anomalies("PP1")

    reply(94)
    settled = await client.verify_price(session, product, {CITY: 480}, settled=True)
    assert settled.store_anomalies == ()


@pytest.mark.parametrize(
    ("pp1", "pp2", "anomalies"),
    [
        (_availability("PP1", "yes", 94), _availability("PP2", "yes", None), ("PP2",)),
        (_availability("PP1", "yes", 100), _availability("PP2", "yes", 60), ()),
        (_availability("PP1", "no", 0), _availability("PP2", "yes", 41), ()),
        (_availability("PP1", "yes", 94), None, ("PP2",)),
        (_availability("PP1", "no", 5), _availability("PP2", "yes", 40), ("PP1",)),
    ],
    ids=["pp2-erased", "both-up", "pp1-off-pp2-up", "pp2-absent", "pp1-off-with-stock"],
)
@pytest.mark.parametrize("settled", [False, True])
async def test_store_anomalies_each_counted_point(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    settled: bool,
    pp1: dict[str, Any],
    pp2: dict[str, Any] | None,
    anomalies: tuple[str, ...],
) -> None:
    """Этап 23, Т16: правило применяется к каждой точке; PP1 (100/94), PP2 (50/40)."""
    list_row, state = captured_stock_counted()
    _with_pp2(list_row, state, 50, 40)
    product = snapshot_from(state, list_row)
    after = _counted_row_after_write(list_row, [pp1] if pp2 is None else [pp1, pp2])
    mock_kaspi.post(url__startswith=LIST_URL).mock(return_value=_list_reply([after]))
    result = await client.verify_price(
        authenticated_session(), product, {CITY: 480}, settled=settled
    )
    assert result.store_anomalies == _anomalies(*anomalies)


@pytest.mark.parametrize(
    ("settled", "price", "pp1", "listing_found", "applied", "anomalies"),
    [
        (True, 470, _availability("PP1", "yes", None), True, False, ("PP1",)),
        (True, 470, _availability("PP1", "yes", 94), True, False, ()),
        (True, 480, None, False, False, ()),
        (True, 480, _availability("PP1", "yes", 100), True, True, ()),
        (False, 470, _availability("PP1", "yes", None), True, False, ()),
    ],
    ids=["other-price-erased", "other-price-fine", "no-row", "same-price-100", "first-other-price"],
)
async def test_settled_verify_checks_points_at_any_price(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    settled: bool,
    price: int,
    pp1: dict[str, Any] | None,
    listing_found: bool,
    applied: bool,
    anomalies: tuple[str, ...],
) -> None:
    """Этап 23, Т17: поздняя проверка сверяет точки при любой цене, первая - только при своей цене.

    Строки нет - аномалий нет.
    """
    list_row, state = captured_stock_counted()
    product = snapshot_from(state, list_row)
    rows = [_counted_row_after_write(list_row, [pp1], price)] if pp1 is not None else []
    mock_kaspi.post(url__startswith=LIST_URL).mock(return_value=_list_reply(rows))
    result = await client.verify_price(
        authenticated_session(), product, {CITY: 480}, settled=settled
    )
    assert (result.listing is not None) is listing_found
    assert result.applied is applied
    assert result.mismatches == ({} if applied else {CITY: (480, None if pp1 is None else price)})
    assert result.store_anomalies == _anomalies(*anomalies)


def _pp1_rows(*rows: tuple[str, int | None]) -> Callable[[dict[str, Any]], None]:
    """Заменяет строки PP1 в списке на заданные (available, stockCount); PP3 не трогает."""

    def edit(row: dict[str, Any]) -> None:
        kept = [a for a in row["availabilities"] if a["storeId"] != "90100003_PP1"]
        row["availabilities"] = [*kept, *(_availability("PP1", a, c) for a, c in rows)]

    return edit


@pytest.mark.parametrize(
    ("edit_row", "read_pp1", "points"),
    [
        (_pp1_rows(("yes", None)), 94, ("90100003_PP1",)),
        (_pp1_rows(("yes", 100)), 94, ()),
        (_pp1_rows(("no", 0)), 94, ()),
        (_pp1_rows(), 94, ()),
        (_pp1_rows(("yes", 100), ("yes", None)), 94, ("90100003_PP1",)),
        (_pp1_rows(("yes", None)), None, ()),
        (_pp1_rows(("no", None)), 94, ()),
    ],
    ids=["a", "b", "v", "g", "d", "e", "z"],
)
def test_reset_points(
    edit_row: Callable[[dict[str, Any]], None], read_pp1: int | None, points: tuple[str, ...]
) -> None:
    """Этап 23, Т27: точка с остатком в чтении, которая в строке списка стала «не указан»."""
    from kaspisa.kaspi import reset_points

    list_row, state = captured_stock_counted()
    _read_point(state, "PP1")["stock"] = read_pp1
    edit_row(list_row)
    snapshot = snapshot_from(state, list_row)
    assert reset_points(snapshot.state, snapshot.listing) == points


def test_reset_points_ignores_zero_stock_points() -> None:
    """Этап 23, Т27 (ж): в чтении 0 - точка не «с остатком», даже если в списке «не указан»."""
    from kaspisa.kaspi import reset_points

    list_row, state = captured_stock_counted()
    _list_point(list_row, "PP3").update(available="yes", stockCount=None, stockSpecified=False)
    _pp1_rows(("yes", 100))(list_row)
    snapshot = snapshot_from(state, list_row)
    assert reset_points(snapshot.state, snapshot.listing) == ()


def test_reset_points_boundary_read_stock_one() -> None:
    """Этап 23, Т27: остаток чтения 1 - точка уже «с остатком» (граница `>= 1`)."""
    from kaspisa.kaspi import reset_points

    list_row, state = captured_stock_counted()
    _read_point(state, "PP1")["stock"] = 1
    _pp1_rows(("yes", None))(list_row)
    snapshot = snapshot_from(state, list_row)
    assert reset_points(snapshot.state, snapshot.listing) == ("90100003_PP1",)


def test_reset_points_are_sorted_for_many_points() -> None:
    """Этап 23, Т27: несколько сброшенных точек возвращаются по возрастанию кода."""
    from kaspisa.kaspi import reset_points

    list_row, state = captured_stock_counted()
    stores = ["PP9", "PP7", "PP5", "PP4", "PP2"]
    for store in stores:
        state["cities"][0]["points"].append({"id": f"90100003_{store}", "stock": 3})
        list_row["availabilities"].append(_availability(store, "yes", None))
    _pp1_rows(("yes", None))(list_row)
    snapshot = snapshot_from(state, list_row)
    expected = tuple(sorted(f"90100003_{s}" for s in ["PP1", *stores]))
    assert reset_points(snapshot.state, snapshot.listing) == expected


def test_reset_points_use_the_first_read_of_a_point() -> None:
    """Этап 23, Т27: точка в двух городах чтения берётся по первому упоминанию."""
    from kaspisa.kaspi import reset_points

    list_row, state = captured_stock_counted()
    second = copy.deepcopy(state["cities"][0])
    second["id"] = "710000001"
    _read_point({"cities": [second]}, "PP1")["stock"] = None
    state["cities"].append(second)
    _pp1_rows(("yes", None))(list_row)
    snapshot = snapshot_from(state, list_row)
    assert reset_points(snapshot.state, snapshot.listing) == ("90100003_PP1",)


def test_reset_points_ignores_null_stock_marked_as_specified() -> None:
    """Этап 23, Т27: `yes`, `stockCount: null`, но `stockSpecified: true` - не сброс."""
    from kaspisa.kaspi import reset_points

    list_row, state = captured_stock_counted()
    _pp1_rows(("yes", None))(list_row)
    _list_point(list_row, "PP1")["stockSpecified"] = True
    snapshot = snapshot_from(state, list_row)
    assert reset_points(snapshot.state, snapshot.listing) == ()
