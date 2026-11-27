"""Т24–Т26, Т29, Т30, Т32, Т33, Т38–Т40: запись цены (до отказов и подтверждения)."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    CITY_ID,
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    execute,
    fetch,
    journal,
    listed_product,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    shop_row,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, add_sent_rows, decision, hold_lease, prepare

from kaspisa.kaspi.models import ProductSnapshot


async def _slice_env(
    rig: Rig, redis: Redis, prefix: str, **kw: Any
) -> tuple[Prepared, FakeKaspiClient]:
    prepared = await prepare(rig, redis, prefix, **kw)
    return prepared, prepared.client


# --- Т24, Т32, Т40 ---


async def test_write_chain_order_and_journal(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p, client = await _slice_env(rig, redis_client, redis_prefix)
    assert await run_one_slice(p.ctx, p.database)
    assert client.methods() == [
        "find_product",
        "get_offers",
        "get_product",
        "find_product",
        "set_price",
    ]
    snapshot, prices = client.sent[0]
    assert isinstance(snapshot, ProductSnapshot)
    assert snapshot.state.sku == p.sku and snapshot.listing.sku == p.sku
    assert prices == {CITY_ID: 94}
    # Т32
    rows = await fetch(
        rig,
        "SELECT status, job_id, sent_at, next_verify_at FROM price_write WHERE product_id=:i",
        i=p.product_id,
    )
    assert rows == [("receipt", "job-1", NOW, NOW + timedelta(seconds=30))]
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "unconfirmed", None)]
    # Т40
    detail = await fetch(
        rig,
        "SELECT target_price, competitor_merchant_id, competitor_price, capped_at_max, "
        "skipped_competitors FROM price_change WHERE shop_id=:i",
        i=p.shop.shop_id,
    )
    assert detail == [(94, "other", 95, False, 0)]


# --- Т25, Т26 ---


async def test_listing_missing_in_chain_is_journaled(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p, client = await _slice_env(rig, redis_client, redis_prefix)
    client.find_results = [p.listing(100), None]
    await run_one_slice(p.ctx, p.database)
    assert "set_price" not in client.methods()
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "failed", "listing_missing")
    ]
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    checked = await fetch(rig, "SELECT last_checked_at FROM product WHERE id=:i", i=p.product_id)
    assert checked[0][0] is not None


async def test_fresh_price_differs_from_decision_input(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p, client = await _slice_env(rig, redis_client, redis_prefix)
    client.find_results = [p.listing(100), p.listing(95)]
    await run_one_slice(p.ctx, p.database)
    assert "set_price" not in client.methods()
    assert await journal(rig, p.shop.shop_id) == []
    assert await fetch(rig, "SELECT 1 FROM price_write") == []


# --- Т29 ---

CHANGES = [
    ("UPDATE product SET min_price=95 WHERE id=:p", "bounds"),
    ("UPDATE product SET min_price=NULL WHERE id=:p", "bounds"),
    ("UPDATE product SET max_price=NULL WHERE id=:p", "bounds"),
    ("UPDATE product SET max_price=90 WHERE id=:p", "bounds"),
    ("UPDATE product SET tracked=false WHERE id=:p", None),
    ("UPDATE shop SET bot_mode='off' WHERE id=:s", None),
    ("UPDATE shop SET halt_reason='write_errors', halted_at=now() WHERE id=:s", "halted"),
    ("UPDATE shop SET connection_state='reauth_required', connection_error='x' WHERE id=:s", None),
]


@pytest.mark.parametrize(("sql", "reason"), CHANGES)
async def test_state_changed_between_decision_and_write(
    sql: str, reason: str | None, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.writer import WriteOutcome

    p, client = await _slice_env(rig, redis_client, redis_prefix)
    product = await p.product()

    async def change(method: str, key: str) -> None:
        if method == "get_product":
            await execute(rig, sql, p=p.product_id, s=p.shop.shop_id)

    client.on_call = change
    result = await p.write(94, product=product)
    assert result.outcome is WriteOutcome.SKIPPED
    if reason is not None:
        assert result.reason == reason
    assert client.sent == []
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    assert await journal(rig, p.shop.shop_id) == []


# --- Т30 ---


async def test_row_is_not_locked_while_set_price_waits(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p, client = await _slice_env(rig, redis_client, redis_prefix)
    client.set_price_gate = asyncio.Event()
    reached = asyncio.Event()

    async def notice(method: str, key: str) -> None:
        if method == "set_price":
            reached.set()

    client.on_call = notice
    task = asyncio.create_task(p.write(94))
    async with asyncio.timeout(10):
        await reached.wait()
    started = time.monotonic()
    await execute(rig, "UPDATE product SET min_price=60 WHERE id=:p", p=p.product_id)
    await execute(rig, "UPDATE shop SET city_id='1' WHERE id=:s", s=p.shop.shop_id)
    assert time.monotonic() - started < 1
    client.set_price_gate.set()
    result = await task
    assert result.outcome.value == "sent"


# --- Т33 ---


class Killed(BaseException):
    """Имитация убитого процесса: не Exception и не отмена."""


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), Killed()])
async def test_hold_survives_interrupted_send_and_restart(
    failure: BaseException, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.writer import WriteOutcome

    p, client = await _slice_env(rig, redis_client, redis_prefix)
    client.errors["set_price"] = failure
    with pytest.raises(type(failure)):
        await run_one_slice(p.ctx, p.database)
    row = await fetch(
        rig,
        "SELECT status, sent_at, snapshot, expected_prices FROM price_write WHERE product_id=:i",
        i=p.product_id,
    )
    assert row[0][0] in ("sending", "unknown")
    assert row[0][1] == NOW and row[0][2] and row[0][3] == {CITY_ID: 94}
    # новый обработчик на той же базе
    later = FakeClock(NOW + timedelta(seconds=599))
    again = await prepare(
        rig,
        redis_client,
        redis_prefix,
        clock=later,
        shop=p.shop,
        database=rig.new_database(),
        worker_id="w2",
    )
    result = await again.write(94)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "hold")
    assert again.client.sent == []
    ctx2 = again.ctx
    ctx2.clock.now = NOW + timedelta(seconds=600)
    assert await run_one_slice(ctx2, again.database)
    assert len(again.client.sent) == 1


# --- Т38 ---


async def test_quota_blocks_251st_send(rig: Rig, redis_client: Redis, redis_prefix: str) -> None:
    from kaspisa.worker.writer import WriteOutcome

    p, client = await _slice_env(rig, redis_client, redis_prefix)
    await add_sent_rows(rig, p.shop, 250, NOW - timedelta(seconds=1799))
    result = await p.write(94)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "quota")
    assert client.sent == []
    assert await fetch(rig, "SELECT 1 FROM price_write") == []


async def test_quota_window_edge_allows_send(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p, _client = await _slice_env(rig, redis_client, redis_prefix)
    await add_sent_rows(rig, p.shop, 1, NOW - timedelta(seconds=1800))
    await add_sent_rows(rig, p.shop, 249, NOW - timedelta(seconds=10))
    result = await p.write(94)
    assert result.outcome.value == "sent"


async def test_quota_two_concurrent_writes_one_wins(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    first = await prepare(rig, redis_client, redis_prefix, products=2)
    second = await prepare(
        rig,
        redis_client,
        redis_prefix,
        shop=first.shop,
        database=rig.new_database(),
    )
    await hold_lease(rig, first.shop, "w1", NOW + timedelta(days=1))
    await add_sent_rows(rig, first.shop, 249, NOW - timedelta(seconds=10))
    product_a = await first.product()
    async with second.database.tenant(second.shop.tenant_id) as scope:
        product_b = await scope.products.get(first.shop.product_ids[1])
    assert product_b is not None
    from kaspisa.worker.writer import write_price

    async def send(prep: Prepared, product: Any, sku: str) -> Any:
        return await write_price(prep.sctx, product, decision(94), 100)

    results = await asyncio.gather(
        send(first, product_a, first.shop.skus[0]), send(second, product_b, first.shop.skus[1])
    )
    assert sorted(r.outcome.value for r in results) == ["sent", "skipped"]
    assert len(first.client.sent) + len(second.client.sent) == 1


async def test_exhausted_quota_slice_makes_no_reads_and_defers(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p, client = await _slice_env(rig, redis_client, redis_prefix)
    await add_sent_rows(rig, p.shop, 250, NOW - timedelta(seconds=1700))
    assert await run_one_slice(p.ctx, p.database)
    assert client.calls == []
    assert (await shop_row(rig, p.shop.shop_id))["claimed_until"] == NOW + timedelta(seconds=100)
    assert not await run_one_slice(p.ctx, p.database, NOW + timedelta(seconds=99))
    assert await run_one_slice(p.ctx, p.database, NOW + timedelta(seconds=100))


# --- Т39 ---


async def test_concurrent_writes_to_one_sku_send_once(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    first = await prepare(rig, redis_client, redis_prefix)
    second = await prepare(
        rig,
        redis_client,
        redis_prefix,
        shop=first.shop,
        database=rig.new_database(),
    )
    await hold_lease(rig, first.shop, "w1", NOW + timedelta(days=1))
    results = await asyncio.gather(first.write(94), second.write(94))
    outcomes = sorted((r.outcome.value, r.reason) for r in results)
    assert outcomes == [("sent", None), ("skipped", "hold")]
    assert len(first.client.sent) + len(second.client.sent) == 1
    assert len(await fetch(rig, "SELECT 1 FROM price_write")) == 1


async def test_unchanged_price_is_reported_with_its_own_reason(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.writer import WriteOutcome

    p = await prepare(rig, redis_client, redis_prefix)
    result = await p.write(100, current=100)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "same_price")
    assert p.client.calls == []
