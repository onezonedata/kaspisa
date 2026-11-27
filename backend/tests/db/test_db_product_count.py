"""ADR 0018: `ProductRepository.count_for_shop` - число товаров и отслеживаемых."""

from __future__ import annotations

import uuid

from db_worker_seed import insert_product, insert_shop, insert_tenant
from sqlalchemy.ext.asyncio import AsyncConnection

from kaspisa.db import Database, TenantId


def _t(tenant: uuid.UUID) -> TenantId:
    return TenantId(tenant)


async def test_counts_total_and_tracked(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    for index in range(5):
        await insert_product(connection, tenant, shop, tracked=index < 2)
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.products.count_for_shop(shop) == (5, 2)


async def test_empty_shop_is_zero_zero(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.products.count_for_shop(shop) == (0, 0)


async def test_counts_only_own_shop_and_tenant(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    other_tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    other_shop = await insert_shop(connection, tenant)
    foreign_shop = await insert_shop(connection, other_tenant)
    await insert_product(connection, tenant, shop, tracked=True)
    await insert_product(connection, tenant, other_shop, tracked=True)
    await insert_product(connection, tenant, other_shop, tracked=False)
    await insert_product(connection, other_tenant, foreign_shop, tracked=True)
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.products.count_for_shop(shop) == (1, 1)
        assert await scope.products.count_for_shop(other_shop) == (2, 1)
        assert await scope.products.count_for_shop(foreign_shop) == (0, 0)
