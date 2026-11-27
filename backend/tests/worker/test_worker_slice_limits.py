"""Т60, Т61: недоступный Redis и лимит 429 в слайсе."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    redis_client,
    redis_prefix,
    rig,
    seed_shop,
    worker_cipher,
    worker_database_url,
)

from kaspisa.db import ClaimedShop, Database
from kaspisa.kaspi import RateLimitedError
from kaspisa.worker.context import WorkerContext


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _ctx(
    database: Database,
    client: FakeKaspiClient,
    redis: Redis,
    prefix: str,
    clock: FakeClock,
    sleeps: Sleeps,
) -> WorkerContext:
    from kaspisa.worker.context import WorkerConfig, WorkerContext
    from kaspisa.worker.redis_limits import RedisIntervalLimiter

    limiter = RedisIntervalLimiter(redis, prefix=prefix, clock=clock.seconds, sleep=sleeps)
    return WorkerContext(
        database=database,
        client=client,
        redis=redis,
        limiter=limiter,
        config=WorkerConfig(worker_id="w1", dry_run=True, slots=1),
        clock=clock,
    )


async def _claim(database: Database, now: datetime) -> ClaimedShop | None:
    from kaspisa.worker.constants import (
        LEASE_SECONDS,
        MIN_RECHECK_SECONDS,
        UNBOUNDED_RECHECK_SECONDS,
    )

    async with database.system() as system:
        return await system.claim_shop(
            worker_id="w1",
            now=now,
            lease_seconds=LEASE_SECONDS,
            recheck_seconds=MIN_RECHECK_SECONDS,
            unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
        )


async def _row(rig: Rig, shop_id: uuid.UUID) -> tuple[Any, ...]:
    async with rig.engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT connection_state, connection_version, halt_reason, claimed_by, "
                "claimed_until, login_error_streak, write_error_streak FROM shop WHERE id = :i"
            ),
            {"i": shop_id},
        )
        return tuple(row.one())


async def test_unavailable_redis_defers_shop_without_reads(rig: Rig) -> None:
    """Т60."""
    from redis.asyncio import Redis as AsyncRedis

    from kaspisa.worker.slice import run_shop_slice

    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=2)
    before = await _row(rig, shop.shop_id)
    dead = AsyncRedis(port=1, socket_connect_timeout=0.5)
    try:
        client = FakeKaspiClient()
        ctx = _ctx(database, client, dead, "x", clock, Sleeps())
        claim = await _claim(database, clock())
        assert claim is not None
        await run_shop_slice(ctx, claim)
    finally:
        await dead.aclose()
    assert client.calls == []
    after = await _row(rig, shop.shop_id)
    assert after[:3] == before[:3]
    assert after[5:] == before[5:]
    assert after[3:5] == ("w1", NOW + timedelta(seconds=300))
    assert await _claim(database, NOW + timedelta(seconds=299)) is None
    assert await _claim(database, NOW + timedelta(seconds=300)) is not None


@pytest.mark.parametrize(("retry_after", "expected"), [(40, 60.0), (90, 90.0)])
async def test_rate_limit_penalizes_and_ends_slice(
    retry_after: int, expected: float, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т61."""
    from kaspisa.worker.slice import run_shop_slice

    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=3)
    client = FakeKaspiClient()
    client.errors["get_offers"] = RateLimitedError("get_offers", retry_after)
    sleeps = Sleeps()
    ctx = _ctx(database, client, redis_client, redis_prefix, clock, sleeps)
    claim = await _claim(database, clock())
    assert claim is not None
    await run_shop_slice(ctx, claim)
    assert client.methods() == ["find_product", "get_offers"]  # слайс закончен на первом товаре
    # магазин отложен до конца штрафа, аренда не снята (ADR 0008, уточнения по ревью)
    assert (await _row(rig, shop.shop_id))[3:5] == ("w1", NOW + timedelta(seconds=expected))
    async with rig.engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM price_change"))).scalar_one() == 0
    await ctx.limiter.acquire("kaspi.kz")
    assert sleeps.delays == [expected]
