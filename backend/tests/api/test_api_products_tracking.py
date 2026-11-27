"""Дополнение к Т53-Т65: `tracked` без границ, предел каталога ровно по границе."""

import uuid
from datetime import UTC, datetime

import pytest
from api_testing import (
    api_cipher,
    api_database_url,
    confirm_password,
    connected_shop,
    deps,
    fake_client,
    kaspi_session,
    listed,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_connect_shop,
    seed_product,
    seed_shop,
    signed_in,
    sql_exec,
    sql_scalar,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api import deps as api_deps
from kaspisa.db import TenantId

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]


async def _next_due(deps: ApiDeps, tenant: uuid.UUID, shop_id: uuid.UUID) -> int | None:
    async with deps.database.tenant(TenantId(tenant)) as scope:
        for bounded in (False, True):  # как слайс: без границ и с границами
            found = await scope.products.next_due(
                shop_id,
                now=datetime.now(UTC),
                recheck_seconds=0,
                unbounded_recheck_seconds=0,
                bounded=bounded,
            )
            if found is not None:
                return found.id
        return None


async def test_tracked_without_bounds_is_scheduled(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Отслеживаемый товар без границ обработчик берёт (ради цен конкурентов), без записи."""
    client = await signed_in(deps)
    await confirm_password(client)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    product = await seed_product(raw_engine, tenant, shop_id)
    url = f"/api/shops/{shop_id}/products/{product}"

    on = await client.patch(url, json={"tracked": True})
    assert on.status_code == 200, on.text
    assert on.json()["tracked"] is True
    assert await _next_due(deps, tenant, shop_id) == product

    only_min = await client.patch(url, json={"min_price": 100})
    assert only_min.status_code == 200
    assert await _next_due(deps, tenant, shop_id) == product

    both = await client.patch(url, json={"max_price": 200})
    assert both.status_code == 200
    assert await _next_due(deps, tenant, shop_id) == product

    cleared = await client.patch(url, json={"max_price": None})
    assert cleared.status_code == 200
    assert await _next_due(deps, tenant, shop_id) == product


async def test_bulk_tracked_makes_unbounded_products_due(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    bare = await seed_product(raw_engine, tenant, shop_id)

    response = await client.patch(
        f"/api/shops/{shop_id}/products",
        json={"product_ids": [bare], "changes": {"tracked": True}},
    )
    assert response.status_code == 200
    assert await _next_due(deps, tenant, shop_id) == bare


async def test_catalog_exactly_at_limit_is_accepted(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Предел включительно: ровно `CATALOG_MAX` товаров - не ошибка."""
    monkeypatch.setattr(api_deps, "CATALOG_MAX", 3)
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    fake_client(deps).queue("list_products", [listed(sku) for sku in ("A", "B", "C")])

    sync = await client.post(f"/api/shops/{shop_id}/products/sync")
    assert sync.status_code == 200, sync.text
    assert (sync.json()["outcome"], sync.json()["added"]) == ("synced", 3)

    listing = await client.get(f"/api/shops/{shop_id}/products")
    assert listing.status_code == 200
    assert listing.json()["total"] == 3

    fake_client(deps).queue("list_products", [listed(sku) for sku in ("A", "B", "C", "D")])
    over = await client.post(f"/api/shops/{shop_id}/products/sync")
    assert over.status_code == 409
    assert over.json()["code"] == "catalog_too_large"


def test_catalog_limit_is_ten_thousand() -> None:
    assert api_deps.CATALOG_MAX == 10_000


async def test_bounds_equal_are_allowed_and_inverted_are_not(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    await confirm_password(client)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    product = await seed_product(raw_engine, tenant, shop_id)
    url = f"/api/shops/{shop_id}/products/{product}"

    equal = await client.patch(url, json={"min_price": 500, "max_price": 500})
    assert equal.status_code == 200, equal.text
    assert (equal.json()["min_price"], equal.json()["max_price"]) == (500, 500)

    # Меняется только минимум: проверка идёт против уже сохранённого максимума.
    raised = await client.patch(url, json={"min_price": 501})
    assert raised.status_code == 422
    assert raised.json()["code"] == "min_above_max"
    lowered = await client.patch(url, json={"max_price": 499})
    assert lowered.status_code == 422
    assert lowered.json()["code"] == "min_above_max"
    both = await client.patch(url, json={"min_price": 600, "max_price": 599})
    assert both.status_code == 422
    after = (await client.get(url)).json()
    assert (after["min_price"], after["max_price"]) == (500, 500)


async def test_sync_not_allowed_outside_connected_state(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Загрузка каталога требует `connected`, даже если есть merchant_uid и сессия."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    for state in ("reauth_required", "awaiting_code"):
        shop_id = await seed_connect_shop(
            deps,
            raw_engine,
            tenant,
            state=state,
            merchant_uid=f"M-{state}",
            session=kaspi_session("awaiting_code" if state == "awaiting_code" else "authenticated"),
            version=2,
        )
        response = await client.post(f"/api/shops/{shop_id}/products/sync")
        assert response.status_code == 409, state
        assert response.json()["code"] == "operation_not_allowed"
    assert fake_client(deps).calls == []


async def test_sync_duplicate_sku_first_row_wins(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    fake_client(deps).queue(
        "list_products", [listed("A", "Первое"), listed("A", "Второе"), listed("B")]
    )

    sync = await client.post(f"/api/shops/{shop_id}/products/sync")

    assert (sync.json()["added"], sync.json()["updated"]) == (2, 0)
    items = (await client.get(f"/api/shops/{shop_id}/products")).json()["items"]
    assert {item["sku"]: item["title"] for item in items}["A"] == "Первое"


async def test_sync_does_not_rewrite_unchanged_session(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    before = await sql_scalar(raw_engine, "SELECT session_enc FROM shop WHERE id = :i", i=shop_id)
    fake_client(deps).queue("list_products", [listed("A")])

    await client.post(f"/api/shops/{shop_id}/products/sync")

    after = await sql_scalar(raw_engine, "SELECT session_enc FROM shop WHERE id = :i", i=shop_id)
    assert bytes(after) == bytes(before)


async def test_sync_lock_is_released_only_by_its_owner(deps: ApiDeps) -> None:
    shop_id = uuid.uuid4()
    token = await deps.sync_lock.acquire(shop_id, 60_000)
    assert token is not None
    assert await deps.sync_lock.acquire(shop_id, 60_000) is None

    await deps.sync_lock.release(shop_id, "someone-else")
    assert await deps.sync_lock.acquire(shop_id, 60_000) is None

    await deps.sync_lock.release(shop_id, token)
    assert await deps.sync_lock.acquire(shop_id, 60_000) is not None


async def test_bulk_changes_leave_unlisted_fields_untouched(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    product = await seed_product(raw_engine, tenant, shop_id, min_price=5, max_price=9, step=7)
    await sql_exec(
        raw_engine,
        "UPDATE product SET auto_raise = true, auto_decrease = true WHERE id = :i",
        i=product,
    )

    response = await client.patch(
        f"/api/shops/{shop_id}/products",
        json={"product_ids": [product], "changes": {"tracked": True}},
    )

    assert response.status_code == 200
    item = (await client.get(f"/api/shops/{shop_id}/products/{product}")).json()
    assert (item["tracked"], item["step"], item["auto_raise"], item["auto_decrease"]) == (
        True,
        7,
        True,
        True,
    )
    assert (item["min_price"], item["max_price"]) == (5, 9)
