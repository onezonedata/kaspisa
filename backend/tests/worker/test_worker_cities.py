"""Н12 (ADR 0011, ADR 0015): несколько городов товара - одна цена во всех городах строки."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from datetime import datetime, timedelta

from redis.asyncio import Redis
from worker_testing import (
    CITY_ID,
    NOW,
    Rig,
    fetch,
    journal,
    not_applied,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, hold_lease, prepare

from kaspisa.worker.writer import WriteOutcome

OTHER_CITY = "999"


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


async def _aligned_flags(rig: Rig, p: Prepared) -> list[bool]:
    rows = await fetch(
        rig,
        "SELECT cities_aligned FROM price_change WHERE shop_id=:i ORDER BY id",
        i=p.shop.shop_id,
    )
    return [row[0] for row in rows]


async def test_different_city_prices_are_aligned_and_marked(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.extra_cities = {OTHER_CITY: 120}
    assert await run_one_slice(p.ctx, p.database)
    _, prices = p.client.sent[0]
    assert prices == {CITY_ID: 94, OTHER_CITY: 94}
    # движок получил цену города сверки (100), а не соседнего
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "unconfirmed", None)]
    assert await _aligned_flags(rig, p) == [True]
    expected = await fetch(
        rig, "SELECT expected_prices FROM price_write WHERE product_id=:i", i=p.product_id
    )
    assert expected[0][0] == {CITY_ID: 94, OTHER_CITY: 94}


async def test_equal_city_prices_are_not_marked(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.extra_cities = {OTHER_CITY: 100}
    assert await run_one_slice(p.ctx, p.database)
    _, prices = p.client.sent[0]
    assert prices == {CITY_ID: 94, OTHER_CITY: 94}
    assert await _aligned_flags(rig, p) == [False]


async def test_dry_run_marks_aligned_cities_too(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.extra_cities = {OTHER_CITY: 120}
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.sent == []
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "dry_run", None)]
    assert await _aligned_flags(rig, p) == [True]


async def test_missing_reconciliation_city_uses_the_row_price(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """ADR 0015: города сверки нет в строке - своя цена из городов строки, запись туда же."""
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {OTHER_CITY: 100}
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.offers_cities == [CITY_ID]
    _, prices = p.client.sent[0]
    assert prices == {OTHER_CITY: 94}
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "unconfirmed", None)]


async def test_fresh_read_with_changed_reconciliation_price_is_not_written(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.extra_cities = {OTHER_CITY: 100}
    p.client.price = 97  # свежее чтение: цена города сверки уже не 100
    result = await p.write(94, 100)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "price_changed")
    assert p.client.sent == []


async def test_fresh_read_with_unknown_own_price_is_not_written(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """ADR 0015: в свежей строке цены разные и города сверки нет - своя цена неизвестна."""
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {OTHER_CITY: 100, "998": 120}
    result = await p.write(94, 100)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "own_price_unknown")
    assert p.client.sent == []


async def test_confirmation_waits_for_every_city(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.confirm import verify_open_writes

    p = await prepare(rig, redis_client, redis_prefix)
    p.client.extra_cities = {OTHER_CITY: 120}
    assert (await p.write(94, 100)).outcome is WriteOutcome.SENT
    await hold_lease(rig, p.shop, "w1", at(100_000))
    p.client.verify_results = [not_applied()]  # одна из цен городов ещё старая
    p.clock.now = at(30)
    await verify_open_writes(p.sctx)
    _, expected = p.client.verified[0]
    assert expected == {CITY_ID: 94, OTHER_CITY: 94}
    status = await fetch(rig, "SELECT status FROM price_write WHERE product_id=:i", i=p.product_id)
    assert status[0][0] != "confirmed"
    assert (await journal(rig, p.shop.shop_id))[0][3] == "unconfirmed"
