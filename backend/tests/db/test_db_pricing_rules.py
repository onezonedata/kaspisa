"""ADR 0020, Т39-Т42: решение товара, журнал `no_change`, миграция 0007."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    new_test_db_name,
    sqlalchemy_url,
)
from db_worker_seed import NOW, ago, insert_change, insert_product, insert_shop, insert_tenant
from sqlalchemy import NullPool, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from alembic import command
from kaspisa.db import Database, NotFoundError, SkipReason, TenantId


def _t(tenant: uuid.UUID) -> TenantId:
    return TenantId(tenant)


async def _insert_no_change(
    conn: AsyncConnection,
    tenant: uuid.UUID,
    shop: uuid.UUID,
    product: int,
    *,
    created_at: Any = NOW,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO price_change (tenant_id, shop_id, product_id, created_at, old_price, "
            "new_price, reason, result) "
            "VALUES (:t, :s, :p, :c, 100, 100, 'out_of_bounds', 'no_change')"
        ),
        {"t": tenant, "s": shop, "p": product, "c": created_at},
    )


# --- Т39 ---


async def test_no_change_rows_are_not_counted_as_sent(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    for seconds in (30, 20, 10):
        await _insert_no_change(connection, tenant, shop, product, created_at=ago(seconds))
    async with database.tenant(_t(tenant)) as scope:
        repo = scope.price_changes
        assert await repo.count_sent_since(shop, ago(100)) == 0
        assert await repo.has_sent_since(product, ago(100)) is False
        assert await repo.quota_free_at(shop, now=NOW, limit=1, window_seconds=100) is None
    await insert_change(connection, tenant, shop, product, result="applied", created_at=ago(5))
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.price_changes.count_sent_since(shop, ago(100)) == 1


# --- Т40 ---


async def _migration_state(url: str, sql: str) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            return [tuple(row) for row in (await conn.execute(text(sql))).all()]
    finally:
        await engine.dispose()


async def _seed_0006(url: str) -> dict[str, uuid.UUID]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    shops: dict[str, uuid.UUID] = {}
    try:
        async with engine.begin() as conn:
            tenant = uuid.uuid4()
            await conn.execute(
                text("INSERT INTO tenant (id, name) VALUES (:i, 't')"), {"i": tenant}
            )
            for name, below_min, bot in (
                ("s1", "do_nothing", True),
                ("s2", "next_place", True),
                ("s3", "drop_to_min", True),
                ("s4", "next_place", False),
            ):
                shop = uuid.uuid4()
                shops[name] = shop
                await conn.execute(
                    text(
                        "INSERT INTO shop (id, tenant_id, login_enc, password_enc, below_min, "
                        "bot_enabled) VALUES (:i, :t, :b, :b, :m, :bot)"
                    ),
                    {"i": shop, "t": tenant, "b": b"x", "m": below_min, "bot": bot},
                )
            await conn.execute(
                text(
                    "INSERT INTO product (tenant_id, shop_id, sku, master_sku, title, step) "
                    "VALUES (:t, :s, 'sku', 'm', 'Товар', 1)"
                ),
                {"t": tenant, "s": shops["s1"]},
            )
    finally:
        await engine.dispose()
    return shops


async def _insert_no_change_raw(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO price_change (tenant_id, shop_id, product_id, old_price, "
                    "new_price, reason, result) SELECT tenant_id, shop_id, id, 100, 100, "
                    "'out_of_bounds', 'no_change' FROM product"
                )
            )
    finally:
        await engine.dispose()


_SHOP_COLUMNS = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'shop' AND column_name = 'below_min'"
)
_DECISION_COLUMNS = (
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'product' "
    "AND column_name IN ('decision_reason', 'decision_target_price', 'decision_write') "
    "ORDER BY column_name"
)


def test_migration_0007_keeps_bots_and_downgrades() -> None:
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "0006")
        shops = asyncio.run(_seed_0006(url))
        command.upgrade(config, "0007")  # закреплено: позднее `bot_enabled` заменён (ADR 0024)
        bots = dict(asyncio.run(_migration_state(url, "SELECT id, bot_enabled FROM shop")))
        assert [bots[shops[n]] for n in ("s1", "s2", "s3", "s4")] == [True, True, True, False]
        assert asyncio.run(_migration_state(url, _SHOP_COLUMNS)) == []
        assert asyncio.run(_migration_state(url, _DECISION_COLUMNS)) == [
            ("decision_reason",),
            ("decision_target_price",),
            ("decision_write",),
        ]
        asyncio.run(_insert_no_change_raw(url))
        command.downgrade(config, "0006")
        assert asyncio.run(_migration_state(url, "SELECT count(*) FROM price_change")) == [(0,)]
        assert asyncio.run(_migration_state(url, _DECISION_COLUMNS)) == []
        rows = asyncio.run(_migration_state(url, "SELECT id, below_min, bot_enabled FROM shop"))
        assert {row[1] for row in rows} == {"do_nothing"}
        back = {row[0]: row[2] for row in rows}
        assert [back[shops[n]] for n in ("s1", "s2", "s3", "s4")] == [True, True, True, False]
        command.upgrade(config, "0007")
    finally:
        drop_test_database_sync(name)


# --- Т41 ---


async def _expect_violation(conn: AsyncConnection, sql: str, fragment: str, **params: Any) -> None:
    with pytest.raises(IntegrityError) as caught:
        async with conn.begin_nested():
            await conn.execute(text(sql), params)
    assert fragment in str(caught.value.orig)


async def test_decision_and_no_change_constraints(connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    await _expect_violation(
        connection,
        "UPDATE product SET skip_reason = 'x', skipped_at = now(), decision_reason = 'y' "
        "WHERE id = :i",
        "skip_or_decision",
        i=product,
    )
    await _expect_violation(
        connection,
        "UPDATE product SET decision_target_price = 600 WHERE id = :i",
        "decision_target_with_reason",
        i=product,
    )
    await _expect_violation(
        connection,
        "UPDATE product SET decision_write = 'sent' WHERE id = :i",
        "decision_write_with_reason",
        i=product,
    )
    await _expect_violation(
        connection,
        "UPDATE product SET decision_reason = 'y', decision_target_price = 0 WHERE id = :i",
        "decision_target_price_positive",
        i=product,
    )
    await connection.execute(
        text(
            "UPDATE product SET decision_reason = 'y', decision_target_price = 600, "
            "decision_write = 'dry_run' WHERE id = :i"
        ),
        {"i": product},
    )
    base = (
        "INSERT INTO price_change (tenant_id, shop_id, product_id, old_price, new_price, "
        "reason, result) VALUES (:t, :s, :p, :old, :new, 'x', :r)"
    )
    ids = {"t": tenant, "s": shop, "p": product}
    await _expect_violation(
        connection, base, "price_changed", **ids, old=100, new=90, r="no_change"
    )
    await _expect_violation(connection, base, "price_changed", **ids, old=100, new=100, r="applied")
    await connection.execute(text(base), {**ids, "old": 100, "new": 100, "r": "no_change"})
    await connection.execute(text(base), {**ids, "old": 100, "new": 90, "r": "applied"})


# --- Т42 ---


async def _decision(database: Database, tenant: uuid.UUID, product: int) -> tuple[Any, ...]:
    async with database.tenant(_t(tenant)) as scope:
        loaded = await scope.products.get(product)
        assert loaded is not None
        return (
            loaded.decision_reason,
            loaded.decision_target_price,
            loaded.decision_write,
            loaded.skip_reason,
            loaded.last_checked_at,
        )


async def test_decision_repository_methods(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    t1, t2, t3 = NOW + timedelta(seconds=1), NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)
    observation: dict[str, Any] = {
        "current_price": 590,
        "position": 1,
        "position_over": None,
        "competitors_count": 2,
        "first_place_price": 700,
    }
    async with database.tenant(_t(tenant)) as scope:
        await scope.products.record_skip(product, SkipReason.LISTING_MISSING, NOW)
        await scope.products.record_observation(
            product,
            **observation,
            decision_reason="out_of_bounds",
            decision_target_price=None,
            checked_at=t1,
        )
        await scope.commit()
    assert await _decision(database, tenant, product) == (
        "out_of_bounds",
        None,
        None,
        None,
        t1,
    )
    async with database.tenant(_t(tenant)) as scope:
        await scope.products.record_write(
            product,
            decision_reason="undercut_first",
            decision_target_price=699,
            decision_write="dry_run",
            checked_at=t2,
        )
        await scope.commit()
    assert await _decision(database, tenant, product) == (
        "undercut_first",
        699,
        "dry_run",
        None,
        t2,
    )
    async with database.tenant(_t(tenant)) as scope:
        await scope.products.touch_checked(product, t3)  # решение не трогает
        await scope.commit()
    assert await _decision(database, tenant, product) == (
        "undercut_first",
        699,
        "dry_run",
        None,
        t3,
    )
    async with database.tenant(_t(tenant)) as scope:
        # новое наблюдение обнуляет итог записи прошлого решения
        await scope.products.record_observation(
            product,
            **observation,
            decision_reason="decrease_disabled",
            decision_target_price=600,
            checked_at=t3,
        )
        await scope.commit()
    assert (await _decision(database, tenant, product))[:3] == ("decrease_disabled", 600, None)
    async with database.tenant(_t(tenant)) as scope:
        await scope.products.record_skip(product, SkipReason.OWN_PRICE_UNKNOWN, t3)
        await scope.commit()
    assert await _decision(database, tenant, product) == (None, None, None, "own_price_unknown", t3)
    async with database.tenant(_t(other)) as scope:
        with pytest.raises(NotFoundError):
            await scope.products.record_observation(
                product,
                **observation,
                decision_reason="out_of_bounds",
                decision_target_price=None,
                checked_at=t1,
            )
        with pytest.raises(NotFoundError):
            await scope.products.record_write(
                product,
                decision_reason="undercut_first",
                decision_target_price=699,
                decision_write="sent",
                checked_at=t1,
            )
