"""Т29, Т31–Т34, Т36, Т37: поведение репозиториев."""

# Хеши паролей в тестах — тестовые строки.

from __future__ import annotations

import asyncio

import pytest
from db_seed import TwoTenants, seed_tenant
from sqlalchemy import NullPool, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from kaspisa.db import (
    DEFAULT_CITY_ID,
    ConstraintViolationError,
    Database,
    EmailTakenError,
    PriceChangeResult,
    ShopAlreadyConnectedError,
    ShopConnectionState,
    StaleStateError,
    TenantId,
)
from kaspisa.security import Secret


async def _new_tenant(database: Database) -> TenantId:
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    return TenantId(tenant.id)


async def test_defaults(database: Database) -> None:
    tenant_id = await _new_tenant(database)
    async with database.tenant(tenant_id) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        assert shop.connection_state == ShopConnectionState.NOT_CONNECTED
        assert shop.connection_version == 0
        assert shop.bot_mode == "off"
        assert shop.strategy == "be_first"
        assert shop.default_step == 1
        assert shop.excluded_merchant_ids == []
        shop.default_step = 5
        await scope.commit()
        product = await scope.products.add(shop_id=shop.id, sku="s", master_sku="m", title="t")
        assert product.auto_decrease is False
        assert product.auto_raise is False
        assert product.min_price is None
        assert product.max_price is None
        assert product.step == 5


async def test_shop_add_sets_default_city(database: Database) -> None:
    """Т63: новый магазин получает город Астана (ADR 0032)."""
    tenant_id = await _new_tenant(database)
    async with database.tenant(tenant_id) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))

        assert shop.city_id == DEFAULT_CITY_ID == "710000000"


async def test_uniqueness_and_connected_rules(database: Database, two_tenants: TwoTenants) -> None:
    a, b = two_tenants.a, two_tenants.b
    async with database.tenant(a.tenant_id) as scope:
        with pytest.raises(ConstraintViolationError):
            await scope.products.add(shop_id=a.shop_id, sku=a.sku, master_sku="m", title="t")
    async with database.tenant(b.tenant_id) as scope:
        with pytest.raises(EmailTakenError):
            await scope.users.add(email=a.email.upper(), password_hash="h")
    async with database.tenant(b.tenant_id) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        with pytest.raises(ShopAlreadyConnectedError):
            await scope.shops.mark_connected(
                shop.id,
                merchant_uid=a.merchant_uid,
                session_state=Secret("s"),
                expected_version=shop.connection_version,
            )


async def test_connection_states(database: Database) -> None:
    tenant_id = await _new_tenant(database)
    async with database.tenant(tenant_id) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        changed0 = shop.connection_changed_at
        shop = await scope.shops.mark_awaiting_code(
            shop.id, Secret("step1"), expected_version=shop.connection_version
        )
        assert shop.connection_state == "awaiting_code"
        assert shop.connection_version == 1
        assert shop.connection_changed_at > changed0
        shop = await scope.shops.mark_connected(
            shop.id,
            merchant_uid="uid-1",
            session_state=Secret("cookies"),
            expected_version=shop.connection_version,
        )
        assert (shop.connection_state, shop.connection_version) == ("connected", 2)
        assert shop.merchant_uid == "uid-1"
        shop = await scope.shops.mark_reauth_required(
            shop.id, error="wrong_password", expected_version=shop.connection_version
        )
        assert shop.connection_state == "reauth_required"
        assert shop.connection_error == "wrong_password"
        assert shop.connection_version == 3
        session = await scope.shops.get_session(shop.id)
        assert session is not None
        assert session.reveal() == "cookies"
        shop = await scope.shops.set_credentials(shop.id, login=Secret("l2"), password=Secret("p2"))
        assert shop.connection_state == "not_connected"
        assert shop.connection_error is None
        assert shop.connection_version == 4
        session = await scope.shops.get_session(shop.id)
        assert session is not None
        assert session.reveal() == "cookies"
        creds = await scope.shops.get_credentials(shop.id)
        assert creds.login.reveal() == "l2"


