"""ADR 0017, Т4-Т8, Т35: причина пропуска проверки хранится в товаре."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    Rig,
    execute,
    fetch,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, prepare

from kaspisa.kaspi import KaspiTransportError, RateLimitedError, ResponseFormatError

PP1 = "710000000"
SECOND = "620000000"


async def _skip(rig: Rig, p: Prepared) -> tuple[Any, ...]:
    rows = await fetch(
        rig,
        "SELECT skip_reason, skipped_at, last_checked_at FROM product WHERE id=:i",
        i=p.product_id,
    )
    return tuple(rows[0])


async def _observations(rig: Rig, p: Prepared) -> tuple[Any, ...]:
    rows = await fetch(
        rig,
        "SELECT current_price, position, competitors_count FROM product WHERE id=:i",
        i=p.product_id,
    )
    return tuple(rows[0])


async def test_listing_missing_is_stored(rig: Rig, redis_client: Redis, redis_prefix: str) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.find_results = [None]
    assert await run_one_slice(p.ctx, p.database)
    reason, skipped_at, checked = await _skip(rig, p)
    assert reason == "listing_missing"
    assert skipped_at == checked == p.clock.now
    assert await _observations(rig, p) == (None, None, None)


async def test_own_price_unknown_is_stored(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.cities = {PP1: 1000, SECOND: 1100}
    assert await run_one_slice(p.ctx, p.database)
    assert (await _skip(rig, p))[0] == "own_price_unknown"


async def test_kaspi_errors_store_closed_codes_only(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["get_offers"] = ResponseFormatError("get_offers", ("SECRET-FIELD",))
    assert await run_one_slice(p.ctx, p.database)
    assert (await _skip(rig, p))[0] == "response_format"

    p.client.errors["get_offers"] = KaspiTransportError("get_offers", "SECRET-KIND")
    p.clock.advance(61)
    assert await run_one_slice(p.ctx, p.database)
    assert (await _skip(rig, p))[0] == "kaspi_error"
    stored = await fetch(rig, "SELECT skip_reason FROM product")
    assert all("SECRET" not in str(row) and "Error" not in str(row) for row in stored)


async def test_successful_check_clears_reason_but_write_hold_keeps_it(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.cities = {PP1: 1000, SECOND: 1100}
    assert await run_one_slice(p.ctx, p.database)
    assert (await _skip(rig, p))[0] == "own_price_unknown"

    async def holding(*_: Any) -> bool:
        return True

    monkeypatch.setattr("kaspisa.worker.cycle._hold_active", holding)
    p.client.cities = None
    p.clock.advance(61)
    assert await run_one_slice(p.ctx, p.database)
    reason, skipped_at, checked = await _skip(rig, p)
    assert reason == "own_price_unknown"  # запрет записи - не проверка
    assert checked == p.clock.now and skipped_at < checked
    monkeypatch.undo()

    p.clock.advance(61)
    assert await run_one_slice(p.ctx, p.database)
    reason, skipped_at, _ = await _skip(rig, p)
    assert (reason, skipped_at) == (None, None)


async def test_engine_rejection_is_stored(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(*_: Any, **__: Any) -> Any:
        raise ValueError("вход отвергнут")

    monkeypatch.setattr("kaspisa.worker.cycle.decide_price", reject)
    p = await prepare(rig, redis_client, redis_prefix)
    assert await run_one_slice(p.ctx, p.database)
    assert (await _skip(rig, p))[0] == "engine_rejected"


async def test_rate_limit_is_not_a_skip_reason(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def limited(*_: Any) -> Any:
        raise RateLimitedError("get_offers", 30)

    monkeypatch.setattr("kaspisa.worker.slice.check_product", limited)
    p = await prepare(rig, redis_client, redis_prefix)
    assert await run_one_slice(p.ctx, p.database)
    reason, skipped_at, checked = await _skip(rig, p)
    assert (reason, skipped_at) == (None, None)
    assert checked is not None
