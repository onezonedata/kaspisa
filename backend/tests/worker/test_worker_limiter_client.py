"""Т18: KaspiClient с RedisIntervalLimiter через respx; ключи и попытки."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from redis.asyncio import Redis
from worker_testing import FakeClock, authenticated_session, redis_client, redis_prefix

from kaspisa.kaspi import KaspiClient

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi"
OFFERS_URL = "https://kaspi.kz/yml/offer-view/offers/900000005"
EDIT_PRICE_URL = "https://mc.shop.kaspi.kz/bff/offer-view/edit-price"
SKU = "900000005_900000024"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def _no_sleep(delay: float) -> None:
    return None


@pytest.fixture
def mock_kaspi() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


async def test_client_acquires_limiter_per_host_and_attempt(
    redis_client: Redis, redis_prefix: str, mock_kaspi: respx.MockRouter
) -> None:
    from kaspisa.worker.redis_limits import RedisIntervalLimiter

    keys: list[str] = []

    class Recording(RedisIntervalLimiter):
        async def acquire(self, key: str) -> None:
            keys.append(key)
            await super().acquire(key)

    limiter = Recording(
        redis_client,
        prefix=redis_prefix,
        clock=FakeClock().seconds,
        sleep=_no_sleep,
    )
    mock_kaspi.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_load("offers.json")))
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=_load("edit-price.json")),
        ]
    )
    async with KaspiClient(limiter=limiter, sleep=_no_sleep) as client:
        await client.get_offers("900000005", "710000000", "90100003")
        await client.get_product(authenticated_session(), "M1", SKU)
    assert keys == ["kaspi.kz", "mc:M1", "mc:M1"]
