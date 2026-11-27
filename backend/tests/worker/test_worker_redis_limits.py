"""Т15–Т17, Т19, Т71: ограничитель частоты и блокировка входа на настоящем Redis."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from redis.asyncio import Redis
from worker_testing import FakeClock, redis_client, redis_prefix

from kaspisa.worker.redis_limits import RedisIntervalLimiter


class Sleeps:
    """Записывает задержки, ничего не ожидая."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _limiter(
    redis: Redis, prefix: str, sleeps: Sleeps, clock: FakeClock | None = None
) -> RedisIntervalLimiter:
    return RedisIntervalLimiter(
        redis,
        prefix=prefix,
        min_interval=1.0,
        clock=None if clock is None else clock.seconds,
        sleep=sleeps,
    )


async def test_two_instances_share_one_key(redis_client: Redis, redis_prefix: str) -> None:
    """Т15: три acquire подряд дают задержки 0, 1, 2 независимо от экземпляра."""
    sleeps = Sleeps()
    clock = FakeClock()
    first = _limiter(redis_client, redis_prefix, sleeps, clock)
    second = _limiter(redis_client, redis_prefix, sleeps, clock)
    await first.acquire("kaspi.kz")
    await second.acquire("kaspi.kz")
    await first.acquire("kaspi.kz")
    assert sleeps.delays == [1.0, 2.0]  # первый запрос не ждал


async def test_different_keys_do_not_delay_each_other(
    redis_client: Redis, redis_prefix: str
) -> None:
    """Т16."""
    sleeps = Sleeps()
    limiter = _limiter(redis_client, redis_prefix, sleeps, FakeClock())
    for key in ("kaspi.kz", "mc:A", "mc:B"):
        await limiter.acquire(key)
    assert sleeps.delays == []


async def test_penalize_delays_every_instance(redis_client: Redis, redis_prefix: str) -> None:
    """Т17."""
    sleeps = Sleeps()
    clock = FakeClock()
    first = _limiter(redis_client, redis_prefix, sleeps, clock)
    second = _limiter(redis_client, redis_prefix, sleeps, clock)
    await first.penalize("kaspi.kz", 60)
    await second.acquire("kaspi.kz")
    await second.acquire("mc:A")
    assert sleeps.delays == [60.0]


async def test_penalize_does_not_shorten_existing_wait(
    redis_client: Redis, redis_prefix: str
) -> None:
    sleeps = Sleeps()
    limiter = _limiter(redis_client, redis_prefix, sleeps, FakeClock())
    await limiter.penalize("k", 60)
    await limiter.penalize("k", 10)
    await limiter.acquire("k")
    assert sleeps.delays == [60.0]


async def test_redis_time_is_used_in_production_mode(
    redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т71: без внедрённых часов время берёт Redis; часы процессов (±2 с) не важны."""
    for shift in (0.0, 2.0, -2.0):
        prefix = f"{redis_prefix}:{shift}"
        monkeypatch.setattr(time, "time", lambda s=shift: 1_000_000.0 + s)
        sleeps = Sleeps()
        first = _limiter(redis_client, prefix, sleeps)
        second = _limiter(redis_client, prefix, sleeps)
        await first.acquire("kaspi.kz")
        await second.acquire("kaspi.kz")
        await first.acquire("kaspi.kz")
        assert len(sleeps.delays) == 2
        assert 0.5 <= sleeps.delays[0] <= 1.0
        assert 1.5 <= sleeps.delays[1] <= 2.0


async def test_redis_time_is_used_when_process_clocks_disagree(
    redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т71: два экземпляра на одном ключе, часы их процессов расходятся на 4 с."""
    offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0 + offset[0])
    sleeps = Sleeps()
    first = _limiter(redis_client, redis_prefix, sleeps)
    second = _limiter(redis_client, redis_prefix, sleeps)
    offset[0] = 2.0
    await first.acquire("kaspi.kz")
    offset[0] = -2.0
    await second.acquire("kaspi.kz")
    offset[0] = 2.0
    await first.acquire("kaspi.kz")
    assert len(sleeps.delays) == 2
    assert 0.5 <= sleeps.delays[0] <= 1.0
    assert 1.5 <= sleeps.delays[1] <= 2.0


async def test_unavailable_redis_raises_and_counts() -> None:
    from redis.asyncio import Redis as AsyncRedis

    from kaspisa.worker.redis_limits import LimiterUnavailableError, RedisIntervalLimiter

    dead = AsyncRedis(port=1, socket_connect_timeout=0.5)
    try:
        limiter = RedisIntervalLimiter(dead, prefix="x", sleep=Sleeps())
        assert limiter.failures == 0
        for expected in (1, 2):
            with pytest.raises(LimiterUnavailableError):
                await limiter.acquire("kaspi.kz")
            assert limiter.failures == expected
    finally:
        await dead.aclose()


async def test_limiter_is_interval_rate_limiter(redis_client: Redis, redis_prefix: str) -> None:
    from kaspisa.kaspi import IntervalRateLimiter

    assert isinstance(_limiter(redis_client, redis_prefix, Sleeps()), IntervalRateLimiter)


# --- Т19: блокировка входа ---


async def test_login_lock(redis_client: Redis, redis_prefix: str) -> None:
    from kaspisa.worker.redis_limits import LoginLock

    lock = LoginLock(redis_client, prefix=redis_prefix)
    shop = uuid.uuid4()
    token = await lock.acquire(shop, 5000)
    assert token is not None
    assert await lock.acquire(shop, 5000) is None
    assert await lock.release(shop, "чужой-токен") is False
    assert await lock.acquire(shop, 5000) is None  # чужой токен блокировку не снял
    assert await lock.release(shop, token) is True
    assert await lock.release(shop, token) is False
    assert await lock.acquire(shop, 5000) is not None
    other = await lock.acquire(uuid.uuid4(), 5000)  # другой магазин не мешает
    assert other is not None


async def test_login_lock_expires(redis_client: Redis, redis_prefix: str) -> None:
    from kaspisa.worker.redis_limits import LoginLock

    lock = LoginLock(redis_client, prefix=redis_prefix)
    shop = uuid.uuid4()
    assert await lock.acquire(shop, 200) is not None
    assert await lock.acquire(shop, 200) is None
    await asyncio.sleep(0.3)
    assert await lock.acquire(shop, 200) is not None


async def test_login_lock_hold_context(redis_client: Redis, redis_prefix: str) -> None:
    from kaspisa.worker.redis_limits import LoginLock

    lock = LoginLock(redis_client, prefix=redis_prefix)
    shop = uuid.uuid4()
    async with lock.hold(shop, 5000) as got:
        assert got is True
        async with lock.hold(shop, 5000) as second:
            assert second is False
    async with lock.hold(shop, 5000) as again:
        assert again is True
