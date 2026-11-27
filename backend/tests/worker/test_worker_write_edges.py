"""Границы цены и квоты на самой границе, запрет при отмене, остановка слайса после пропуска."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    Rig,
    execute,
    fetch,
    journal,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    shop_row,
    worker_cipher,
    worker_database_url,
)
from writer_testing import add_sent_rows, decision, hold_lease, prepare

# (min, max, текущая цена, новая цена, уйдёт ли запись)
BOUNDS = [
    (80, 120, 100, 80, True),
    (80, 120, 100, 120, True),
    (80, 120, 100, 79, False),
    (80, 120, 100, 121, False),
    (94, 94, 100, 94, True),
    (94, 94, 100, 93, False),
    (94, 94, 100, 95, False),
    (80, 120, 100, 100, False),  # цена не меняется: решения нет
    (80, 120, 80, 80, False),  # то же на нижней границе
    (80, 120, 100, 0, False),
    (80, 120, 100, -5, False),
    (None, 120, 100, 94, False),
    (80, None, 100, 94, False),
    (None, None, 100, 94, False),
]


@pytest.mark.parametrize(("low", "high", "current", "new", "sent"), BOUNDS)
async def test_price_bounds_edges_through_write_price(
    low: int | None,
    high: int | None,
    current: int,
    new: int,
    sent: bool,
    rig: Rig,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Граница цены включительна; вне её, без границы и без изменения запись не уходит."""
    from kaspisa.worker.writer import WriteOutcome

    p = await prepare(rig, redis_client, redis_prefix)
    p.client.price = current
    await execute(
        rig,
        "UPDATE product SET min_price=:lo, max_price=:hi WHERE id=:p",
        lo=low,
        hi=high,
        p=p.product_id,
    )
    result = await p.write(new, current, product=await p.product(), decision=decision(new))
    if sent:
        assert result.outcome is WriteOutcome.SENT
        assert [list(prices.values()) for _, prices in p.client.sent] == [[new]]
    else:
        in_bounds = low is not None and high is not None and low <= new <= high
        if new == current and in_bounds:  # цена не меняется: своя причина
            assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "same_price")
        else:
            assert result.outcome is WriteOutcome.BLOCKED_BOUNDS
        assert p.client.sent == [] and p.client.calls == []
        assert await fetch(rig, "SELECT 1 FROM price_write") == []
        assert await journal(rig, p.shop.shop_id) == []


CHANGED_BOUNDS = [
    ("UPDATE product SET min_price=94 WHERE id=:p", True),
    ("UPDATE product SET max_price=94 WHERE id=:p", True),
    ("UPDATE product SET min_price=94, max_price=94 WHERE id=:p", True),
    ("UPDATE product SET min_price=95 WHERE id=:p", False),
    ("UPDATE product SET max_price=93 WHERE id=:p", False),
]


