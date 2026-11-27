"""Т57-Т65: список, настройки одного товара, массовая правка, изоляция, журнал."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from api_testing import (
    ROUTES,
    FakeClock,
    api_cipher,
    api_database_url,
    confirm_password,
    deps,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_change,
    seed_price_write,
    seed_product,
    seed_shop,
    signed_in,
    sql_exec,
    sql_scalar,
    tenant_of,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps, create_app
from kaspisa.db import TenantId

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]


async def _setup(
    deps: ApiDeps, engine: AsyncEngine, email: str = "a@example.com"
) -> tuple[Any, uuid.UUID, uuid.UUID]:
    client = await signed_in(deps, email)
    tenant = await tenant_of(engine, email)
    shop_id = await seed_shop(deps, engine, tenant)
    return client, tenant, shop_id


async def _row(engine: AsyncEngine, product_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT * FROM product WHERE id = :id"), {"id": product_id})
        return dict(row.mappings().one())


async def test_list_and_filters(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    ids = [
        await seed_product(raw_engine, tenant, shop_id, sku="IPH-2", title="Чехол"),
        await seed_product(
            raw_engine,
            tenant,
            shop_id,
            sku="X1",
            title="iPhone 15",
            tracked=True,
            min_price=100,
            max_price=200,
        ),
        await seed_product(
            raw_engine,
            tenant,
            shop_id,
            sku="X2",
            title="Кабель",
            refusal_reason="stock_counted",
            refusal_since=datetime(2026, 11, 1, 10, tzinfo=UTC),
            retry_at=datetime(2026, 11, 2, 10, tzinfo=UTC),
        ),
        await seed_product(raw_engine, tenant, shop_id, sku="X3", title="Зарядка", tracked=True),
        await seed_product(raw_engine, tenant, shop_id, sku="X4", title="Ремешок"),
    ]
    url = f"/api/shops/{shop_id}/products"

    async def fetch(query: str) -> dict[str, Any]:
        response = await client.get(f"{url}?{query}" if query else url)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def picked(body: dict[str, Any]) -> list[int]:
        return [item["id"] for item in body["items"]]

    found = await fetch("q=iph")
    # отслеживаемые идут первыми, затем по id (ADR 0017)
    assert (picked(found), found["total"]) == ([ids[1], ids[0]], 2)
    assert picked(await fetch("tracked=true")) == [ids[1], ids[3]]
    assert picked(await fetch("has_bounds=false")) == [ids[3], ids[0], ids[2], ids[4]]
    refused = await fetch("has_refusal=true")
    assert picked(refused) == [ids[2]]
    item = refused["items"][0]
    assert item["refusal_reason"] == "stock_counted"
    assert item["refusal_since"] is not None
    assert item["retry_at"] is not None
    assert picked(await fetch("tracked=true&has_bounds=true")) == [ids[1]]
    page = await fetch("limit=2&offset=2")
    assert (picked(page), page["total"]) == ([ids[0], ids[2]], 5)
    everything = await fetch("")
    assert all(item["pending_write"] is None for item in everything["items"])
    assert (everything["items"][0]["shop_id"], everything["items"][0]["sku"]) == (
        str(shop_id),
        "X1",
    )


async def test_list_parameters(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, _, shop_id = await _setup(deps, raw_engine)
    url = f"/api/shops/{shop_id}/products"

    for query in ("limit=0", "limit=201", "offset=-1", f"q={'x' * 101}"):
        response = await client.get(f"{url}?{query}")
        assert response.status_code == 422, query
        assert response.json()["code"] == "validation_error"


async def test_update_product(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    product = await seed_product(raw_engine, tenant, shop_id)
    url = f"/api/shops/{shop_id}/products/{product}"
    await confirm_password(client)  # у нового товара нет опоры: минимум «опасен» (ADR 0024, Н2)

    first = await client.patch(
        url,
        json={
            "tracked": True,
            "min_price": 1000,
            "max_price": 1500,
            "step": 10,
            "auto_decrease": True,
            "auto_raise": True,
        },
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["tracked"], body["min_price"], body["max_price"], body["step"]) == (
        True,
        1000,
        1500,
        10,
    )
    assert (body["auto_decrease"], body["auto_raise"]) == (True, True)

    above = await client.patch(url, json={"max_price": 900})
    assert above.status_code == 422
    assert above.json()["code"] == "min_above_max"
    row = await _row(raw_engine, product)
    assert (row["min_price"], row["max_price"]) == (1000, 1500)

    cleared = await client.patch(url, json={"min_price": None})
    assert cleared.status_code == 200
    assert (cleared.json()["min_price"], cleared.json()["max_price"]) == (None, 1500)
    assert (await client.get(url)).json()["max_price"] == 1500


async def test_update_product_validation(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    product = await seed_product(raw_engine, tenant, shop_id, min_price=5, max_price=9)
    url = f"/api/shops/{shop_id}/products/{product}"
    before = await _row(raw_engine, product)

    bad: list[dict[str, Any]] = [
        {"min_price": 0},
        {"step": 0},
        {"tracked": None},
        {"foo": 1},
        {"max_price": 100_000_001},
        {},
    ]
    for body in bad:
        response = await client.patch(url, json=body)
        assert response.status_code == 422, body
        assert response.json()["code"] == "validation_error"
    assert await _row(raw_engine, product) == before

    # Т60: большие и нулевые id - 422, а не 500.
    huge = 9223372036854775808
    for method in ("get", "patch"):
        kwargs: dict[str, Any] = {"json": {"step": 2}} if method == "patch" else {}
        response = await getattr(client, method)(f"/api/shops/{shop_id}/products/{huge}", **kwargs)
        assert response.status_code == 422, method
        assert response.json()["code"] == "validation_error"
        assert "path.product_id" in response.json()["fields"]
    zero = await client.get(f"/api/shops/{shop_id}/products/0")
    assert zero.status_code == 422
    changes = await client.get(f"/api/shops/{shop_id}/price-changes?product_id={huge}")
    assert changes.status_code == 422
    assert "query.product_id" in changes.json()["fields"]
    bulk = await client.patch(
        f"/api/shops/{shop_id}/products",
        json={"product_ids": [huge], "changes": {"tracked": True}},
    )
    assert bulk.status_code == 422
    assert bulk.json()["code"] == "validation_error"
    assert any(f.startswith("body.product_ids") for f in bulk.json()["fields"])
    missing = await client.get(f"/api/shops/{shop_id}/products/9223372036854775807")
    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")


async def test_bounds_change_with_open_write(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    await confirm_password(client)
    clock = deps.clock
    assert isinstance(clock, FakeClock)
    now = clock.now
    product = await seed_product(raw_engine, tenant, shop_id)
    await seed_price_write(
        raw_engine,
        tenant,
        shop_id,
        product,
        status="receipt",
        sent_at=now - timedelta(seconds=60),
        prices={"750000000": 1200},
    )
    url = f"/api/shops/{shop_id}/products/{product}"

    response = await client.patch(url, json={"min_price": 1300, "max_price": 2000})

    assert response.status_code == 200, response.text
    pending = response.json()["pending_write"]
    assert pending["status"] == "receipt"
    assert pending["prices"] == {"750000000": 1200}
    assert pending["outside_bounds"] is True
    sent = now - timedelta(seconds=60)
    assert _iso(pending["sent_at"]) == sent
    assert _iso(pending["hold_until"]) == sent + timedelta(seconds=600)
    row = await _row(raw_engine, product)
    assert (row["min_price"], row["max_price"]) == (1300, 2000)
    inside = await client.patch(url, json={"min_price": 1000, "max_price": 2000})
    assert inside.json()["pending_write"]["outside_bounds"] is False
    assert (await client.get(url)).json()["pending_write"] == inside.json()["pending_write"]

    clock.advance(600)
    assert (await client.get(url)).json()["pending_write"] is None
    clock.advance(-600)
    await sql_exec(
        raw_engine, "UPDATE price_write SET status = 'confirmed' WHERE product_id = :p", p=product
    )
    assert (await client.get(url)).json()["pending_write"] is None


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def test_product_row_lock(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    await confirm_password(client)
    product = await seed_product(raw_engine, tenant, shop_id)

    async with deps.database.tenant(TenantId(tenant)) as scope:
        held = await scope.products.get_for_update(product)
        assert held is not None
        task = asyncio.create_task(
            client.patch(f"/api/shops/{shop_id}/products/{product}", json={"min_price": 500})
        )
        done, _ = await asyncio.wait({task}, timeout=0.3)
        assert not done
        await scope.commit()
    response = await asyncio.wait_for(task, timeout=5)

    assert response.status_code == 200
    assert (await _row(raw_engine, product))["min_price"] == 500


async def test_bulk_update(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    other_shop = await seed_shop(deps, raw_engine, tenant)
    first = await seed_product(raw_engine, tenant, shop_id)
    second = await seed_product(raw_engine, tenant, shop_id)
    elsewhere = await seed_product(raw_engine, tenant, other_shop)
    url = f"/api/shops/{shop_id}/products"

    ok = await client.patch(
        url, json={"product_ids": [second, first, second], "changes": {"tracked": True}}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"updated": 2}
    assert (await _row(raw_engine, first))["tracked"] is True
    assert (await _row(raw_engine, second))["tracked"] is True

    await sql_exec(raw_engine, "UPDATE product SET tracked = false")
    await seed_shop(deps, raw_engine, tenant)
    await signed_in(deps, "b@example.com")
    tenant_b = await tenant_of(raw_engine, "b@example.com")
    shop_b = await seed_shop(deps, raw_engine, tenant_b)
    foreign = await seed_product(raw_engine, tenant_b, shop_b)
    for stray in (elsewhere, foreign, 10**9):
        response = await client.patch(
            url, json={"product_ids": [first, stray], "changes": {"tracked": True}}
        )
        assert response.status_code == 404, stray
        assert response.json()["code"] == "not_found"
    assert (await _row(raw_engine, first))["tracked"] is False
    assert (await _row(raw_engine, elsewhere))["tracked"] is False

    bad: list[dict[str, Any]] = [
        {"product_ids": [], "changes": {"tracked": True}},
        {"product_ids": list(range(1, 502)), "changes": {"tracked": True}},
        {"product_ids": [first], "changes": {"min_price": 1}},
        {"product_ids": [first], "changes": {}},
        {"product_ids": [first]},
    ]
    for body in bad:
        response = await client.patch(url, json=body)
        assert response.status_code == 422, body
    steps = await client.patch(
        url,
        json={
            "product_ids": [first],
            "changes": {"step": 4, "auto_raise": True, "auto_decrease": True},
        },
    )
    assert steps.status_code == 200
    row = await _row(raw_engine, first)
    assert (row["step"], row["auto_raise"], row["auto_decrease"]) == (4, True, True)


async def test_product_isolation(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    app = create_app(deps)
    openapi_paths = app.openapi()["paths"]
    expected = {
        (method.upper(), path)
        for path, operations in openapi_paths.items()
        if "{product_id}" in path
        for method in operations
    }
    expected |= {
        ("PATCH", "/api/shops/{shop_id}/products"),
        ("GET", "/api/shops/{shop_id}/price-changes"),
    }
    product_routes = [
        r
        for r in ROUTES
        if "{product_id}" in r.path
        or (r.method, r.path)
        in {
            ("PATCH", "/api/shops/{shop_id}/products"),
            ("GET", "/api/shops/{shop_id}/price-changes"),
        }
    ]
    assert {(r.method, r.path) for r in product_routes} == expected

    client_a, tenant_a, shop_a1 = await _setup(deps, raw_engine, "a@example.com")
    client_b, _, shop_b = await _setup(deps, raw_engine, "b@example.com")
    shop_a2 = await seed_shop(deps, raw_engine, tenant_a)
    product_a = await seed_product(raw_engine, tenant_a, shop_a1)
    product_a2 = await seed_product(raw_engine, tenant_a, shop_a2)
    await seed_change(raw_engine, tenant_a, shop_a1, product_a)
    before = (await _row(raw_engine, product_a), await _row(raw_engine, product_a2))

    for spec in product_routes:
        # Товар A по пути магазина B; товар второго магазина по пути первого; чужой арендатор.
        tries = [
            (client_b, shop_b, product_a),
            (client_a, shop_a1, product_a2),
            (client_b, shop_a1, product_a),
        ]
        for client, shop_id, product_id in tries:
            if spec.path.endswith("/price-changes"):
                response = await client.get(f"{spec.url(shop_id)}?product_id={product_id}")
            elif spec.path.endswith("/products") and spec.method == "PATCH":
                response = await client.request(
                    spec.method,
                    spec.url(shop_id),
                    json={"product_ids": [product_id], "changes": {"tracked": True}},
                )
            elif "{product_id}" in spec.path:
                response = await client.request(
                    spec.method, spec.url(shop_id, product_id), json=spec.body
                )
            else:
                continue
            assert response.status_code == 404, (spec, shop_id, product_id)
            assert response.json()["code"] == "not_found"

    assert (await _row(raw_engine, product_a), await _row(raw_engine, product_a2)) == before
    assert not any(row["tracked"] for row in before)


async def test_price_changes_journal(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    other_shop = await seed_shop(deps, raw_engine, tenant)
    first = await seed_product(raw_engine, tenant, shop_id, sku="S1", title="Первый")
    second = await seed_product(raw_engine, tenant, shop_id, sku="S2", title="Второй")
    foreign = await seed_product(raw_engine, tenant, other_shop)
    changes = [await seed_change(raw_engine, tenant, shop_id, first) for _ in range(3)]
    changes.append(await seed_change(raw_engine, tenant, shop_id, second))
    url = f"/api/shops/{shop_id}/price-changes"

    everything = await client.get(url)
    assert everything.status_code == 200, everything.text
    body = everything.json()
    assert [item["id"] for item in body["items"]] == list(reversed(changes))
    assert (body["items"][0]["sku"], body["items"][0]["title"]) == ("S2", "Второй")
    assert (body["items"][1]["sku"], body["items"][1]["title"]) == ("S1", "Первый")
    assert (body["limit"], body["offset"]) == (50, 0)
    assert body["items"][0]["result"] == "unconfirmed"
    assert body["items"][0]["cities_aligned"] is False

    only_first = await client.get(f"{url}?product_id={first}")
    assert [item["id"] for item in only_first.json()["items"]] == changes[2::-1][:3]
    page = await client.get(f"{url}?product_id={first}&limit=2&offset=1")
    assert [item["id"] for item in page.json()["items"]] == [changes[1], changes[0]]
    assert (await client.get(f"{url}?product_id={foreign}")).status_code == 404
    assert (await client.get(f"{url}?limit=0")).status_code == 422
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM price_change") == 4


async def test_position_over_in_list(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Позиция «N+» (ADR 0016): `position` пуст, `position_over` - число прочитанных продавцов."""
    client, tenant, shop_id = await _setup(deps, raw_engine)
    await seed_product(
        raw_engine, tenant, shop_id, sku="OVER", position_over=20, competitors_count=57
    )
    await seed_product(raw_engine, tenant, shop_id, sku="EXACT", position=3)
    items = (await client.get(f"/api/shops/{shop_id}/products")).json()["items"]
    by_sku = {i["sku"]: i for i in items}
    assert (by_sku["OVER"]["position"], by_sku["OVER"]["position_over"]) == (None, 20)
    assert by_sku["OVER"]["competitors_count"] == 57
    assert (by_sku["EXACT"]["position"], by_sku["EXACT"]["position_over"]) == (3, None)
