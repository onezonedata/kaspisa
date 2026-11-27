"""Т21–Т23: планировщик и слайс магазина без записи."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import text
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    SeededShop,
    redis_client,
    redis_prefix,
    rig,
    seed_shop,
    worker_cipher,
    worker_database_url,
)

from kaspisa.db import Database
from kaspisa.worker.context import WorkerContext


def _context(
    database: Database,
    client: FakeKaspiClient,
    redis: Redis,
    prefix: str,
    clock: FakeClock,
    worker_id: str = "w1",
) -> WorkerContext:
    from kaspisa.worker.context import WorkerConfig, WorkerContext
    from kaspisa.worker.redis_limits import RedisIntervalLimiter

    async def no_sleep(delay: float) -> None:
        return None

    limiter = RedisIntervalLimiter(redis, prefix=prefix, clock=clock.seconds, sleep=no_sleep)
    return WorkerContext(
        database=database,
        client=client,
        redis=redis,
        limiter=limiter,
        config=WorkerConfig(worker_id=worker_id, dry_run=True, slots=1),
        clock=clock,
    )


async def _drain(ctx: WorkerContext, database: Database) -> int:
    """Захват и слайсы, пока брать нечего; возвращает число слайсов."""
    from kaspisa.worker.constants import (
        LEASE_SECONDS,
        MIN_RECHECK_SECONDS,
        UNBOUNDED_RECHECK_SECONDS,
    )
    from kaspisa.worker.slice import run_shop_slice

    slices = 0
    while True:
        async with database.system() as system:
            claim = await system.claim_shop(
                worker_id=ctx.config.worker_id,
                now=ctx.clock(),
                lease_seconds=LEASE_SECONDS,
                recheck_seconds=MIN_RECHECK_SECONDS,
                unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
            )
        if claim is None:
            return slices
        await run_shop_slice(ctx, claim)
        slices += 1


async def test_each_product_once_per_circle_oldest_first(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т21."""
    clock = FakeClock()
    database = rig.new_database()
    ages = [None, 500, 100, 400, 90, 300, 200, 80, 70, 250]
    shop = await seed_shop(
        rig,
        database,
        products=10,
        last_checked=[None if a is None else NOW - timedelta(seconds=a) for a in ages],
    )
    client = FakeKaspiClient()
    ctx = _context(database, client, redis_client, redis_prefix, clock)

    await _drain(ctx, database)
    order = [key for method, key in client.calls if method == "find_product"]
    # самый давний первым, NULL — самым первым
    by_age = sorted(range(10), key=lambda i: (ages[i] is not None, -(ages[i] or 0)))
    assert order == [shop.skus[i] for i in by_age]
    assert len(order) == len(set(order)) == 10

    # раньше 60 с повторно ничего не проверяется
    clock.advance(59)
    client.calls.clear()
    assert await _drain(ctx, database) == 0
    assert client.calls == []

    # через 60 с — следующий круг, каждый товар ровно один раз
    clock.advance(1)
    assert await _drain(ctx, database) == 1
    again = [key for method, key in client.calls if method == "find_product"]
    assert sorted(again) == sorted(shop.skus)


async def _claimed(rig: Rig, shop: SeededShop) -> tuple[str | None, datetime | None]:
    async with rig.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT claimed_by, claimed_until FROM shop WHERE id = :i"),
                {"i": shop.shop_id},
            )
        ).one()
    return row[0], row[1]


async def test_slice_stops_at_20_products(rig: Rig, redis_client: Redis, redis_prefix: str) -> None:
    """Т22: 20 товаров за слайс; аренда снимается в конце."""
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=25)
    client = FakeKaspiClient()
    ctx = _context(database, client, redis_client, redis_prefix, clock)
    await _drain_once(ctx, database)
    assert len([c for c in client.calls if c[0] == "find_product"]) == 20
    assert await _claimed(rig, shop) == (None, None)


async def _drain_once(ctx: WorkerContext, database: Database) -> None:
    from kaspisa.worker.constants import (
        LEASE_SECONDS,
        MIN_RECHECK_SECONDS,
        UNBOUNDED_RECHECK_SECONDS,
    )
    from kaspisa.worker.slice import run_shop_slice

    async with database.system() as system:
        claim = await system.claim_shop(
            worker_id=ctx.config.worker_id,
            now=ctx.clock(),
            lease_seconds=LEASE_SECONDS,
            recheck_seconds=MIN_RECHECK_SECONDS,
            unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
        )
    assert claim is not None
    await run_shop_slice(ctx, claim)


async def test_slice_stops_at_30_seconds_and_renews_lease(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т22: каждый вызов — 1 с, товар — 2 вызова; через 30 с слайс заканчивается."""
    from kaspisa.worker.constants import LEASE_SECONDS

    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=25)
    client = FakeKaspiClient(call_seconds=1.0, clock=clock)
    seen: list[tuple[datetime, datetime | None]] = []

    async def spy(method: str, key: str) -> None:
        if method == "find_product":
            seen.append((clock(), (await _claimed(rig, shop))[1]))

    client.on_call = spy
    ctx = _context(database, client, redis_client, redis_prefix, clock)
    await _drain_once(ctx, database)
    assert len([c for c in client.calls if c[0] == "find_product"]) == 15
    # к началу каждого товара аренда продлена от момента окончания предыдущего
    for index, (moment, until) in enumerate(seen):
        assert until == moment + timedelta(seconds=LEASE_SECONDS), index
    assert await _claimed(rig, shop) == (None, None)


async def test_two_workers_never_share_a_shop(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т23: два Worker на двух движках; каждый товар проверен, sku не обрабатывается параллельно."""
    from kaspisa.worker.runner import Worker

    shops = []
    seed_db = rig.new_database()
    for _ in range(6):
        shops.append(await seed_shop(rig, seed_db, products=5))
    clock = FakeClock(datetime.now(tz=NOW.tzinfo))
    client = FakeKaspiClient(pause=0.01)
    workers = []
    for index in range(2):
        ctx = _context(rig.new_database(), client, redis_client, redis_prefix, clock, f"w{index}")
        workers.append(Worker(ctx, idle_sleep=0.05))
    stop = asyncio.Event()
    runs = [asyncio.create_task(worker.run(stop)) for worker in workers]
    try:
        async with asyncio.timeout(30):
            while True:
                async with rig.engine.connect() as conn:
                    left = (
                        await conn.execute(
                            text("SELECT count(*) FROM product WHERE last_checked_at IS NULL")
                        )
                    ).scalar_one()
                if left == 0:
                    break
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.gather(*runs)
    finds = [key for method, key in client.calls if method == "find_product"]
    assert sorted(finds) == sorted(sku for shop in shops for sku in shop.skus)
    by_sku: dict[str, list[tuple[float, float]]] = {}
    for key, start, end in client.spans:
        by_sku.setdefault(key, []).append((start, end))
    for spans in by_sku.values():
        spans.sort()
        for (_, end_a), (start_b, _) in itertools.pairwise(spans):
            assert end_a <= start_b
