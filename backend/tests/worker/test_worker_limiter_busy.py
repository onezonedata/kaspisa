"""Т75: 429, ограниченное ожидание ограничителя и перегруженный ключ записи."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    Sleeps,
    fetch,
    journal,
    make_worker_ctx,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    shop_row,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, prepare

from kaspisa.kaspi import PriceWriteOutcomeUnknownError, RateLimitedError
from kaspisa.worker import redis_limits
from kaspisa.worker.redis_limits import RedisIntervalLimiter
from kaspisa.worker.writer import WriteOutcome


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


def _limiter(redis: Redis, prefix: str, clock: FakeClock, **kw: object) -> RedisIntervalLimiter:
    return RedisIntervalLimiter(
        redis,
        prefix=prefix,
        clock=clock.seconds,
        sleep=Sleeps(),
        **kw,  # type: ignore[arg-type]
    )


async def test_limiter_refuses_long_waits_without_sleeping_or_reserving(
    redis_client: Redis, redis_prefix: str
) -> None:
    clock = FakeClock()
    sleeps = Sleeps()
    limiter = RedisIntervalLimiter(
        redis_client, prefix=redis_prefix, clock=clock.seconds, sleep=sleeps, max_wait=30
    )
    await limiter.penalize("kaspi.kz", 150)
    with pytest.raises(redis_limits.LimiterBusyError) as caught:
        await limiter.acquire("kaspi.kz")
    assert caught.value.wait_seconds == pytest.approx(150)
    assert sleeps.delays == []
    patient = RedisIntervalLimiter(
        redis_client, prefix=redis_prefix, clock=clock.seconds, sleep=sleeps
    )
    await patient.acquire("kaspi.kz")
    assert sleeps.delays == [150.0]  # отказ слот не занял
    assert await limiter.pending_delay("mc:none") == 0.0
    assert await limiter.pending_delay("kaspi.kz") == pytest.approx(151.0)


async def test_rate_limit_defers_the_shop_until_the_penalty_ends(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.errors["get_offers"] = RateLimitedError("get_offers", 150)
    assert await run_one_slice(p.ctx, p.database, NOW)
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["claimed_by"], row["claimed_until"]) == ("w1", at(150))
    assert not await run_one_slice(p.ctx, p.database, at(149))


async def test_other_shop_gets_busy_error_and_waits_for_the_penalty(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database)
    client = FakeKaspiClient()
    ctx = make_worker_ctx(
        database, client, redis_client, redis_prefix, clock, dry_run=True, limiter_max_wait=30
    )
    await ctx.limiter.penalize("kaspi.kz", 150)

    async def waits_in_limiter(method: str, key: str) -> None:
        if method == "get_offers":
            await ctx.limiter.acquire("kaspi.kz")

    client.on_call = waits_in_limiter
    assert await run_one_slice(ctx, database, NOW)
    row = await shop_row(rig, shop.shop_id)
    assert (row["claimed_by"], row["claimed_until"]) == ("w1", at(150))  # аренда не истекает
    assert client.methods() == ["find_product", "get_offers"]


async def test_limiter_busy_inside_set_price_is_compensated(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = PriceWriteOutcomeUnknownError(p.sku, "LimiterBusyError")
    with pytest.raises(redis_limits.LimiterBusyError):
        await p.write(94)
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    assert await journal(rig, p.shop.shop_id) == []
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0


async def test_write_is_skipped_when_the_write_key_is_busy(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    key = f"mc:{p.shop.merchant_uid}"
    await p.ctx.limiter.penalize(key, 10)
    result = await p.write(94)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "limiter_busy")
    assert p.client.sent == []
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    assert await journal(rig, p.shop.shop_id) == []
    # ожидание не больше 3 с не мешает записи
    p.clock.advance(7)
    assert (await p.write(94)).outcome is WriteOutcome.SENT


async def test_busy_defer_is_rounded_up_to_whole_seconds(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database)
    client = FakeKaspiClient()
    ctx = make_worker_ctx(
        database, client, redis_client, redis_prefix, clock, dry_run=True, limiter_max_wait=30
    )
    await ctx.limiter.penalize("kaspi.kz", 150.4)

    async def waits_in_limiter(method: str, key: str) -> None:
        if method == "get_offers":
            await ctx.limiter.acquire("kaspi.kz")

    client.on_call = waits_in_limiter
    assert await run_one_slice(ctx, database, NOW)
    assert (await shop_row(rig, shop.shop_id))["claimed_until"] == at(151)


async def test_limiter_wait_equal_to_the_limit_is_allowed(
    redis_client: Redis, redis_prefix: str
) -> None:
    clock = FakeClock()
    sleeps = Sleeps()
    limiter = RedisIntervalLimiter(
        redis_client, prefix=redis_prefix, clock=clock.seconds, sleep=sleeps, max_wait=1.0
    )
    await limiter.acquire("k")
    await limiter.acquire("k")  # ждать ровно 1 с: предел не превышен
    assert sleeps.delays == [1.0]
    with pytest.raises(redis_limits.LimiterBusyError):
        await limiter.acquire("k")  # ждать 2 с


@pytest.mark.parametrize(("penalty", "skipped"), [(3.0, False), (3.5, True), (4.0, True)])
async def test_write_wait_threshold_is_three_seconds(
    penalty: float, skipped: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    await p.ctx.limiter.penalize(f"mc:{p.shop.merchant_uid}", penalty)
    result = await p.write(94)
    if skipped:
        assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "limiter_busy")
    else:
        assert result.outcome is WriteOutcome.SENT


async def test_limiter_busy_raised_straight_from_set_price_is_compensated(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = redis_limits.LimiterBusyError(5)
    with pytest.raises(redis_limits.LimiterBusyError):
        await p.write(94)
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    assert await journal(rig, p.shop.shop_id) == []
