"""Т76: причины цикла не затирают отказ записи."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    Rig,
    execute,
    fetch,
    journal,
    listed_product,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, prepare

from kaspisa.kaspi import WriteRefusal, WriteRefusedError


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


async def _refusal(rig: Rig, p: Prepared) -> tuple[object, ...]:
    rows = await fetch(
        rig,
        "SELECT refusal_reason, refusal_fingerprint FROM product WHERE id=:i",
        i=p.product_id,
    )
    return rows[0]


async def _slice_at(p: Prepared, seconds: int) -> None:
    p.clock.now = at(seconds)
    assert await run_one_slice(p.ctx, p.database, at(seconds))


async def test_cycle_reasons_do_not_overwrite_a_write_refusal(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.PREORDER, p.sku)
    await _slice_at(p, 0)
    reason, fingerprint = await _refusal(rig, p)
    assert reason == "preorder" and fingerprint is not None
    assert p.client.methods().count("set_price") == 1
    # своя цена временно неизвестна: цены городов разные, города сверки нет (own_price_unknown)
    row = p.listing(100)
    other = row.city_prices[0].model_copy(update={"city_id": "999", "value": 120})
    another = row.city_prices[0].model_copy(update={"city_id": "998", "value": 130})
    diverged = row.model_copy(update={"city_prices": [other, another]})
    p.client.find_results = [diverged]
    await _slice_at(p, 61)
    assert await _refusal(rig, p) == (reason, fingerprint)
    # товар на время пропал из списка
    p.client.find_results = [None]
    await _slice_at(p, 122)
    assert await _refusal(rig, p) == (reason, fingerprint)
    # всё вернулось с прежним отпечатком: попытки записи нет
    calls = len(p.client.calls)
    await _slice_at(p, 183)
    assert p.client.calls[calls:] == [("find_product", p.sku)]
    assert await _refusal(rig, p) == (reason, fingerprint)
    assert p.client.methods().count("set_price") == 1
    failed = [r for r in await journal(rig, p.shop.shop_id) if r[3] == "failed"]
    assert [r[4] for r in failed] == ["preorder"]


async def _full_refusal(rig: Rig, p: Prepared) -> tuple[object, ...]:
    rows = await fetch(
        rig,
        "SELECT refusal_reason, refusal_since, refusal_fingerprint, retry_at "
        "FROM product WHERE id=:i",
        i=p.product_id,
    )
    return rows[0]


@pytest.mark.parametrize("case", ["same_row", "changed_row", "engine_keeps_price"])
async def test_old_stock_counted_refusal_is_cleared(
    case: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Этап 23, Т10: старый отказ `stock_counted` снимается на первом круге (ADR 0037, п. 4)."""
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STOCK_COUNTED, p.sku)
    await _slice_at(p, 0)
    reason, _since, fingerprint, _retry = await _full_refusal(rig, p)
    assert reason == "stock_counted" and fingerprint is not None
    del p.client.errors["set_price"]
    if case == "changed_row":
        p.client.stock_count = 7  # строка списка изменилась: отпечаток другой
    if case == "engine_keeps_price":
        # конкурент дороже, повышение выключено: движок цену не меняет
        p.client.competitor_price = 120
        await execute(rig, "UPDATE product SET auto_raise=false WHERE id=:i", i=p.product_id)
    calls = len(p.client.calls)
    await _slice_at(p, 61)
    methods = [method for method, _ in p.client.calls[calls:]]
    if case == "engine_keeps_price":
        assert methods == ["find_product", "get_offers"]
    else:
        assert methods == ["find_product", "get_offers", "get_product", "find_product", "set_price"]
    assert await _full_refusal(rig, p) == (None, None, None, None)
