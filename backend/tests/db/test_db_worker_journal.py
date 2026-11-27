"""Журнал цен для обработчика: были ли отправки товара после момента (серия временных отказов)."""

from __future__ import annotations

import uuid

from db_worker_seed import ago, insert_change, insert_product, insert_shop, insert_tenant
from sqlalchemy.ext.asyncio import AsyncConnection

from kaspisa.db import Database, TenantId


def _t(tenant: uuid.UUID) -> TenantId:
    return TenantId(tenant)


async def test_has_sent_since_counts_only_sent_rows_of_own_product(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    other_tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    neighbour = await insert_product(connection, tenant, shop)
    await insert_change(connection, tenant, shop, product, result="failed", created_at=ago(10))
    await insert_change(connection, tenant, shop, product, result="dry_run", created_at=ago(10))
    await insert_change(connection, tenant, shop, neighbour, result="applied", created_at=ago(10))
    await insert_change(connection, tenant, shop, product, result="applied", created_at=ago(500))
    async with database.tenant(_t(tenant)) as scope:
        assert not await scope.price_changes.has_sent_since(product, ago(300))
        assert await scope.price_changes.has_sent_since(product, ago(501))
    await insert_change(connection, tenant, shop, product, result="unconfirmed", created_at=ago(5))
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.price_changes.has_sent_since(product, ago(300))
        assert not await scope.price_changes.has_sent_since(product, ago(5))  # строго после
    async with database.tenant(_t(other_tenant)) as scope:
        assert not await scope.price_changes.has_sent_since(product, ago(10_000))
