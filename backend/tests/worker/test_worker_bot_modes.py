"""ADR 0024, Т3-Т8а, Т12а: режим магазина, предохранитель сервера, смена режима, флаг."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
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
    seed_shop,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, prepare

from kaspisa.db import ClaimedShop
from kaspisa.worker import runner
from kaspisa.worker.runner import Worker


async def _in_mode(
    rig: Rig, redis: Redis, prefix: str, mode: str, *, dry_run: bool = False
) -> Prepared:
    database = rig.new_database()
    shop = await seed_shop(rig, database, bot_mode=mode)
    return await prepare(rig, redis, prefix, dry_run=dry_run, database=database, shop=shop)


async def _no_cabinet_calls(p: Prepared) -> None:
    assert p.client.calls == []
    assert p.client.sent == []
    assert await fetch(p.rig, "SELECT 1 FROM price_write") == []


# --- Т3 ---


async def test_live_without_server_ban_sends_price_once(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.writer import WriteOutcome

    p = await _in_mode(rig, redis_client, redis_prefix, "live")
    result = await p.write(94)
    assert result.outcome is WriteOutcome.SENT
    assert len(p.client.sent) == 1
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "unconfirmed", None)]


# --- Т4, Т5, Т6 ---


@pytest.mark.parametrize(
    ("mode", "server_dry_run"), [("dry_run", False), ("live", True), ("dry_run", True)]
)
async def test_no_send_without_live_and_open_server(
    mode: str, server_dry_run: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.writer import WriteOutcome

    p = await _in_mode(rig, redis_client, redis_prefix, mode, dry_run=server_dry_run)
    result = await p.write(94)
    assert result.outcome is WriteOutcome.DRY_RUN
    assert p.client.methods() == []  # ни get_product, ни find_product, ни set_price
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "dry_run", None)]
    await _no_cabinet_calls(p)


# --- Т7 ---


async def test_off_mode_checks_nothing(rig: Rig, redis_client: Redis, redis_prefix: str) -> None:
    from kaspisa.worker.slice import run_shop_slice

    p = await _in_mode(rig, redis_client, redis_prefix, "off")
    assert not await run_one_slice(p.ctx, p.database)  # цены проверять нечего: захвата нет
    await run_shop_slice(p.ctx, ClaimedShop(tenant_id=p.shop.tenant_id, shop_id=p.shop.shop_id))
    assert p.client.methods() == []
    assert await journal(rig, p.shop.shop_id) == []
    sql = "SELECT last_checked_at FROM product WHERE shop_id=:s"
    rows = await fetch(rig, sql, s=p.shop.shop_id)
    assert rows == [(None,)]


# --- Т8 ---


@pytest.mark.parametrize(("in_db", "reason"), [("dry_run", "not_live"), ("off", "bot_disabled")])
async def test_decisive_check_uses_locked_shop_row(
    in_db: str, reason: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Слайс видел `live`, к `_reserve` в базе другой режим: отправки и строк журнала нет."""
    from kaspisa.worker.writer import WriteOutcome

    p = await _in_mode(rig, redis_client, redis_prefix, "live")
    assert p.sctx.shop.bot_mode == "live"
    await execute(rig, "UPDATE shop SET bot_mode=:m WHERE id=:s", m=in_db, s=p.shop.shop_id)
    result = await p.write(94)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, reason)
    assert "set_price" not in p.client.methods()
    assert p.client.sent == []
    assert await journal(rig, p.shop.shop_id) == []
    assert await fetch(rig, "SELECT 1 FROM price_write") == []


# --- Т8а ---


async def test_mode_switched_after_reserve_does_not_cancel_reserved_write(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.writer import WriteOutcome

    p = await _in_mode(rig, redis_client, redis_prefix, "live")

    async def switch_off(method: str, key: str) -> None:
        if method == "set_price":
            await execute(rig, "UPDATE shop SET bot_mode='off' WHERE id=:s", s=p.shop.shop_id)

    p.client.on_call = switch_off
    result = await p.write(94)
    assert result.outcome is WriteOutcome.SENT
    assert len(p.client.sent) == 1
    assert [row[3] for row in await journal(rig, p.shop.shop_id)] == ["unconfirmed"]
    assert len(await fetch(rig, "SELECT 1 FROM price_write")) == 1
    # следующий слайс у `off` цены не проверяет: только подтверждает открытую запись
    p.client.calls.clear()
    p.clock.advance(100)
    await run_one_slice(p.ctx, p.database)
    assert "get_offers" not in p.client.methods()
    assert "set_price" not in p.client.methods()


# --- Т12а ---


async def _wait_key(redis: Redis, key: str, expected: bytes) -> None:
    async with asyncio.timeout(10):
        while await redis.get(key) != expected:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(("server_dry_run", "value"), [(False, b"0"), (True, b"1")])
async def test_worker_publishes_its_dry_run_flag(
    server_dry_run: bool, value: bytes, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _in_mode(rig, redis_client, redis_prefix, "off", dry_run=server_dry_run)
    key = f"{redis_prefix}:worker:dry_run"
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(p.ctx, idle_sleep=0.05).run(stop))
    try:
        await _wait_key(redis_client, key, value)
        assert 0 < await redis_client.ttl(key) <= 120
        # не реже раза в 30 секунд по часам обработчика
        await redis_client.delete(key)
        p.clock.advance(31)
        await _wait_key(redis_client, key, value)
    finally:
        stop.set()
        async with asyncio.timeout(5):
            await task


async def test_redis_failure_on_publish_does_not_stop_the_loop(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise RedisError("down")

    monkeypatch.setattr(runner, "publish_dry_run", broken)
    p = await _in_mode(rig, redis_client, redis_prefix, "dry_run", dry_run=True)
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(p.ctx, idle_sleep=0.05).run(stop))
    try:
        async with asyncio.timeout(10):
            while "get_offers" not in p.client.methods():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        async with asyncio.timeout(5):
            await task
