"""Т2–Т6: захват магазина (`SystemScope.claim_shop`) и аренда."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from db_worker_seed import (
    NOW,
    ago,
    insert_product,
    insert_shop,
    insert_tenant,
    insert_write,
)
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from kaspisa.db import Database, TenantId, create_database
from kaspisa.security import SecretCipher
from kaspisa.worker.constants import UNBOUNDED_RECHECK_SECONDS

LEASE = 180
RECHECK = 60
UNBOUNDED_RECHECK = UNBOUNDED_RECHECK_SECONDS


async def _claim(
    database: Database, *, worker: str = "w1", now: datetime = NOW
) -> tuple[uuid.UUID, uuid.UUID] | None:
    async with database.system() as system:
        claimed = await system.claim_shop(
            worker_id=worker,
            now=now,
            lease_seconds=LEASE,
            recheck_seconds=RECHECK,
            unbounded_recheck_seconds=UNBOUNDED_RECHECK,
        )
    return None if claimed is None else (claimed.tenant_id, claimed.shop_id)


REJECTED: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "not_tracked": ({}, {"tracked": False}),
    "no_min_checked_599s_ago": ({}, {"min_price": None, "last_checked_at": ago(599)}),
    "no_max_checked_599s_ago": ({}, {"max_price": None, "last_checked_at": ago(599)}),
    "checked_59s_ago": ({}, {"last_checked_at": ago(59)}),
    "retry_in_future": ({}, {"retry_at": NOW + timedelta(seconds=1)}),
    "not_connected": ({"state": "not_connected"}, {}),
    "awaiting_code": ({"state": "awaiting_code"}, {}),
    "reauth_other_error": ({"state": "reauth_required", "error": "INVALID_CREDENTIALS"}, {}),
    "reauth_without_error": ({"state": "reauth_required"}, {}),
    "reauth_after_login": (
        {"state": "reauth_required", "error": "session_expired_after_login"},
        {},
    ),
    "bot_disabled": ({"bot_mode": "off"}, {}),
    "halted": ({"halt_reason": "write_errors"}, {}),
    "no_merchant_uid": (
        {"state": "reauth_required", "error": "session_expired", "merchant_uid": None},
        {},
    ),
    "no_city": ({"city_id": None}, {}),
    "reauth_expired_bot_disabled": (
        {"state": "reauth_required", "error": "session_expired", "bot_mode": "off"},
        {},
    ),
    "reauth_expired_halted": (
        {"state": "reauth_required", "error": "session_expired", "halt_reason": "login_errors"},
        {},
    ),
}

ACCEPTED: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "checked_60s_ago": ({}, {"last_checked_at": ago(60)}),
    "never_checked": ({}, {"last_checked_at": None}),
    "no_min_never_checked": ({}, {"min_price": None}),
    "no_max_never_checked": ({}, {"max_price": None}),
    "retry_due_exactly_now": ({}, {"retry_at": NOW}),
    "retry_due_earlier": ({}, {"retry_at": ago(1)}),
    "session_expired": ({"state": "reauth_required", "error": "session_expired"}, {}),
    "mode_dry_run": ({"bot_mode": "dry_run"}, {}),
    "mode_live": ({"bot_mode": "live"}, {}),
    "session_expired_dry_run": (
        {"state": "reauth_required", "error": "session_expired", "bot_mode": "dry_run"},
        {},
    ),
}


@pytest.mark.parametrize("name", sorted(REJECTED))
async def test_claim_rejects(name: str, database: Database, connection: AsyncConnection) -> None:
    shop_kwargs, product_kwargs = REJECTED[name]
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, **shop_kwargs)
    await insert_product(connection, tenant, shop, **product_kwargs)
    assert await _claim(database) is None


@pytest.mark.parametrize("name", sorted(ACCEPTED))
async def test_claim_accepts(name: str, database: Database, connection: AsyncConnection) -> None:
    shop_kwargs, product_kwargs = ACCEPTED[name]
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, **shop_kwargs)
    await insert_product(connection, tenant, shop, **product_kwargs)
    assert await _claim(database) == (tenant, shop)


_NO_BOUNDS: dict[str, Any] = {"min_price": None, "max_price": None}

# Т44, Т52: товар без границ захватывает магазин при включённом боте, при «выключен» - нет.
UNBOUNDED_CLAIMED: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "dry_run_never_checked": ({"bot_mode": "dry_run"}, {**_NO_BOUNDS, "last_checked_at": None}),
    "dry_run_checked_600s_ago": (
        {"bot_mode": "dry_run"},
        {**_NO_BOUNDS, "last_checked_at": ago(600)},
    ),
    "live_checked_600s_ago": ({"bot_mode": "live"}, {**_NO_BOUNDS, "last_checked_at": ago(600)}),
}

UNBOUNDED_REJECTED: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "off_never_checked": ({"bot_mode": "off"}, {**_NO_BOUNDS, "last_checked_at": None}),
    "off_checked_600s_ago": ({"bot_mode": "off"}, {**_NO_BOUNDS, "last_checked_at": ago(600)}),
    "dry_run_checked_599s_ago": (
        {"bot_mode": "dry_run"},
        {**_NO_BOUNDS, "last_checked_at": ago(599)},
    ),
    "off_only_bounded_never_checked": ({"bot_mode": "off"}, {"last_checked_at": None}),
    "halted": ({"bot_mode": "dry_run", "halt_reason": "write_errors"}, _NO_BOUNDS),
    "no_city": ({"bot_mode": "dry_run", "city_id": None}, _NO_BOUNDS),
    "off_reauth_session_expired": (
        {"bot_mode": "off", "state": "reauth_required", "error": "session_expired"},
        _NO_BOUNDS,
    ),
}


@pytest.mark.parametrize("name", sorted(UNBOUNDED_CLAIMED))
async def test_claim_for_unbounded_products(
    name: str, database: Database, connection: AsyncConnection
) -> None:
    shop_kwargs, product_kwargs = UNBOUNDED_CLAIMED[name]
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, **shop_kwargs)
    await insert_product(connection, tenant, shop, **product_kwargs)
    assert await _claim(database) == (tenant, shop)


@pytest.mark.parametrize("name", sorted(UNBOUNDED_REJECTED))
async def test_claim_rejects_for_unbounded_products(
    name: str, database: Database, connection: AsyncConnection
) -> None:
    shop_kwargs, product_kwargs = UNBOUNDED_REJECTED[name]
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, **shop_kwargs)
    await insert_product(connection, tenant, shop, **product_kwargs)
    assert await _claim(database) is None


async def test_claim_sets_lease(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    await insert_product(connection, tenant, shop)
    assert await _claim(database, worker="w7") == (tenant, shop)
    async with database.tenant(TenantId(tenant)) as scope:
        loaded = await scope.shops.get(shop)
        assert loaded is not None
        assert (loaded.claimed_by, loaded.claimed_until) == ("w7", NOW + timedelta(seconds=LEASE))


async def test_claim_order_oldest_first(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shops = {}
    for label, last in (("d100", ago(100)), ("d300", ago(300)), ("never", None)):
        shop = await insert_shop(connection, tenant)
        await insert_product(connection, tenant, shop, last_checked_at=last)
        shops[label] = shop
    order = []
    for _ in range(3):
        claimed = await _claim(database)
        assert claimed is not None
        order.append(claimed[1])
    assert order == [shops["never"], shops["d300"], shops["d100"]]
    assert await _claim(database) is None


async def test_claim_order_uses_verification_event(
    database: Database, connection: AsyncConnection
) -> None:
    """Порядок — по самой давней дате из событий: проверка записи старше товара."""
    tenant = await insert_tenant(connection)
    shop_a = await insert_shop(connection, tenant)
    await insert_product(connection, tenant, shop_a, last_checked_at=ago(100))
    shop_b = await insert_shop(connection, tenant)
    product_b = await insert_product(connection, tenant, shop_b, last_checked_at=ago(70))
    await insert_write(connection, tenant, shop_b, product_b, next_verify_at=ago(500))
    first = await _claim(database)
    assert first is not None
    assert first[1] == shop_b


@pytest.fixture
async def two_databases(
    database_url: str, cipher: SecretCipher
) -> AsyncIterator[tuple[Database, Database]]:
    first = create_database(database_url, cipher=cipher)
    second = create_database(database_url, cipher=cipher)
    try:
        yield first, second
    finally:
        await first.dispose()
        await second.dispose()


@pytest.fixture
async def seeder(
    database_url: str,
) -> AsyncIterator[Callable[[int], Awaitable[set[uuid.UUID]]]]:
    """Сидирование с настоящими commit; в конце все арендаторы удаляются каскадом."""
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)

    async def seed(count: int) -> set[uuid.UUID]:
        shops: set[uuid.UUID] = set()
        async with engine.begin() as conn:
            for _ in range(count):
                tenant = await insert_tenant(conn)
                shop = await insert_shop(conn, tenant)
                await insert_product(conn, tenant, shop)
                shops.add(shop)
        return shops

    try:
        yield seed
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenant"))
        await engine.dispose()


async def test_concurrent_claims_do_not_overlap(
    two_databases: tuple[Database, Database],
    seeder: Callable[[int], Awaitable[set[uuid.UUID]]],
) -> None:
    """Т4: два движка, по 10 одновременных захватов; множества не пересекаются."""
    expected: set[uuid.UUID] = await seeder(20)
    barrier = asyncio.Barrier(20)

    async def one(database: Database, worker: str) -> uuid.UUID | None:
        await barrier.wait()
        claimed = await _claim(database, worker=worker)
        return None if claimed is None else claimed[1]

    results = await asyncio.gather(*[one(two_databases[i % 2], f"w{i % 2}") for i in range(20)])
    got = [r for r in results if r is not None]
    assert len(got) == len(set(got)) == 20
    assert set(got) == expected


async def test_lease_blocks_until_expiry(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    await insert_product(connection, tenant, shop)
    assert await _claim(database, worker="w1") == (tenant, shop)
    assert await _claim(database, worker="w2", now=NOW + timedelta(seconds=LEASE - 1)) is None
    assert await _claim(database, worker="w2", now=NOW + timedelta(seconds=LEASE)) == (
        tenant,
        shop,
    )
    async with database.tenant(TenantId(tenant)) as scope:
        loaded = await scope.shops.get(shop)
        assert loaded is not None
        assert loaded.claimed_by == "w2"


async def test_lease_operations_only_for_owner(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    await insert_product(connection, tenant, shop)
    await _claim(database, worker="w1")
    until = NOW + timedelta(seconds=600)
    async with database.tenant(TenantId(tenant)) as scope:
        assert await scope.shops.release_lease(shop, "w2") is False
        assert await scope.shops.renew_lease(shop, "w2", until) is False
        assert await scope.shops.defer(shop, "w2", until) is False
        await scope.commit()
        loaded = await scope.shops.get(shop)
        assert loaded is not None
        assert (loaded.claimed_by, loaded.claimed_until) == ("w1", NOW + timedelta(seconds=LEASE))

        assert await scope.shops.renew_lease(shop, "w1", until) is True
        await scope.commit()
        assert await _claim(database, worker="w2", now=NOW + timedelta(seconds=599)) is None

        assert await scope.shops.release_lease(shop, "w1") is True
        await scope.commit()
        assert await scope.shops.release_lease(shop, "w1") is False
    assert await _claim(database, worker="w2") == (tenant, shop)


async def test_defer_keeps_owner_and_blocks_everyone(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    await insert_product(connection, tenant, shop)
    await _claim(database, worker="w1")
    until = NOW + timedelta(seconds=600)
    async with database.tenant(TenantId(tenant)) as scope:
        assert await scope.shops.defer(shop, "w1", until) is True
        await scope.commit()
        loaded = await scope.shops.get(shop)
        assert loaded is not None
        assert (loaded.claimed_by, loaded.claimed_until) == ("w1", until)
    for worker in ("w1", "w2"):
        assert await _claim(database, worker=worker, now=until - timedelta(seconds=1)) is None
    assert await _claim(database, worker="w2", now=until) == (tenant, shop)


async def test_other_tenant_cannot_touch_lease(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    await insert_product(connection, tenant, shop)
    await _claim(database, worker="w1")
    until = NOW + timedelta(seconds=600)
    async with database.tenant(TenantId(other)) as scope:
        assert await scope.shops.release_lease(shop, "w1") is False
        assert await scope.shops.renew_lease(shop, "w1", until) is False
        assert await scope.shops.defer(shop, "w1", until) is False


@pytest.mark.parametrize("variant", ["bot_disabled", "halted"])
async def test_halted_shop_claimed_only_for_verification(
    variant: str, database: Database, connection: AsyncConnection
) -> None:
    """Т6: остановленный магазин отдаётся только ради проверки открытой записи."""
    kwargs: dict[str, Any] = (
        {"bot_mode": "off"} if variant == "bot_disabled" else {"halt_reason": "write_errors"}
    )
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, **kwargs)
    product = await insert_product(connection, tenant, shop)
    assert await _claim(database) is None
    await insert_write(connection, tenant, shop, product, next_verify_at=NOW)
    assert await _claim(database) == (tenant, shop)


async def test_future_verification_does_not_claim(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, bot_mode="off")
    product = await insert_product(connection, tenant, shop)
    await insert_write(connection, tenant, shop, product, next_verify_at=NOW + timedelta(seconds=1))
    assert await _claim(database) is None


@pytest.mark.parametrize(
    "kwargs",
    [{"bot_mode": "off"}, {"halt_reason": "login_errors"}],
    ids=["bot_disabled", "halted"],
)
async def test_expired_session_shop_without_permission_is_not_claimed_for_verification(
    kwargs: dict[str, Any], database: Database, connection: AsyncConnection
) -> None:
    """Т70: вход в `reauth_required` разрешён только при включённом боте и без остановки.

    Открытая запись не повод: проверять её без сессии нельзя, а входить боту запрещено.
    """
    tenant = await insert_tenant(connection)
    shop = await insert_shop(
        connection, tenant, state="reauth_required", error="session_expired", **kwargs
    )
    product = await insert_product(connection, tenant, shop)
    await insert_write(connection, tenant, shop, product, next_verify_at=NOW)
    assert await _claim(database) is None
