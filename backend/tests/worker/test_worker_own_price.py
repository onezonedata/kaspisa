"""ADR 0015: своя цена - из городов строки списка, конкуренты - по городу сверки магазина."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    CITY_ID,
    Rig,
    fetch,
    journal,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, prepare

from kaspisa.worker.observation import own_price
from kaspisa.worker.writer import WriteOutcome

PP1 = "710000000"  # город склада (Астана); город сверки магазина - CITY_ID
SECOND = "620000000"


async def _observation(rig: Rig, p: Prepared) -> tuple[Any, ...]:
    rows = await fetch(
        rig,
        "SELECT position, competitors_count, first_place_price, last_checked_at "
        "FROM product WHERE id=:i",
        i=p.product_id,
    )
    return tuple(rows[0])


# --- правило своей цены (чистая функция) ---


@pytest.mark.parametrize(
    ("cities", "expected"),
    [
        ({CITY_ID: 100}, 100),  # город сверки - единственный город строки
        ({CITY_ID: 100, PP1: 120}, 100),  # город сверки есть - его цена, даже если цены разные
        ({PP1: 100}, 100),  # только город склада
        ({PP1: 100, SECOND: 100}, 100),  # несколько городов с одной ценой
        ({PP1: 100, SECOND: 120}, None),  # цены разные, города сверки нет
        ({}, None),  # городов нет
    ],
)
def test_own_price_rule(cities: dict[str, int], expected: int | None) -> None:
    assert own_price(cities, CITY_ID) == expected


# --- цикл ---


async def test_warehouse_city_price_competes_in_reconciliation_city(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Цена задана только в городе склада, в карточке города сверки есть дешевле и дороже."""
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {PP1: 100}
    p.client.competitor_price = 95
    p.client.extra_offers = {"dear": 130}
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.offers_cities == [CITY_ID]  # конкуренты - из карточки города сверки
    position, count, first, checked = await _observation(rig, p)
    assert (position, count, first) == (2, 2, 95)
    assert checked is not None
    # запись - во все города строки, города сверки среди них нет
    _, prices = p.client.sent[0]
    assert prices == {PP1: 94}
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "unconfirmed", None)]
    expected = await fetch(
        rig, "SELECT expected_prices FROM price_write WHERE product_id=:i", i=p.product_id
    )
    assert expected[0][0] == {PP1: 94}


async def test_warehouse_city_price_on_first_place_in_reconciliation_city(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Наш магазин в карточке города сверки дешевле всех: первое место по цене склада."""
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {PP1: 90}
    p.client.own_offer = 90
    p.client.competitor_price = 95
    p.client.extra_offers = {"dear": 130}
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.offers_cities == [CITY_ID]
    position, count, first, _ = await _observation(rig, p)
    assert (position, count, first) == (1, 2, 90)


async def test_own_offer_missing_in_reconciliation_city_card_changes_nothing(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """В город сверки магазин не доставляет: движок не меняет цену (ADR 0004)."""
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {PP1: 100}
    p.client.own_in_offers = False
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.methods() == ["find_product", "get_offers"]
    assert p.client.sent == []
    # решение «не менять» видно в журнале одной строкой (ADR 0020)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 100, "own_offer_missing", "no_change", None)
    ]
    position, count, first, _ = await _observation(rig, p)
    # карточка прочитана целиком, своего магазина в ней нет - позиции нет, «—» (ADR 0016)
    assert (position, count, first) == (None, 1, 95)


async def test_equal_prices_in_other_cities_are_written_to_all(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {PP1: 100, SECOND: 100}
    assert await run_one_slice(p.ctx, p.database)
    _, prices = p.client.sent[0]
    assert prices == {PP1: 94, SECOND: 94}


async def test_different_prices_without_reconciliation_city_skip_the_product(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {PP1: 100, SECOND: 120}
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.methods() == ["find_product"]
    assert await journal(rig, p.shop.shop_id) == []
    *_, checked = await _observation(rig, p)
    assert checked is not None


# --- сверка свежей строки перед записью ---


async def test_fresh_warehouse_price_changed_is_not_written(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {PP1: 97}  # решение считалось от 100
    result = await p.write(94, 100)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "price_changed")
    assert p.client.sent == []


async def test_fresh_warehouse_price_unchanged_is_written(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {PP1: 100}
    result = await p.write(94, 100)
    assert result.outcome is WriteOutcome.SENT
    _, prices = p.client.sent[0]
    assert prices == {PP1: 94}
