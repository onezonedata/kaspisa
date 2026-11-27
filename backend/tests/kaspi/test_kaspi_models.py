"""Модели ответов Kaspi: разбор примеров и отказ на изменившемся формате (Т1-Т7)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from kaspi_testing import (
    captured_merchants,
    captured_null_stock,
    captured_point_off,
    load_fixture,
    point_off_unspecified,
)

from kaspisa.kaspi import ResponseFormatError
from kaspisa.kaspi.models import (
    ListedProduct,
    MerchantsResponse,
    Offer,
    OffersPage,
    PriceUpdateRequest,
    PriceWriteReceipt,
    ProductListPage,
    ProductState,
    parse_model,
)


def test_offers_page_parses_fixture() -> None:
    """Т1: 7 продавцов, `merchantId` «Magazin5» остаётся строкой, цены — int."""
    page = parse_model(OffersPage, load_fixture("offers.json"), "get_offers")
    assert len(page.offers) == 7
    assert page.total == 7
    textual = next(o for o in page.offers if o.merchant_name == "МАГАЗИН 5")
    assert textual.merchant_id == "Magazin5"
    assert all(type(o.price) is int for o in page.offers)


def test_offer_full_format_takes_only_needed_fields() -> None:
    """Т2: полный формат; лишние поля игнорируются."""
    raw = load_fixture("offer-full-sample.json")
    offers = [parse_model(Offer, item, "get_offers") for item in raw]
    assert offers[0].merchant_id == "90100003"
    assert offers[0].merchant_name == "Магазин 1"
    assert offers[0].price == 2639
    assert offers[0].preorder == 0
    assert not hasattr(offers[0], "merchant_rating")


def test_product_list_page_parses_fixture() -> None:
    """Т3: два товара, остаток не указан, предзаказа нет."""
    page = parse_model(ProductListPage, load_fixture("product-list.json"), "list_products")
    assert len(page.data) == 2
    assert page.total == 998
    first = page.data[0]
    assert first.archived_at is None
    assert first.merchant_uid == "90100003"
    for availability in first.availabilities:
        assert availability.stock_count is None
        assert availability.stock_specified is False
        assert availability.pre_order == 0
        assert availability.available == "yes"
    assert first.city_prices[0].city_id == "710000000"
    assert first.city_prices[0].value == 931


def test_product_state_parses_fixture() -> None:
    """Т4: город, цена, склады и название."""
    state = parse_model(ProductState, load_fixture("edit-price.json"), "get_product")
    assert state.sku == "900000005_900000024"
    assert state.title is not None
    assert len(state.cities) == 1
    city = state.cities[0]
    assert city.id == "710000000"
    assert city.price == 2654
    assert [(p.id, p.stock) for p in city.points] == [
        ("90100003_PP3", 0),
        ("90100003_PP1", 9),
    ]


def test_price_write_receipt_parses_fixture() -> None:
    """Т5: номер задания."""
    receipt = parse_model(
        PriceWriteReceipt, load_fixture("price-update-response.json"), "set_price"
    )
    assert receipt.job_id == "9f0000000000000000000005"


def _drop(data: Any, path: list[str | int]) -> Any:
    copied = copy.deepcopy(data)
    node = copied
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]
    return copied


MISSING_CASES: list[tuple[Any, str, list[str | int], str]] = [
    (OffersPage, "offers.json", ["offers"], "offers"),
    (OffersPage, "offers.json", ["total"], "total"),
    (OffersPage, "offers.json", ["offers", 0, "merchantId"], "merchantId"),
    (OffersPage, "offers.json", ["offers", 0, "price"], "price"),
    (ProductListPage, "product-list.json", ["data", 0, "sku"], "sku"),
    (ProductListPage, "product-list.json", ["data", 0, "availabilities"], "availabilities"),
    (
        ProductListPage,
        "product-list.json",
        ["data", 0, "availabilities", 0, "storeId"],
        "storeId",
    ),
    (
        ProductListPage,
        "product-list.json",
        ["data", 0, "availabilities", 0, "stockCount"],
        "stockCount",
    ),
    (ProductState, "edit-price.json", ["sku"], "sku"),
    (ProductState, "edit-price.json", ["cities"], "cities"),
    (ProductState, "edit-price.json", ["cities", 0, "price"], "price"),
    (ProductState, "edit-price.json", ["cities", 0, "points", 0, "id"], "id"),
    (PriceWriteReceipt, "price-update-response.json", ["id"], "id"),
]


@pytest.mark.parametrize(("model", "fixture", "path", "field"), MISSING_CASES)
def test_missing_required_field_gives_format_error(
    model: Any, fixture: str, path: list[str | int], field: str
) -> None:
    """Т6: нет обязательного поля - `ResponseFormatError` с путём поля, без значений."""
    data = _drop(load_fixture(fixture), path)
    with pytest.raises(ResponseFormatError) as info:
        parse_model(model, data, "op")
    assert any(field in item for item in info.value.fields)
    text = str(info.value) + repr(info.value)
    assert "Магазин 1" not in text
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def _with(data: Any, path: list[str | int], value: Any) -> Any:
    copied = copy.deepcopy(data)
    node = copied
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return copied


WRONG_TYPE_CASES: list[tuple[Any, str, list[str | int], Any]] = [
    (OffersPage, "offers.json", ["offers", 0, "price"], "2643"),
    (OffersPage, "offers.json", ["offers", 0, "price"], 2643.5),
    (OffersPage, "offers.json", ["offers", 0, "price"], float("nan")),
    (OffersPage, "offers.json", ["offers", 0, "merchantId"], 90100003),
    (ProductListPage, "product-list.json", ["data", 0, "availabilities", 0, "available"], "maybe"),
]


@pytest.mark.parametrize(("model", "fixture", "path", "value"), WRONG_TYPE_CASES)
def test_wrong_type_gives_format_error(
    model: Any, fixture: str, path: list[str | int], value: Any
) -> None:
    """Т7: неверный тип не приводится молча."""
    data = _with(load_fixture(fixture), path, value)
    with pytest.raises(ResponseFormatError):
        parse_model(model, data, "op")


def test_captured_samples_parse() -> None:
    """Т75 (без `PriceUpdateRequest`): магазины, строка и чтение НО, строка ВТ и ВТн."""
    merchants = parse_model(MerchantsResponse, captured_merchants().json(), "login")
    assert [m.uid for m in merchants.merchants] == ["90100003"]
    assert len(merchants.merchants[0].roles) == 11
    assert "MANAGE_OFFERS" in merchants.merchants[0].roles

    sample = captured_null_stock()
    row = parse_model(ListedProduct, sample["listRow"], "list_products")
    points = [
        (a.store_id, a.available, a.stock_count, a.stock_specified, a.pre_order)
        for a in row.availabilities
    ]
    assert points == [("90100003_PP1", "yes", None, False, 0)]
    assert row.city_prices[0].value == 2998
    state = parse_model(ProductState, sample["editPrice"], "get_product")
    assert [(p.id, p.stock) for p in state.cities[0].points] == [
        ("90100003_PP3", 0),
        ("90100003_PP1", None),
    ]
    assert state.cities[0].price == 2998

    off_row, off_state = captured_point_off()
    off = parse_model(ListedProduct, off_row, "list_products")
    pp3 = next(a for a in off.availabilities if a.store_id == "90100003_PP3")
    assert (pp3.available, pp3.stock_count) == ("no", 0)
    parse_model(ProductState, off_state, "get_product")

    unspecified_row, unspecified_state = point_off_unspecified()
    pp1 = next(
        a
        for a in parse_model(ListedProduct, unspecified_row, "list_products").availabilities
        if a.store_id == "90100003_PP1"
    )
    assert (pp1.available, pp1.stock_count, pp1.stock_specified) == ("yes", None, False)
    reads = {
        p.id: p.stock
        for p in parse_model(ProductState, unspecified_state, "get_product").cities[0].points
    }
    assert reads == {"90100003_PP3": 0, "90100003_PP1": None}


def test_price_update_request_roundtrip_without_stock_count() -> None:
    """Т75: снятый запрос НО разбирается; выгрузка без `None` равна исходному JSON."""
    raw = captured_null_stock()["priceUpdateRequest"]
    request = parse_model(PriceUpdateRequest, raw, "set_price")
    assert request.availabilities[1].stock_count is None
    assert request.model_dump(by_alias=True, mode="json", exclude_none=True) == raw


@pytest.mark.parametrize(
    ("model", "fixture", "path"),
    [
        (ProductState, "edit-price.json", ["cities", 0, "points", 0, "stock"]),
        (ProductListPage, "product-list.json", ["data", 0, "availabilities", 0, "preOrder"]),
        (ProductListPage, "product-list.json", ["data", 0, "availabilities", 0, "stockSpecified"]),
    ],
    ids=["read-stock", "listing-preorder", "listing-stock-specified"],
)
def test_absent_stock_fields_are_not_defaulted(
    model: Any, fixture: str, path: list[str | int]
) -> None:
    """Дополнено, не снято: нет ключа остатка или предзаказа - ошибка формата, а не молчаливый 0.

    Иначе пропавший `stock` читался бы как остаток 0 и запись выставила бы `stockCount: 0`.
    """
    with pytest.raises(ResponseFormatError):
        parse_model(model, _drop(load_fixture(fixture), path), "op")


def test_product_state_without_cities_is_format_error() -> None:
    """Дополнено, не снято: чтение без городов - ошибка формата."""
    data = _with(load_fixture("edit-price.json"), ["cities"], [])
    with pytest.raises(ResponseFormatError):
        parse_model(ProductState, data, "get_product")


@pytest.mark.parametrize(
    "path",
    [
        ["merchantUid"],
        ["sku"],
        ["availabilities"],
        ["cityPrices"],
        ["availabilities", 0, "available"],
        ["availabilities", 0, "storeId"],
        ["cityPrices", 0, "cityId"],
        ["cityPrices", 0, "value"],
    ],
)
def test_price_update_request_rejects_none_everywhere_but_stock_count(
    path: list[str | int],
) -> None:
    """Дополнено, не снято: `None` законен только у `stockCount` и `model` (ADR 0019)."""
    raw = captured_null_stock()["priceUpdateRequest"]
    with pytest.raises(ResponseFormatError):
        parse_model(PriceUpdateRequest, _with(raw, path, None), "set_price")
    parse_model(PriceUpdateRequest, _with(raw, ["availabilities", 0, "stockCount"], None), "x")
    parse_model(PriceUpdateRequest, _with(raw, ["model"], None), "x")


def test_price_update_request_model_is_required() -> None:
    """ADR 0019: ключ `model` обязателен, значения по умолчанию нет."""
    raw = dict(captured_null_stock()["priceUpdateRequest"])
    del raw["model"]
    with pytest.raises(ResponseFormatError):
        parse_model(PriceUpdateRequest, raw, "set_price")


def test_negative_stock_is_format_error_in_read_and_listing() -> None:
    """Н6: отрицательный остаток в чтении или строке списка - `ResponseFormatError` при разборе."""
    sample = captured_null_stock()
    read = copy.deepcopy(sample["editPrice"])
    read["cities"][0]["points"][0]["stock"] = -2
    with pytest.raises(ResponseFormatError) as read_error:
        parse_model(ProductState, read, "get_product")
    assert "cities[0].points[0].stock" in read_error.value.fields
    row = copy.deepcopy(sample["listRow"])
    row["availabilities"][0]["stockCount"] = -2
    with pytest.raises(ResponseFormatError) as row_error:
        parse_model(ListedProduct, row, "list_products")
    assert "availabilities[0].stockCount" in row_error.value.fields
