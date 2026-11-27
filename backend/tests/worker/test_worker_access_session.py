"""Т77: сессия сохраняется на 429; сбой снятия блокировки входа не откладывает магазин."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from datetime import timedelta

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from worker_testing import (
    NOW,
    Rig,
    execute,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    shop_row,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, hold_lease, prepare

from kaspisa.kaspi import KaspiSession, RateLimitedError
from kaspisa.worker.errors import ShopThrottled
from kaspisa.worker.redis_limits import LoginLock


async def _saved_session(p: Prepared) -> KaspiSession:
    async with p.database.tenant(p.shop.tenant_id) as scope:
        blob = await scope.shops.get_session(p.shop.shop_id)
    assert blob is not None
    return KaspiSession.load(blob)


async def test_session_is_saved_on_rate_limit_of_a_read(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    await hold_lease(rig, p.shop, "w1", NOW + timedelta(days=1))
    session = p.sctx.access.session
    assert session is not None

    async def limited(s: KaspiSession) -> None:
        s.changed = True
        raise RateLimitedError("find_product", 10)

    with pytest.raises(ShopThrottled):
        await p.sctx.access.cabinet(limited)
    assert session.changed is False  # сохранена и сброшена


async def test_session_is_saved_on_rate_limit_of_a_write(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    session = p.sctx.access.session
    assert session is not None

    async def limited(s: KaspiSession) -> None:
        s.changed = True
        raise RateLimitedError("set_price", 10)

    with pytest.raises(RateLimitedError):
        await p.sctx.access.cabinet_write(limited)
    assert session.changed is False


async def test_failed_login_lock_release_does_not_defer_the_shop(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    await execute(
        rig,
        "UPDATE shop SET connection_state='reauth_required', "
        "connection_error='session_expired' WHERE id=:i",
        i=p.shop.shop_id,
    )

    async def broken(self: LoginLock, shop_id: object, token: str) -> bool:
        raise RedisConnectionError("упал после входа")

    monkeypatch.setattr(LoginLock, "release", broken)
    assert await run_one_slice(p.ctx, p.database, NOW)
    row = await shop_row(rig, p.shop.shop_id)
    assert row["state"] == "connected"  # результат входа сохранён
    assert (row["claimed_by"], row["claimed_until"]) == (None, None)  # без отсрочки в 300 с
