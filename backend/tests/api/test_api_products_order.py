"""ADR 0017, Т14, Т15, Т34: порядок списка товаров и причина пропуска проверки."""

from datetime import UTC, datetime

from api_testing import (
    api_cipher,
    api_database_url,
    confirm_password,
    deps,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_product,
    seed_shop,
    signed_in,
    sql_scalar,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

MOMENT = datetime(2030, 5, 1, 12, 30, tzinfo=UTC)


async def test_tracked_products_come_first_then_by_id(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    ids = [
        await seed_product(raw_engine, tenant, shop_id, tracked=index >= 2) for index in range(4)
    ]
    url = f"/api/shops/{shop_id}/products"

    body = (await client.get(url)).json()
    assert [row["id"] for row in body["items"]] == [ids[2], ids[3], ids[0], ids[1]]
    assert body["total"] == 4
    untracked = (await client.get(url, params={"tracked": "false"})).json()
    assert [row["id"] for row in untracked["items"]] == [ids[0], ids[1]]
    page = (await client.get(url, params={"limit": 1, "offset": 1})).json()
    assert [row["id"] for row in page["items"]] == [ids[3]]
    assert page["total"] == 4


async def test_product_out_has_skip_reason(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    product = await seed_product(
        raw_engine,
        tenant,
        shop_id,
        tracked=True,
        skip_reason="own_price_unknown",
        skipped_at=MOMENT,
    )
    plain = await seed_product(raw_engine, tenant, shop_id)
    listing = (await client.get(f"/api/shops/{shop_id}/products")).json()["items"]
    by_id = {row["id"]: row for row in listing}
    assert by_id[product]["skip_reason"] == "own_price_unknown"
    assert datetime.fromisoformat(by_id[product]["skipped_at"]) == MOMENT
    assert (by_id[plain]["skip_reason"], by_id[plain]["skipped_at"]) == (None, None)
    card = (await client.get(f"/api/shops/{shop_id}/products/{product}")).json()
    assert card["skip_reason"] == "own_price_unknown"
    assert datetime.fromisoformat(card["skipped_at"]) == MOMENT


async def test_editing_a_product_clears_the_skip_reason(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    await confirm_password(client)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    first, second, third = [
        await seed_product(
            raw_engine,
            tenant,
            shop_id,
            skip_reason="own_price_unknown",
            skipped_at=MOMENT,
        )
        for _ in range(3)
    ]
    response = await client.patch(f"/api/shops/{shop_id}/products/{first}", json={"min_price": 900})
    assert response.status_code == 200, response.text
    assert (response.json()["skip_reason"], response.json()["skipped_at"]) == (None, None)
    stored = await sql_scalar(
        raw_engine,
        "SELECT count(*) FROM product WHERE id = :i AND skip_reason IS NULL AND skipped_at IS NULL",
        i=first,
    )
    assert stored == 1

    bulk = await client.patch(
        f"/api/shops/{shop_id}/products",
        json={"product_ids": [second, third], "changes": {"tracked": True}},
    )
    assert bulk.status_code == 200, bulk.text
    for product in (second, third):
        cleared = await sql_scalar(
            raw_engine,
            "SELECT count(*) FROM product WHERE id = :i AND skip_reason IS NULL "
            "AND skipped_at IS NULL",
            i=product,
        )
        assert cleared == 1
