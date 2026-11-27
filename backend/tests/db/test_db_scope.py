"""Т35: области доступа — транзакция, откат, чтение после выхода."""

# Хеши паролей в тестах — тестовые строки.

from __future__ import annotations

import pytest
from db_seed import TwoTenants

from kaspisa.db import Database, TenantId
from kaspisa.pricing.types import Strategy
from kaspisa.security import Secret


async def test_changes_without_commit_are_not_saved(database: Database) -> None:
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    tenant_id = TenantId(tenant.id)
    async with database.tenant(tenant_id) as scope:
        await scope.users.add(email="x@example.kz", password_hash="h")
    async with database.tenant(tenant_id) as scope:
        assert await scope.users.list() == []


async def test_commit_saves_and_object_readable_after_exit(database: Database) -> None:
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    tenant_id = TenantId(tenant.id)
    async with database.tenant(tenant_id) as scope:
        user = await scope.users.add(email="x@example.kz", password_hash="h")
        await scope.commit()
    assert user.email == "x@example.kz"
    async with database.tenant(tenant_id) as scope:
        assert [u.id for u in await scope.users.list()] == [user.id]


async def test_exception_rolls_back(database: Database) -> None:
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    tenant_id = TenantId(tenant.id)
    with pytest.raises(RuntimeError):
        async with database.tenant(tenant_id) as scope:
            await scope.users.add(email="x@example.kz", password_hash="h")
            raise RuntimeError("сбой")
    async with database.tenant(tenant_id) as scope:
        assert await scope.users.list() == []


async def test_commit_can_be_called_repeatedly(database: Database) -> None:
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    async with database.tenant(TenantId(tenant.id)) as scope:
        await scope.users.add(email="a@example.kz", password_hash="h")
        await scope.commit()
        await scope.users.add(email="b@example.kz", password_hash="h")
        await scope.commit()
    async with database.tenant(TenantId(tenant.id)) as scope:
        assert len(await scope.users.list()) == 2


async def test_setting_assignment_is_saved(database: Database, two_tenants: TwoTenants) -> None:
    a = two_tenants.a
    async with database.tenant(a.tenant_id) as scope:
        shop = await scope.shops.get(a.shop_id)
        assert shop is not None
        shop.strategy = Strategy.MATCH_FIRST.value
        await scope.commit()
    async with database.tenant(a.tenant_id) as scope:
        shop = await scope.shops.get(a.shop_id)
        assert shop is not None
        assert shop.strategy == "match_first"


async def test_shops_need_no_key_for_users_only(database: Database) -> None:
    """Секрет создаётся при первом обращении к магазинам, а не при создании области."""
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    async with database.tenant(TenantId(tenant.id)) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        assert shop.tenant_id == tenant.id
