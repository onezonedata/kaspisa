"""Т47–Т49: остановка бота магазина при сериях ошибок записи и входа."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from worker_testing import (
    CITY_ID,
    NOW,
    Rig,
    execute,
    journal,
    logged_in,
    not_applied,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    shop_row,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, hold_lease, prepare

from kaspisa.kaspi import (
    KaspiTransportError,
    LoginRejectedError,
    PriceWriteOutcomeUnknownError,
    WriteRefusal,
    WriteRefusedError,
)
from kaspisa.worker.redis_limits import LoginLock
from kaspisa.worker.writer import WriteOutcome


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


async def _unknown_write(p: Prepared, seconds: int) -> Any:
    """Запись с неизвестным исходом в момент `seconds` от NOW."""
    p.clock.now = at(seconds)
    p.client.errors["set_price"] = PriceWriteOutcomeUnknownError(p.sku, "timeout")
    return await p.write(94)


# --- Т47 ---


async def test_worker_never_clears_halt_but_a_human_does(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    await execute(
        rig,
        "UPDATE shop SET halt_reason='write_errors', halted_at=:t WHERE id=:i",
        t=NOW,
        i=p.shop.shop_id,
    )
    assert not await run_one_slice(p.ctx, p.database)
    assert p.client.calls == []
    assert (await shop_row(rig, p.shop.shop_id))["halt"] == "write_errors"
    async with p.database.tenant(p.shop.tenant_id) as scope:
        await scope.shops.clear_halt(p.shop.shop_id)
        await scope.commit()
    assert await run_one_slice(p.ctx, p.database)
    assert "set_price" in p.client.methods()
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None


async def test_halted_shop_still_confirms_sent_writes_and_keeps_halt(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    assert (await p.write(94)).outcome is WriteOutcome.SENT
    await execute(
        rig,
        "UPDATE shop SET halt_reason='write_errors', halted_at=:t WHERE id=:i",
        t=NOW,
        i=p.shop.shop_id,
    )
    p.clock.now = at(30)
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.methods()[-1] == "verify_price"
    assert "get_offers" not in p.client.methods()  # решений нет
    assert (await shop_row(rig, p.shop.shop_id))["halt"] == "write_errors"
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", None)]


# --- Т48 ---


async def test_three_unknown_outcomes_halt_and_fourth_write_is_blocked(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    for index in range(3):
        result = await _unknown_write(p, 601 * index)
        assert result.outcome is WriteOutcome.UNKNOWN
        row = await shop_row(rig, p.shop.shop_id)
        assert row["write_streak"] == index + 1
    assert row["halt"] == "write_errors"
    result = await _unknown_write(p, 601 * 3)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "halted")
    assert len(p.client.sent) == 3


async def test_confirmed_write_resets_the_write_series(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.confirm import verify_open_writes

    p = await prepare(rig, redis_client, redis_prefix)
    await hold_lease(rig, p.shop, "w1", at(100_000))
    await _unknown_write(p, 0)
    await _unknown_write(p, 601)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 2
    del p.client.errors["set_price"]
    p.clock.now = at(1202)
    assert (await p.write(94)).outcome is WriteOutcome.SENT
    p.clock.now = at(1232)
    await verify_open_writes(p.sctx)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0
    await _unknown_write(p, 2000)
    await _unknown_write(p, 2601)
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["write_streak"], row["halt"]) == (2, None)


async def test_refusals_do_not_grow_the_write_series(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STOCK_COUNTED, p.sku)
    for index in range(3):
        p.clock.now = at(601 * index)
        result = await p.write(94)
        assert result.outcome is WriteOutcome.REFUSED
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["write_streak"], row["halt"]) == (0, None)


# --- Т49 ---


async def _slice_at(p: Prepared, seconds: int) -> bool:
    p.clock.now = at(seconds)
    return await run_one_slice(p.ctx, p.database, at(seconds))


async def _expired(rig: Rig, p: Prepared) -> None:
    await execute(
        rig,
        "UPDATE shop SET connection_state='reauth_required', "
        "connection_error='session_expired' WHERE id=:i",
        i=p.shop.shop_id,
    )


async def test_three_temporary_login_failures_halt_the_shop(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    await _expired(rig, p)
    p.client.login_results = [
        LoginRejectedError("TIMEOUT_EXCEEDED", 30),
        KaspiTransportError("login", "x"),
        LoginRejectedError("TIMEOUT_EXCEEDED"),
    ]
    assert await _slice_at(p, 0)
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["state"], row["login_streak"], row["claimed_until"]) == (
        "reauth_required",
        1,
        at(30),
    )
    assert not await _slice_at(p, 29)
    assert await _slice_at(p, 30)
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["login_streak"], row["claimed_until"], row["halt"]) == (2, at(330), None)
    assert await _slice_at(p, 330)
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["login_streak"], row["halt"]) == (3, "login_errors")
    assert not await _slice_at(p, 10_000)  # остановленный не берётся
    assert p.client.methods().count("login") == 3


async def test_busy_login_lock_defers_for_30_seconds(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    await _expired(rig, p)
    lock = LoginLock(redis_client, prefix=redis_prefix)
    token = await lock.acquire(p.shop.shop_id, 60_000)
    assert token is not None
    assert await _slice_at(p, 0)
    assert p.client.methods() == []
    assert (await shop_row(rig, p.shop.shop_id))["claimed_until"] == at(30)
    await lock.release(p.shop.shop_id, token)
    assert not await _slice_at(p, 29)
    assert await _slice_at(p, 30)
    assert p.client.methods() == ["login"]
    assert (await shop_row(rig, p.shop.shop_id))["state"] == "connected"


async def test_successful_login_resets_the_login_series(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    await _expired(rig, p)
    await execute(rig, "UPDATE shop SET login_error_streak=2 WHERE id=:i", i=p.shop.shop_id)
    assert await _slice_at(p, 0)
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["state"], row["login_streak"], row["halt"]) == ("connected", 0, None)
