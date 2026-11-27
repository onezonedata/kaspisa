"""Н7 (ADR 0010): захват магазина по событию «каталог устарел» и метки загрузки."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from db_worker_seed import NOW, ago, insert_product, insert_shop, insert_tenant
from sqlalchemy.ext.asyncio import AsyncConnection

from kaspisa.db import Database, TenantId
from kaspisa.worker.constants import UNBOUNDED_RECHECK_SECONDS

DAY = 86_400


async def _claim(database: Database) -> uuid.UUID | None:
    async with database.system() as system:
        claimed = await system.claim_shop(
            worker_id="w1",
            now=NOW,
            lease_seconds=180,
            recheck_seconds=60,
            unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
        )
    return None if claimed is None else claimed.shop_id


# Магазин без единого отслеживаемого товара: захват только из-за каталога.
CLAIMED: dict[str, dict[str, Any]] = {
    "never_loaded": {"catalog_synced_at": None},
    "loaded_exactly_24h_ago": {"catalog_synced_at": ago(DAY)},
    "loaded_two_days_ago": {"catalog_synced_at": ago(2 * DAY)},
    "retry_due_exactly_now": {"catalog_synced_at": None, "catalog_retry_at": NOW},
    "bot_disabled": {"catalog_synced_at": None, "bot_mode": "off"},
    "no_city": {"catalog_synced_at": None, "city_id": None},
    "store_anomalies_halt": {"catalog_synced_at": None, "halt_reason": "store_anomalies"},
    "write_errors_halt": {"catalog_synced_at": None, "halt_reason": "write_errors"},
}

NOT_CLAIMED: dict[str, dict[str, Any]] = {
    "loaded_just_under_24h_ago": {"catalog_synced_at": ago(DAY - 1)},
    "loaded_now": {"catalog_synced_at": NOW},
    "retry_in_future": {"catalog_synced_at": None, "catalog_retry_at": NOW + timedelta(seconds=1)},
    "login_halt": {"catalog_synced_at": None, "halt_reason": "login_errors"},
    "not_connected": {"catalog_synced_at": None, "state": "not_connected"},
    "awaiting_code": {"catalog_synced_at": None, "state": "awaiting_code"},
    # Для каталога нужен именно connected, а не reauth_required/session_expired.
    "reauth_session_expired": {
        "catalog_synced_at": None,
        "state": "reauth_required",
        "error": "session_expired",
    },
}


@pytest.mark.parametrize("name", sorted(CLAIMED))
async def test_stale_catalog_claims_shop_without_tracked_products(
    name: str, database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, **CLAIMED[name])
    assert await _claim(database) == shop


@pytest.mark.parametrize("name", sorted(NOT_CLAIMED))
async def test_catalog_not_due_or_forbidden_does_not_claim(
    name: str, database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    await insert_shop(connection, tenant, **NOT_CLAIMED[name])
    assert await _claim(database) is None


async def test_price_checks_come_before_catalog_only_shops(
    database: Database, connection: AsyncConnection
) -> None:
    """Магазин с товаром к проверке идёт раньше магазина, которому нужен только каталог."""
    tenant = await insert_tenant(connection)
    catalog_only = await insert_shop(connection, tenant, catalog_synced_at=ago(5 * DAY))
    priced = await insert_shop(connection, tenant)
    await insert_product(connection, tenant, priced, last_checked_at=ago(61))
    assert await _claim(database) == priced
    assert await _claim(database) == catalog_only
    assert await _claim(database) is None


async def test_catalog_marks_are_written_by_repository(database: Database) -> None:
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    async with database.tenant(TenantId(tenant.id)) as scope:
        from kaspisa.security import Secret

        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        assert (shop.catalog_synced_at, shop.catalog_retry_at) == (None, None)
        shop = await scope.shops.defer_catalog(shop.id, NOW)
        assert shop.catalog_retry_at == NOW
        shop = await scope.shops.mark_catalog_synced(shop.id, NOW + timedelta(seconds=5))
        assert shop.catalog_synced_at == NOW + timedelta(seconds=5)
        assert shop.catalog_retry_at is None  # успех снимает паузу
        await scope.commit()
