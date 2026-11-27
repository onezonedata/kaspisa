"""Ограничитель частоты (Т9-Т11)."""

# ruff: noqa: F401, F811  (фикстуры импортируются из kaspi_fixtures; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import itertools

from kaspi_fixtures import fake_time
from kaspi_testing import FakeTime

from kaspisa.kaspi import IntervalRateLimiter


def _limiter(fake: FakeTime) -> IntervalRateLimiter:
    return IntervalRateLimiter(1.0, clock=fake.clock, sleep=fake.sleep)


async def test_same_key_waits_interval(fake_time: FakeTime) -> None:
    """Т9: три запроса одного ключа - паузы 0, 1, 1."""
    limiter = _limiter(fake_time)
    for _ in range(3):
        await limiter.acquire("kaspi.kz")
    assert fake_time.sleeps == [1.0, 1.0]
    assert fake_time.now == 2.0


async def test_different_keys_independent_and_idle_resets(fake_time: FakeTime) -> None:
    """Т10: разные ключи не ждут; после простоя паузы нет."""
    limiter = _limiter(fake_time)
    await limiter.acquire("a")
    await limiter.acquire("b")
    assert fake_time.sleeps == []
    fake_time.now += 5.0
    await limiter.acquire("a")
    assert fake_time.sleeps == []


async def test_concurrent_acquires_are_spaced(fake_time: FakeTime) -> None:
    """Т11: пять одновременных запросов одного ключа идут с шагом не меньше интервала."""
    limiter = _limiter(fake_time)
    moments: list[float] = []

    async def one() -> None:
        await limiter.acquire("k")
        moments.append(fake_time.now)

    await asyncio.gather(*(one() for _ in range(5)))
    moments.sort()
    assert len(moments) == 5
    assert all(b - a >= 1.0 for a, b in itertools.pairwise(moments))
