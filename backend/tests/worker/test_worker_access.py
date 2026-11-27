"""Т50–Т59, Т69, Т70: доступ к магазину, автовход, ключи."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    SeededShop,
    code_required,
    execute,
    fetch,
    make_worker_ctx,
    open_slice_ctx,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    shop_row,
    worker_cipher,
    worker_database_url,
)

from kaspisa.db import ClaimedShop, Database
from kaspisa.kaspi import KaspiTransportError, LoginRejectedError, SessionExpiredError
from kaspisa.security import (
    DecryptionError,
    KeyRing,
    Secret,
    SecretCipher,
    SecretContext,
    SecretField,
)

EXPIRED = "UPDATE shop SET connection_state='reauth_required', connection_error=:e WHERE id=:i"


def _expired() -> SessionExpiredError:
    return SessionExpiredError("find_product")


async def _env(
    rig: Rig, redis: Redis, prefix: str, *, products: int = 1, dry_run: bool = True
) -> tuple[Any, Database, SeededShop, FakeKaspiClient, FakeClock]:
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=products)
    client = FakeKaspiClient()
    ctx = make_worker_ctx(database, client, redis, prefix, clock, dry_run=dry_run)
    return ctx, database, shop, client, clock


async def _lease(rig: Rig, shop: SeededShop) -> None:
    """Запросы кабинета идут под арендой: магазин захвачен обработчиком `w1`."""
    await execute(
        rig,
        "UPDATE shop SET claimed_by='w1', claimed_until=:u WHERE id=:i",
        u=NOW + timedelta(days=1),
        i=shop.shop_id,
    )


async def _set_expired(rig: Rig, shop: SeededShop, error: str = "session_expired") -> None:
    await execute(rig, EXPIRED, e=error, i=shop.shop_id)


# --- Т50 ---


async def test_permanent_login_refusal_marks_reauth_required(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    ctx, database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    await _set_expired(rig, shop)
    before = await shop_row(rig, shop.shop_id)
    client.login_results = [LoginRejectedError("INVALID_CREDENTIALS")]
    assert await run_one_slice(ctx, database)
    after = await shop_row(rig, shop.shop_id)
    assert (after["state"], after["error"]) == ("reauth_required", "INVALID_CREDENTIALS")
    assert after["version"] == before["version"] + 1
    assert after["login_streak"] == 0
    assert client.methods() == ["login"]
    assert not await run_one_slice(ctx, database)


# --- Т51, Т53 ---


async def test_session_expired_triggers_one_auto_login(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    ctx, database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    client.find_results = [_expired()]
    before = await shop_row(rig, shop.shop_id)
    assert await run_one_slice(ctx, database)
    assert client.methods() == ["find_product", "login"]
    assert [c.login.reveal() for c in client.credentials] == ["l"]
    assert [c.password.reveal() for c in client.credentials] == ["p"]
    after = await shop_row(rig, shop.shop_id)
    assert after["state"] == "connected"
    assert after["version"] == before["version"] + 2  # reauth_required, затем connected
    assert after["unproven"] == after["version"]
    assert after["claimed_by"] is None
    assert await run_one_slice(ctx, database)  # следующий слайс работает по новой сессии
    assert client.methods()[2:4] == ["find_product", "get_offers"]


async def test_first_good_request_clears_unproven_and_allows_second_auto_login(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    ctx, database, shop, client, clock = await _env(rig, redis_client, redis_prefix)
    client.find_results = [_expired()]
    await run_one_slice(ctx, database)
    assert (await shop_row(rig, shop.shop_id))["unproven"] is not None
    await run_one_slice(ctx, database)
    assert (await shop_row(rig, shop.shop_id))["unproven"] is None
    clock.advance(61)
    client.find_results = [_expired()]
    await run_one_slice(ctx, database)
    assert client.methods().count("login") == 2
    assert (await shop_row(rig, shop.shop_id))["state"] == "connected"


# --- Т52 ---


async def test_expired_right_after_auto_login_needs_seller(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    ctx, database, shop, client, clock = await _env(rig, redis_client, redis_prefix)
    client.find_results = [_expired(), _expired()]
    await run_one_slice(ctx, database)
    await run_one_slice(ctx, database)
    row = await shop_row(rig, shop.shop_id)
    assert (row["state"], row["error"]) == ("reauth_required", "session_expired_after_login")
    assert client.methods().count("login") == 1
    assert not await run_one_slice(ctx, database)
    other = make_worker_ctx(rig.new_database(), client, redis_client, redis_prefix, clock)
    assert not await run_one_slice(other, other.database)
    assert client.methods().count("login") == 1


# --- Т54 ---


async def test_two_instances_expire_together_one_login(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.access import ShopAccess
    from kaspisa.worker.errors import ShopUnavailable

    ctx, _database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    await _lease(rig, shop)
    first = await ShopAccess.open(ctx, shop.tenant_id, shop.shop_id)
    second = await ShopAccess.open(ctx, shop.tenant_id, shop.shop_id)
    barrier = asyncio.Barrier(2)

    async def meet(method: str, key: str) -> None:
        if method == "find_product":
            await barrier.wait()

    client.on_call = meet
    client.errors["find_product"] = _expired()

    async def probe(access: ShopAccess) -> None:
        await access.cabinet(lambda s: client.find_product(s, shop.merchant_uid, "x"))

    results = await asyncio.gather(probe(first), probe(second), return_exceptions=True)
    assert all(isinstance(r, ShopUnavailable) for r in results)
    assert client.methods().count("login") == 1
    assert (await shop_row(rig, shop.shop_id))["state"] == "connected"


# --- Т55 ---


async def test_code_required_leaves_awaiting_code_and_stops(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    ctx, database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    await _set_expired(rig, shop)
    client.login_results = [code_required()]
    await run_one_slice(ctx, database)
    assert client.methods() == ["login"]
    assert (await shop_row(rig, shop.shop_id))["state"] == "awaiting_code"
    assert not await run_one_slice(ctx, database)
    assert client.methods() == ["login"]


# --- временные отказы входа (серия — часть 4, здесь пауза) ---


async def test_temporary_login_refusal_defers_and_counts(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    ctx, database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    await _set_expired(rig, shop)
    client.login_results = [
        LoginRejectedError("TIMEOUT_EXCEEDED", 30),
        KaspiTransportError("l", "x"),
    ]
    await run_one_slice(ctx, database)
    row = await shop_row(rig, shop.shop_id)
    assert (row["state"], row["error"], row["login_streak"]) == (
        "reauth_required",
        "session_expired",
        1,
    )
    assert row["claimed_until"] == NOW + timedelta(seconds=30)
    assert not await run_one_slice(ctx, database, NOW + timedelta(seconds=29))
    assert await run_one_slice(ctx, database, NOW + timedelta(seconds=30))
    assert client.methods().count("login") == 2
    row = await shop_row(rig, shop.shop_id)
    assert row["login_streak"] == 2
    assert row["claimed_until"] == NOW + timedelta(seconds=300)  # без retry_after: 300 с


# --- Т56 ---


async def test_session_saved_only_when_changed(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.access import ShopAccess

    ctx, _database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)

    async def blob() -> bytes:
        return bytes(
            (await fetch(rig, "SELECT session_enc FROM shop WHERE id=:i", i=shop.shop_id))[0][0]
        )

    await _lease(rig, shop)
    access = await ShopAccess.open(ctx, shop.tenant_id, shop.shop_id)
    first = await blob()
    await access.cabinet(lambda s: client.find_product(s, shop.merchant_uid, "x"))
    assert await blob() == first  # changed=False: не сохраняется
    client.session_changed = True
    await access.cabinet(lambda s: client.find_product(s, shop.merchant_uid, "x"))
    saved = await blob()
    assert saved != first
    assert access.session is not None and access.session.changed is False
    await access.cabinet(lambda s: client.get_product(s, shop.merchant_uid, "x"))
    assert access.session.changed is False


async def test_stale_session_save_does_not_break_the_slice(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.access import ShopAccess

    ctx, _database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    await _lease(rig, shop)
    access = await ShopAccess.open(ctx, shop.tenant_id, shop.shop_id)
    client.session_changed = True
    await execute(
        rig,
        "UPDATE shop SET connection_version = connection_version + 1 WHERE id=:i",
        i=shop.shop_id,
    )
    listing = await access.cabinet(lambda s: client.find_product(s, shop.merchant_uid, "x"))
    assert listing is not None


# --- Т57, Т69 ---

FOREIGN = SecretCipher(KeyRing(9, {9: bytes(32)}))


async def _break_key(rig: Rig, shop: SeededShop, foreign: SecretCipher = FOREIGN) -> None:
    blob = foreign.encrypt(
        Secret("v"), SecretContext(shop.tenant_id, shop.shop_id, SecretField.SHOP_SESSION)
    )
    await execute(rig, "UPDATE shop SET session_enc=:b WHERE id=:i", b=blob, i=shop.shop_id)


async def test_unknown_key_isolated_shop_is_deferred_others_continue(
    rig: Rig, redis_client: Redis, redis_prefix: str, caplog: pytest.LogCaptureFixture
) -> None:
    ctx, database, a, _client, _clock = await _env(rig, redis_client, redis_prefix)
    b = await seed_shop(rig, database)
    await _break_key(rig, a)
    before = await shop_row(rig, a.shop_id)
    with caplog.at_level(logging.DEBUG):
        while await run_one_slice(ctx, database):
            pass
    after = await shop_row(rig, a.shop_id)
    for key in ("state", "version", "halt", "login_streak", "write_streak", "error"):
        assert after[key] == before[key], key
    assert after["claimed_until"] == NOW + timedelta(seconds=600)
    assert (await fetch(rig, "SELECT last_checked_at FROM product WHERE shop_id=:i", i=b.shop_id))[
        0
    ][0] is not None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [("unknown_key_isolated" in r.getMessage()) for r in errors] == [True]
    assert "marker-" not in caplog.text


async def test_unknown_key_shared_by_two_shops_is_flagged(
    rig: Rig, redis_client: Redis, redis_prefix: str, caplog: pytest.LogCaptureFixture
) -> None:
    ctx, database, a, client, _clock = await _env(rig, redis_client, redis_prefix)
    b = await seed_shop(rig, database)
    await _break_key(rig, a)
    await _break_key(rig, b)
    with caplog.at_level(logging.DEBUG):
        while await run_one_slice(ctx, database):
            pass
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(messages) == 2
    assert all("unknown_key_shared" in m for m in messages)
    assert client.calls == []


async def test_unknown_key_shop_is_claimed_once_per_pause(
    rig: Rig, redis_client: Redis, redis_prefix: str, caplog: pytest.LogCaptureFixture
) -> None:
    ctx, database, a, client, clock = await _env(rig, redis_client, redis_prefix)
    healthy = [await seed_shop(rig, database) for _ in range(5)]
    await _break_key(rig, a)
    second = make_worker_ctx(
        rig.new_database(), client, redis_client, redis_prefix, clock, worker_id="w2"
    )
    with caplog.at_level(logging.DEBUG):
        while await run_one_slice(ctx, database):
            pass
        for step in (299, 300):
            clock.advance(step)
            for worker in (ctx, second):
                while await run_one_slice(worker, worker.database):
                    pass
    isolated = [r for r in caplog.records if "unknown_key_isolated" in r.getMessage()]
    assert len(isolated) == 1
    for shop in healthy:
        rows = await fetch(
            rig, "SELECT last_checked_at FROM product WHERE shop_id=:i", i=shop.shop_id
        )
        assert rows[0][0] is not None


# --- Т58 ---


async def test_corrupted_block_marks_secret_unreadable(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    ctx, database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    rows = await fetch(rig, "SELECT session_enc FROM shop WHERE id=:i", i=shop.shop_id)
    broken = bytearray(rows[0][0])
    broken[-1] ^= 1
    await execute(
        rig, "UPDATE shop SET session_enc=:b WHERE id=:i", b=bytes(broken), i=shop.shop_id
    )
    before = await shop_row(rig, shop.shop_id)
    await run_one_slice(ctx, database)
    after = await shop_row(rig, shop.shop_id)
    assert (after["state"], after["error"]) == ("reauth_required", "secret_unreadable")
    assert after["version"] == before["version"] + 1
    assert client.calls == []


async def test_corrupted_block_with_concurrent_change_is_discarded(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kaspisa.db.repositories import ShopRepository

    ctx, database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)

    async def broken(self: ShopRepository, shop_id: uuid.UUID) -> Secret:
        await execute(
            rig,
            "UPDATE shop SET connection_version = connection_version + 1, bot_mode = 'off' "
            "WHERE id=:i",
            i=shop_id,
        )
        raise DecryptionError

    monkeypatch.setattr(ShopRepository, "get_session", broken)
    await run_one_slice(ctx, database)
    row = await shop_row(rig, shop.shop_id)
    assert (row["state"], row["error"]) == ("connected", None)  # состояние не затёрто
    assert client.calls == []


# --- Т59, Т70 ---


@pytest.mark.parametrize(
    ("state", "error"),
    [
        ("not_connected", None),
        ("awaiting_code", None),
        ("reauth_required", "INVALID_CREDENTIALS"),
        ("reauth_required", "session_expired_after_login"),
    ],
)
async def test_other_states_make_no_requests(
    state: str, error: str | None, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.slice import run_shop_slice

    ctx, database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    await execute(
        rig,
        "UPDATE shop SET connection_state=:s, connection_error=:e WHERE id=:i",
        s=state,
        e=error,
        i=shop.shop_id,
    )
    before = await shop_row(rig, shop.shop_id)
    await run_shop_slice(ctx, ClaimedShop(tenant_id=shop.tenant_id, shop_id=shop.shop_id))
    assert client.calls == []
    assert await shop_row(rig, shop.shop_id) == before
    if state == "reauth_required":
        assert not await run_one_slice(ctx, database)  # claim_shop его не отдаёт


@pytest.mark.parametrize(("mode", "logs_in"), [("off", False), ("dry_run", True), ("live", True)])
async def test_auto_login_depends_on_bot_mode(
    mode: str, logs_in: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т12: автовход при `session_expired` идёт в `dry_run` и `live`, в `off` его нет."""
    from kaspisa.worker.slice import run_shop_slice

    ctx, _database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    await _set_expired(rig, shop)
    await execute(rig, "UPDATE shop SET bot_mode=:m WHERE id=:i", m=mode, i=shop.shop_id)
    # прямой слайс (без захвата): решение об автовходе принимает сам обработчик
    await run_shop_slice(ctx, ClaimedShop(tenant_id=shop.tenant_id, shop_id=shop.shop_id))
    assert client.methods() == (["login"] if logs_in else [])
    state = (await shop_row(rig, shop.shop_id))["state"]
    assert state == ("connected" if logs_in else "reauth_required")


async def test_expired_shop_is_claimed_only_when_bot_runs(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    ctx, database, shop, client, _clock = await _env(rig, redis_client, redis_prefix)
    await _set_expired(rig, shop)
    await execute(
        rig,
        "UPDATE shop SET halt_reason='write_errors', halted_at=now() WHERE id=:i",
        i=shop.shop_id,
    )
    assert not await run_one_slice(ctx, database)
    await execute(
        rig, "UPDATE shop SET halt_reason=NULL, halted_at=NULL WHERE id=:i", i=shop.shop_id
    )
    assert await run_one_slice(ctx, database)
    assert client.methods() == ["login"]
    assert (await shop_row(rig, shop.shop_id))["state"] == "connected"
