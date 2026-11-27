"""ADR 0017, Т1-Т3: причина пропуска проверки товара (`skip_reason`, `skipped_at`)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import timedelta

import pytest
from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    new_test_db_name,
    sqlalchemy_url,
)
from db_worker_seed import NOW, insert_product, insert_shop, insert_tenant, scalar
from sqlalchemy import NullPool, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from alembic import command
from kaspisa.db import Database, NotFoundError, SkipReason, TenantId


def _t(tenant: uuid.UUID) -> TenantId:
    return TenantId(tenant)


async def _columns(url: str) -> Sequence[str]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'product' AND column_name IN ('skip_reason', 'skipped_at')"
                )
            )
            return sorted(row[0] for row in rows)
    finally:
        await engine.dispose()


def test_migration_0006_upgrades_and_downgrades() -> None:
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "0005")
        assert asyncio.run(_columns(url)) == []
        command.upgrade(config, "head")
        assert asyncio.run(_columns(url)) == ["skip_reason", "skipped_at"]
        command.downgrade(config, "0005")
        assert asyncio.run(_columns(url)) == []
        command.upgrade(config, "head")
        assert asyncio.run(_columns(url)) == ["skip_reason", "skipped_at"]
    finally:
        drop_test_database_sync(name)


async def test_skip_reason_with_time_constraint(connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    with pytest.raises(IntegrityError) as caught:
        async with connection.begin_nested():
            await connection.execute(
                text("UPDATE product SET skip_reason = 'x', skipped_at = NULL WHERE id = :i"),
                {"i": product},
            )
    assert "skip_reason_with_time" in str(caught.value.orig)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text("UPDATE product SET skip_reason = NULL, skipped_at = now() WHERE id = :i"),
                {"i": product},
            )
    await connection.execute(
        text("UPDATE product SET skip_reason = 'x', skipped_at = now() WHERE id = :i"),
        {"i": product},
    )
    await connection.execute(
        text("UPDATE product SET skip_reason = NULL, skipped_at = NULL WHERE id = :i"),
        {"i": product},
    )
    assert (
        await scalar(connection, "SELECT skip_reason FROM product WHERE id = :i", i=product) is None
    )


async def test_record_skip_then_observation_resets(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    t1 = NOW + timedelta(seconds=90)
    async with database.tenant(_t(tenant)) as scope:
        await scope.products.record_skip(product, SkipReason.LISTING_MISSING, NOW)
        await scope.commit()
        loaded = await scope.products.get(product)
        assert loaded is not None
        assert loaded.skip_reason == "listing_missing"
        assert (loaded.skipped_at, loaded.last_checked_at) == (NOW, NOW)
        await scope.products.record_observation(
            product,
            current_price=100,
            position=1,
            position_over=None,
            competitors_count=2,
            first_place_price=100,
            decision_reason=None,
            decision_target_price=None,
            checked_at=t1,
        )
        await scope.commit()
        loaded = await scope.products.get(product)
        assert loaded is not None
        assert (loaded.skip_reason, loaded.skipped_at) == (None, None)
        assert loaded.last_checked_at == t1


async def test_touch_checked_keeps_reason_and_foreign_tenant_is_rejected(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    t1 = NOW + timedelta(seconds=90)
    async with database.tenant(_t(tenant)) as scope:
        await scope.products.record_skip(product, SkipReason.OWN_PRICE_UNKNOWN, NOW)
        await scope.products.touch_checked(product, t1)
        await scope.commit()
        loaded = await scope.products.get(product)
        assert loaded is not None
        assert loaded.last_checked_at == t1
        assert (loaded.skip_reason, loaded.skipped_at) == ("own_price_unknown", NOW)
    async with database.tenant(_t(other)) as scope:
        with pytest.raises(NotFoundError):
            await scope.products.record_skip(product, SkipReason.KASPI_ERROR, NOW)
