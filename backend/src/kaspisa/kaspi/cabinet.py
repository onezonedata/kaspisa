"""Запросы кабинета (mc.shop.kaspi.kz): чтение товара, список, запись и проверка цены."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from pydantic import BaseModel

from kaspisa.kaspi.errors import (
    KaspiError,
    KaspiTransportError,
    PriceWriteOutcomeUnknownError,
    RateLimitedError,
    ResponseFormatError,
    SessionExpiredError,
    SessionStateError,
    UnexpectedStatusError,
    WriteRefusal,
    WriteRefusedError,
)
from kaspisa.kaspi.models import (
    ListedAvailability,
    ListedProduct,
    PriceUpdateAvailability,
    PriceUpdateCityPrice,
    PriceUpdateRequest,
    PriceVerification,
    PriceWriteReceipt,
    ProductListPage,
    ProductSnapshot,
    ProductState,
)
from kaspisa.kaspi.session import KaspiSession, SessionPhase
from kaspisa.kaspi.transport import Reply, Transport, parse_body, parse_retry_after

MC_URL = "https://mc.shop.kaspi.kz"
LIST_PAGE_SIZE = 100
MAX_LIST_PAGES = 200
CABINET_HEADERS = {
    "x-auth-version": "3",
    "Origin": "https://kaspi.kz",
    "Referer": "https://kaspi.kz/",
}

# Сроки подтверждения записи цены - умолчания по одному грубому измерению (вопрос пользователю В7).
# Клиент без состояния и часов: ждёт, держит запрет на запись и считает время обработчик (этап 5).
PRICE_VERIFY_FIRST_CHECK_SECONDS = 30  # цена появилась в строке списка через 22-27 с
PRICE_VERIFY_DEADLINE_SECONDS = 300  # вдвое больше верхней оценки появления цены в edit-price
PRICE_WRITE_HOLD_SECONDS = 600  # дольше запрет на новую запись sku не держится (ADR 0007, В7)


def is_session_expired(status_code: int, headers: httpx.Headers) -> bool:
    """Признак истёкшей сессии кабинета: 401 или перенаправление.

    401 проверено 2026-11-14 (`GET /s/m`, список, чтение товара, счётчики: пустое тело).
    Перенаправление (3xx) - предположение: такой ответ не снят. 403 не признак: он приходит
    и при действующей сессии (чужой `origin`), поэтому это `UnexpectedStatusError`.
    """
    return status_code == 401 or 300 <= status_code <= 399


def _require_authenticated(session: KaspiSession) -> None:
    if session.phase is not SessionPhase.AUTHENTICATED:
        raise SessionStateError("запрос кабинета требует сессию в фазе authenticated")


def _check_cabinet_reply(reply: Reply, operation: str) -> None:
    """Общие проверки ответа кабинета: 429, истёкшая сессия, статус вне 2xx."""
    if reply.status_code == 429:
        raise RateLimitedError(operation, parse_retry_after(reply))
    if is_session_expired(reply.status_code, reply.headers):
        raise SessionExpiredError(operation)
    if not 200 <= reply.status_code <= 299:
        raise UnexpectedStatusError(operation, reply.status_code)


def _parse_cabinet[M: BaseModel](reply: Reply, operation: str, model: type[M]) -> M:
    _check_cabinet_reply(reply, operation)
    return parse_body(reply, operation, model)


async def _list_page(
    transport: Transport,
    session: KaspiSession,
    merchant_uid: str,
    operation: str,
    body: dict[str, object],
    start: int,
) -> ProductListPage:
    """Одна страница списка товаров кабинета с заданным телом запроса."""
    reply = await transport.send(
        "POST",
        f"{MC_URL}/bff/offer-view/v2/list",
        operation=operation,
        retry=True,
        merchant_uid=merchant_uid,
        session=session,
        headers=CABINET_HEADERS,
        params={"m": merchant_uid, "l": LIST_PAGE_SIZE, "start": start, "sortBy": "DATE_DESC"},
        json=body,
    )
    return _parse_cabinet(reply, operation, ProductListPage)


async def list_products(
    transport: Transport, session: KaspiSession, merchant_uid: str
) -> list[ListedProduct]:
    """Все товары кабинета в продаже (тело `{"available": true}`).

    Обход кончается на пустой странице, на странице короче `LIST_PAGE_SIZE` или при
    `start >= total`: запрос за концом выдачи не снят, а `total` включает архив.
    """
    _require_authenticated(session)
    collected: list[ListedProduct] = []
    start = 0
    for _ in range(MAX_LIST_PAGES):
        page = await _list_page(
            transport, session, merchant_uid, "list_products", {"available": True}, start
        )
        collected.extend(page.data)
        start += LIST_PAGE_SIZE
        if len(page.data) < LIST_PAGE_SIZE or start >= page.total:
            return collected
    raise ResponseFormatError("list_products", ("слишком много страниц",))


async def find_product(
    transport: Transport, session: KaspiSession, merchant_uid: str, sku: str
) -> ListedProduct | None:
    """Строка списка одного товара: один запрос с поиском по `sku`, первая страница.

    Поиск идёт по артикулу или названию и может вернуть чужие строки, поэтому берутся только
    строки с `sku`, равным запрошенному. Нет таких - `None`; больше одной - ошибка формата.
    """
    if not sku:
        raise ValueError("sku не должен быть пустым")
    _require_authenticated(session)
    page = await _list_page(
        transport,
        session,
        merchant_uid,
        "find_product",
        {"searchTerm": sku, "available": True},
        0,
    )
    rows = [row for row in page.data if row.sku == sku]
    if len(rows) > 1:
        raise ResponseFormatError("find_product", ("data",))
    return rows[0] if rows else None


async def get_product(
    transport: Transport, session: KaspiSession, merchant_uid: str, sku: str
) -> ProductState:
    """Свежее чтение товара; `sku` ответа обязан совпасть с запрошенным."""
    _require_authenticated(session)
    reply = await transport.send(
        "GET",
        f"{MC_URL}/bff/offer-view/edit-price",
        operation="get_product",
        retry=True,
        merchant_uid=merchant_uid,
        session=session,
        headers=CABINET_HEADERS,
        params={"m": merchant_uid, "s": sku},
    )
    state = _parse_cabinet(reply, "get_product", ProductState)
    if state.sku != sku:
        raise ResponseFormatError("get_product", ("sku",))
    return state


def _unverified_store_state(availability: ListedAvailability) -> bool:
    """Состояние точки списка, которое снятие не подтвердило (запись запрещена)."""
    count = availability.stock_count
    return (
        (availability.available == "no" and count != 0)
        or (availability.available == "yes" and count == 0)  # вопрос пользователю В6
        or availability.stock_specified != (count is not None)
    )


def _stores_changed(
    state: ProductState, listing: ListedProduct, read_stocks: dict[str, int | None]
) -> bool:
    """Точка списка повторена или не найдена в чтении; общая точка городов с разным остатком."""
    listed_ids = [a.store_id for a in listing.availabilities]
    if len(set(listed_ids)) != len(listed_ids) or not set(listed_ids) <= set(read_stocks):
        return True
    seen: dict[str, int | None] = {}
    for city in state.cities:
        for point in city.points:
            if point.id in seen and seen[point.id] != point.stock:
                return True
            seen[point.id] = point.stock
    return False


def _price_mismatch(
    state: ProductState, listing: ListedProduct, city_prices: Mapping[str, int]
) -> bool:
    """Р32: охраняются только цены, которые попадут в тело из чтения (города вне `city_prices`)."""
    read_prices = {city.id: city.price for city in state.cities}
    listed_ids = [p.city_id for p in listing.city_prices]
    listed_prices = {p.city_id: p.value for p in listing.city_prices}
    if len(set(listed_ids)) != len(listed_ids) or not set(listed_ids) <= set(read_prices):
        return True
    return any(
        listed_prices.get(city_id) != price
        for city_id, price in read_prices.items()
        if city_id not in city_prices
    )


def _stock_consistent(listed: ListedAvailability | None, read: int | None) -> bool:
    """Остаток точки в чтении согласуется со строкой списка (шаг `STALE_LISTING`, ADR 0037).

    Точка с указанным остатком (`yes`, `L >= 1`): годится любое `R >= 1` (ADR 0037, Н1, Н3),
    `L` служит только признаком «остаток указан». Остальные точки (в том числе отсутствующая
    в строке, `L = 0`): `R == L`.
    """
    listed_count = 0 if listed is None else listed.stock_count
    if (
        listed is not None
        and listed.available == "yes"
        and listed_count is not None
        and listed_count >= 1
    ):
        return read is not None and read >= 1
    return read == listed_count


def build_price_update(
    product: ProductSnapshot, city_prices: Mapping[str, int]
) -> PriceUpdateRequest:
    """Тело записи цены из свежего чтения; отказы проверяются до запроса.

    Чистая функция. Тело повторяет запрос кабинета (ADR 0006, ADR 0037): точки - все точки
    свежего чтения в порядке первого появления, у каждой `available: "yes"`; `stockCount` -
    остаток чтения, включая числа больше 0 и 0; неуказанный остаток (`None`) не передаётся
    совсем. Строка списка тело не задаёт, а проверяет: всё, что не подтверждено снятием, -
    отказ. Порядок отказов - Д1.15.2.
    `model` - `title` чтения как есть, включая `None`, `""` и пробелы (ADR 0019).
    """
    state, listing = product.state, product.listing
    sku = state.sku
    if state.sku != listing.sku:
        raise WriteRefusedError(WriteRefusal.SNAPSHOT_MISMATCH, sku)
    if listing.archived_at is not None:
        raise WriteRefusedError(WriteRefusal.ARCHIVED, sku)
    read_stocks: dict[str, int | None] = {}
    for city in state.cities:
        for point in city.points:
            read_stocks.setdefault(point.id, point.stock)
    if not read_stocks or not listing.availabilities:
        raise WriteRefusedError(WriteRefusal.NO_STORES, sku)
    if any(_unverified_store_state(a) for a in listing.availabilities):
        raise WriteRefusedError(WriteRefusal.UNVERIFIED_STORE_STATE, sku)
    if any(a.pre_order != 0 for a in listing.availabilities):
        raise WriteRefusedError(WriteRefusal.PREORDER, sku)
    if _stores_changed(state, listing, read_stocks):
        raise WriteRefusedError(WriteRefusal.STORES_CHANGED, sku)
    listed_stocks = {a.store_id: a for a in listing.availabilities}
    if not all(
        _stock_consistent(listed_stocks.get(store), stock) for store, stock in read_stocks.items()
    ):
        raise WriteRefusedError(WriteRefusal.STALE_LISTING, sku)
    if _price_mismatch(state, listing, city_prices):
        raise WriteRefusedError(WriteRefusal.PRICE_MISMATCH, sku)
    known_cities = {city.id for city in state.cities}
    if (
        not city_prices
        or not set(city_prices) <= known_cities
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in city_prices.values()
        )
    ):
        raise WriteRefusedError(WriteRefusal.INVALID_PRICES, sku)
    return PriceUpdateRequest(
        merchant_uid=listing.merchant_uid,
        availabilities=[
            PriceUpdateAvailability(available="yes", store_id=store, stock_count=stock)
            for store, stock in read_stocks.items()
        ],
        city_prices=[
            PriceUpdateCityPrice(city_id=city.id, value=city_prices.get(city.id, city.price))
            for city in state.cities
        ],
        sku=state.sku,
        model=state.title,
    )


async def set_price(
    transport: Transport,
    session: KaspiSession,
    product: ProductSnapshot,
    city_prices: Mapping[str, int],
) -> PriceWriteReceipt:
    """Одна попытка записи цены. Любой сбой после отправки - неизвестный исход."""
    _require_authenticated(session)
    body = build_price_update(product, city_prices)
    sku = body.sku
    try:
        reply = await transport.send(
            "POST",
            f"{MC_URL}/pricefeed/upload/merchant/process",
            operation="set_price",
            retry=False,
            merchant_uid=body.merchant_uid,
            session=session,
            headers=CABINET_HEADERS,
            json=body.to_json_body(),
        )
        if reply.status_code >= 500:
            failure = f"status_{reply.status_code}"
        else:
            return _parse_cabinet(reply, "set_price", PriceWriteReceipt)
    except KaspiTransportError as exc:
        failure = exc.kind
    except KaspiError:
        raise
    except Exception as exc:  # любой иной сбой после начала отправки - исход неизвестен
        failure = type(exc).__name__
    raise PriceWriteOutcomeUnknownError(sku, failure)


def _store_anomalies(state: ProductState, listing: ListedProduct) -> tuple[str, ...]:
    """Точки строки списка, не согласующиеся с чтением снимка записи (Р31, ADR 0037).

    Остаток чтения `None` (не указан) ждёт в списке `yes`, не указан; 0 - точки нет или
    `no`, 0; больше 0 (`N`) - `yes` с любым целым остатком или `no`, 0 (Н4, Н5: рост остатка
    и выключение при нуле не аномалия). Аномалия при `N >= 1`: `yes`, не указан (сброс
    остатка, Н14), `no` с остатком не 0, точки нет. Лишняя точка списка и предзаказ - аномалия.
    """
    read_stocks: dict[str, int | None] = {}
    for city in state.cities:
        for point in city.points:
            read_stocks.setdefault(point.id, point.stock)
    listed: dict[str, list[ListedAvailability]] = {}
    for availability in listing.availabilities:
        listed.setdefault(availability.store_id, []).append(availability)
    anomalies: set[str] = set()
    for store, stock in read_stocks.items():
        rows = listed.get(store, [])
        if stock is None:
            bad = not rows or any(
                (a.available, a.stock_count, a.stock_specified) != ("yes", None, False)
                for a in rows
            )
        elif stock == 0:
            bad = any((a.available, a.stock_count) != ("no", 0) for a in rows)
        else:
            bad = not rows or any(
                not (a.available == "yes" and a.stock_count is not None)
                and (a.available, a.stock_count) != ("no", 0)
                for a in rows
            )
        if bad:
            anomalies.add(store)
    for availability in listing.availabilities:
        if availability.store_id not in read_stocks or availability.pre_order != 0:
            anomalies.add(availability.store_id)
    return tuple(sorted(anomalies))


def reset_points(state: ProductState, listing: ListedProduct) -> tuple[str, ...]:
    """Точки, остаток которых был указан в чтении снимка, а в строке списка стал «не указан».

    Первая встреченная точка чтения (как в `_store_anomalies`) должна иметь остаток
    `>= 1`; в строке списка - хотя бы одна запись точки `yes`, без числа, не указан (ADR 0037).
    """
    read_stocks: dict[str, int | None] = {}
    for city in state.cities:
        for point in city.points:
            read_stocks.setdefault(point.id, point.stock)
    reset = {
        a.store_id
        for a in listing.availabilities
        if (read_stocks.get(a.store_id) or 0) >= 1
        and (a.available, a.stock_count, a.stock_specified) == ("yes", None, False)
    }
    return tuple(sorted(reset))


async def verify_price(
    transport: Transport,
    session: KaspiSession,
    product: ProductSnapshot,
    expected: Mapping[str, int],
    *,
    settled: bool = False,
) -> PriceVerification:
    """Одна проверка записи по строке списка (`find_product`); ожидания клиент не делает.

    `product` - снимок, по которому шла запись. Строка списка обновляется за 22-27 с,
    `edit-price` - за 1,5-4 мин, поэтому читается список. Точки найденной строки сверяются
    с чтением снимка: расхождения идут в `store_anomalies`. По умолчанию точки сверяются,
    только если цены совпали; `settled=True` - точки сверяются при любых ценах (поздняя
    проверка, ADR 0037).
    """
    if not expected:
        raise ValueError("expected не должен быть пустым")
    listing = await find_product(
        transport, session, product.listing.merchant_uid, product.state.sku
    )
    listed_prices = (
        {} if listing is None else {price.city_id: price.value for price in listing.city_prices}
    )
    mismatches = {
        city_id: (price, listed_prices.get(city_id))
        for city_id, price in expected.items()
        if listed_prices.get(city_id) != price
    }
    applied = not mismatches
    anomalies = (
        _store_anomalies(product.state, listing)
        if listing is not None and (applied or settled)
        else ()
    )
    return PriceVerification(
        applied=applied, listing=listing, mismatches=mismatches, store_anomalies=anomalies
    )