@pytest.mark.parametrize(("sql", "sent"), CHANGED_BOUNDS)
async def test_bounds_changed_during_chain_are_inclusive(
    sql: str, sent: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Перепроверка под блокировкой: новая цена на самой границе допустима, за ней - нет."""
    p = await prepare(rig, redis_client, redis_prefix)
    product = await p.product()

    async def change(method: str, key: str) -> None:
        if method == "get_product":
            await execute(rig, sql, p=p.product_id)

    p.client.on_call = change
    result = await p.write(94, product=product)
    assert (result.outcome.value, len(p.client.sent)) == (("sent", 1) if sent else ("skipped", 0))
    if not sent:
        assert result.reason == "bounds"
        assert await fetch(rig, "SELECT 1 FROM price_write") == []


@pytest.mark.parametrize(
    ("rows", "age", "allowed"),
    [
        (249, 10, True),
        (250, 10, False),
        (251, 10, False),
        (250, 1799, False),
        (250, 1800, True),  # строка ровно на краю окна уже не считается
        (251, 1800, True),
    ],
)
async def test_quota_boundary_for_a_single_write(
    rows: int, age: int, allowed: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Квота 250 за 30 минут: 249 - отправка идёт, 250 и 251 - нет."""
    p = await prepare(rig, redis_client, redis_prefix)
    await add_sent_rows(rig, p.shop, rows, NOW - timedelta(seconds=age))
    result = await p.write(94)
    if allowed:
        assert result.outcome.value == "sent"
        assert len(p.client.sent) == 1
    else:
        assert (result.outcome.value, result.reason) == ("skipped", "quota")
        assert p.client.sent == []
        assert await fetch(rig, "SELECT 1 FROM price_write") == []


async def test_cancelled_send_keeps_hold_and_marks_it_unknown(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Отмена вокруг `set_price`: запрет в `unknown`, причина `cancelled`, серия растёт (Н9)."""
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await p.write(94)
    rows = await fetch(rig, "SELECT status FROM price_write WHERE product_id=:i", i=p.product_id)
    assert rows == [("unknown",)]
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "unconfirmed", "cancelled")
    ]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1


# --- слайс: пропуск записи ---


async def test_slice_continues_after_quota_skip_only_until_the_skip(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Квота исчерпана на втором товаре: он проверен, третий нет, магазин отложен до проверки."""
    p = await prepare(rig, redis_client, redis_prefix, products=3)
    await add_sent_rows(rig, p.shop, 249, NOW - timedelta(seconds=100))
    assert await run_one_slice(p.ctx, p.database)
    first, second, third = p.shop.skus
    reads = [key for method, key in p.client.calls if method == "find_product"]
    assert first in reads and second in reads and third not in reads
    assert [key for method, key in p.client.calls if method == "set_price"] == [first]
    claimed_until = (await shop_row(rig, p.shop.shop_id))["claimed_until"]
    # окно квоты освободится через 1700 с, но открытую запись нужно проверить раньше (+30 с)
    assert claimed_until == NOW + timedelta(seconds=30)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE shop SET halt_reason='write_errors', halted_at=now() WHERE id=:s",
        "UPDATE shop SET bot_mode='off' WHERE id=:s",
        "UPDATE shop SET connection_state='reauth_required', connection_error='x' WHERE id=:s",
    ],
    ids=["halted", "bot_disabled", "not_connected"],
)
async def test_slice_stops_after_write_skipped_by_shop_state(
    sql: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Магазин остановлен, пока шла цепочка: остальные товары слайс не берёт."""
    p = await prepare(rig, redis_client, redis_prefix, products=2)

    async def change(method: str, key: str) -> None:
        if method == "get_product":
            await execute(rig, sql, s=p.shop.shop_id)

    p.client.on_call = change
    assert await run_one_slice(p.ctx, p.database)
    reads = {key for method, key in p.client.calls if method == "find_product"}
    assert reads == {p.shop.skus[0]}
    assert p.client.sent == []


async def test_bot_disabled_shop_only_confirms_open_writes(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Бот выключен: открытая запись проверяется, решения по товарам не принимаются."""
    p = await prepare(rig, redis_client, redis_prefix, products=2)
    assert (await p.write(94)).outcome.value == "sent"
    await execute(rig, "UPDATE shop SET bot_mode='off' WHERE id=:s", s=p.shop.shop_id)
    p.client.calls.clear()
    p.clock.now = NOW + timedelta(seconds=30)
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.methods() == ["verify_price"]
    assert await fetch(rig, "SELECT status FROM price_write") == [("confirmed",)]


async def test_compensation_locks_the_product_before_deleting_the_hold(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Порядок блокировок: товар раньше `price_write` (иначе взаимная блокировка с резервом)."""
    from sqlalchemy import text

    from kaspisa.kaspi import RateLimitedError
    from kaspisa.worker.errors import ShopThrottled

    p = await prepare(rig, redis_client, redis_prefix)
    conn = await rig.engine.connect()
    trans = await conn.begin()

    async def lock_product(method: str, key: str) -> None:
        if method == "set_price":
            await conn.execute(
                text("SELECT id FROM product WHERE id=:i FOR UPDATE"), {"i": p.product_id}
            )

    p.client.on_call = lock_product
    p.client.errors["set_price"] = RateLimitedError("set_price", 5)
    task = asyncio.create_task(p.write(94))
    try:
        await asyncio.sleep(0.7)
        assert not task.done()
        # компенсация ждёт товар и ещё ничего не удалила
        assert len(await fetch(rig, "SELECT 1 FROM price_write")) == 1
    finally:
        await trans.rollback()
        await conn.close()
    with pytest.raises(ShopThrottled):
        await asyncio.wait_for(task, 10)
    assert await fetch(rig, "SELECT 1 FROM price_write") == []


async def test_write_is_not_sent_when_the_lease_is_lost_during_the_chain(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Аренду забрал другой экземпляр, пока шла цепочка чтения: `set_price` не вызывается."""
    p = await prepare(rig, redis_client, redis_prefix)
    await hold_lease(rig, p.shop, "w1", NOW + timedelta(seconds=100))

    async def steal(method: str, key: str) -> None:
        if method == "find_product":
            await execute(rig, "UPDATE shop SET claimed_by='w2' WHERE id=:s", s=p.shop.shop_id)

    p.client.on_call = steal
    result = await p.write(94)
    assert (result.outcome.value, result.reason) == ("skipped", "lease_lost")
    assert p.client.sent == [] and "set_price" not in p.client.methods()
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    assert await journal(rig, p.shop.shop_id) == []


async def test_cabinet_request_renews_the_lease_and_is_not_sent_without_it(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.constants import LEASE_SECONDS
    from kaspisa.worker.errors import ShopUnavailable

    p = await prepare(rig, redis_client, redis_prefix)
    access = p.sctx.access
    merchant = p.shop.merchant_uid

    async def read(session: Any) -> Any:
        return await p.client.find_product(session, merchant, p.sku)

    await hold_lease(rig, p.shop, "w1", NOW + timedelta(seconds=5))
    await access.cabinet(read)
    claimed_until = (await shop_row(rig, p.shop.shop_id))["claimed_until"]
    assert claimed_until == NOW + timedelta(seconds=LEASE_SECONDS)
    p.client.calls.clear()
    await execute(rig, "UPDATE shop SET claimed_by='w2' WHERE id=:s", s=p.shop.shop_id)
    with pytest.raises(ShopUnavailable):
        await access.cabinet(read)
    assert p.client.calls == []