async def test_price_change_journal(database: Database, two_tenants: TwoTenants) -> None:
    a = two_tenants.a
    async with database.tenant(a.tenant_id) as scope:
        change = await scope.price_changes.add(
            product_id=a.product_id,
            old_price=990,
            new_price=980,
            reason="matched_first",
            result=PriceChangeResult.DRY_RUN,
            target_price=980,
            competitor_merchant_id="c1",
            competitor_name="Конкурент",
            competitor_price=981,
            capped_at_max=True,
            skipped_competitors=2,
            cities_aligned=True,
            error="e",
        )
        assert change.shop_id == a.shop_id
        for i in range(3):
            await scope.price_changes.add(
                product_id=a.product_id,
                old_price=1000 + i,
                new_price=900 + i,
                reason="r",
                result=PriceChangeResult.APPLIED,
            )
        ids = [c.id for c in await scope.price_changes.list_for_shop(a.shop_id, limit=10)]
        assert ids == sorted(ids, reverse=True)
        assert len(ids) == 5
        page = await scope.price_changes.list_for_product(a.product_id, limit=2, offset=1)
        assert [c.id for c in page] == ids[1:3]
        loaded = (await scope.price_changes.list_for_product(a.product_id, limit=10))[3]
        assert loaded.id == change.id
        assert (loaded.competitor_name, loaded.skipped_competitors, loaded.capped_at_max) == (
            "Конкурент",
            2,
            True,
        )
        assert loaded.result == "dry_run"
        assert loaded.cities_aligned is True
        assert not any(
            c.cities_aligned
            for c in await scope.price_changes.list_for_shop(a.shop_id, limit=10)
            if c.id != change.id
        )


async def test_delete_shop_cascades_only_that_shop(
    database: Database, two_tenants: TwoTenants
) -> None:
    a = two_tenants.a
    async with database.tenant(a.tenant_id) as scope:
        other = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        other_product = await scope.products.add(
            shop_id=other.id, sku="o", master_sku="m", title="t"
        )
        await scope.commit()
        await scope.shops.delete(a.shop_id)
        await scope.commit()
    async with database.tenant(a.tenant_id) as scope:
        assert await scope.shops.get(a.shop_id) is None
        assert await scope.products.get(a.product_id) is None
        assert await scope.price_changes.list_for_shop(a.shop_id, limit=10) == []
        assert await scope.shops.get(other.id) is not None
        assert await scope.products.get(other_product.id) is not None


async def test_stale_writer(database: Database) -> None:
    tenant_id = await _new_tenant(database)
    async with database.tenant(tenant_id) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        version = shop.connection_version
        await scope.shops.mark_awaiting_code(shop.id, Secret("first"), expected_version=version)
        with pytest.raises(StaleStateError):
            await scope.shops.mark_awaiting_code(
                shop.id, Secret("second"), expected_version=version
            )
        session = await scope.shops.get_session(shop.id)
        assert session is not None
        assert session.reveal() == "first"

        shop = await scope.shops.set_credentials(shop.id, login=Secret("n"), password=Secret("n"))
        with pytest.raises(StaleStateError):
            await scope.shops.mark_connected(
                shop.id,
                merchant_uid="u",
                session_state=Secret("s"),
                expected_version=version + 1,
            )
        refreshed = await scope.shops.get(shop.id)
        assert refreshed is not None
        assert refreshed.connection_state == "not_connected"

        with pytest.raises(StaleStateError):
            await scope.shops.save_session(shop.id, Secret("x"), expected_version=version)
        current = shop.connection_version
        saved = await scope.shops.save_session(shop.id, Secret("x"), expected_version=current)
        assert saved.connection_version == current
        reread = await scope.shops.get(shop.id)
        assert reread is not None
        assert reread.connection_version == current

        # версия «из будущего» тоже устарела: сравнение на равенство, а не «не больше»
        with pytest.raises(StaleStateError):
            await scope.shops.save_session(shop.id, Secret("y"), expected_version=current + 1)
        with pytest.raises(StaleStateError):
            await scope.shops.mark_reauth_required(shop.id, error="e", expected_version=current + 1)


async def test_get_session_is_none_without_session(database: Database) -> None:
    tenant_id = await _new_tenant(database)
    async with database.tenant(tenant_id) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        assert await scope.shops.get_session(shop.id) is None


async def test_get_for_update_is_fresh_and_locks(
    committed_database: Database, database_url: str
) -> None:
    db = committed_database
    seed = await seed_tenant(db, "lock")
    url = database_url
    async with db.tenant(seed.tenant_id) as scope_a:
        stale = await scope_a.products.get(seed.product_id)
        assert stale is not None
        assert stale.min_price is None
        async with db.tenant(seed.tenant_id) as scope_b:
            other = await scope_b.products.get(seed.product_id)
            assert other is not None
            other.min_price = 100
            await scope_b.commit()
        fresh = await scope_a.products.get_for_update(seed.product_id)
        assert fresh is not None
        assert fresh.min_price == 100

        engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SET lock_timeout = '300ms'"))
                with pytest.raises(DBAPIError):
                    await conn.execute(
                        text("UPDATE product SET title = 'x' WHERE id = :i"),
                        {"i": seed.product_id},
                    )
                await conn.rollback()
                await scope_a.commit()
                await asyncio.sleep(0)
                await conn.execute(text("SET lock_timeout = '300ms'"))
                await conn.execute(
                    text("UPDATE product SET title = 'x' WHERE id = :i"), {"i": seed.product_id}
                )
                await conn.rollback()
        finally:
            await engine.dispose()
