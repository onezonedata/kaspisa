"""Т67 (часть с базой): снимок записи переживает jsonb."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from db_worker_seed import NOW, insert_change, insert_product, insert_shop, insert_tenant
from sqlalchemy.ext.asyncio import AsyncConnection

from kaspisa.db import Database, TenantId
from kaspisa.kaspi.models import ListedProduct, ProductSnapshot, ProductState

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _snapshots() -> dict[str, ProductSnapshot]:
    listing = ListedProduct.model_validate(_load("product-list.json")["data"][0])
    state = ProductState.model_validate(_load("edit-price.json"))
    null_stock = _load("null-stock-product.json")
    return {
        "list_and_edit": ProductSnapshot(state=state, listing=listing),
        "null_stock": ProductSnapshot(
            state=ProductState.model_validate(null_stock["editPrice"]),
            listing=ListedProduct.model_validate(null_stock["listRow"]),
        ),
    }


@pytest.mark.parametrize("name", ["list_and_edit", "null_stock"])
async def test_snapshot_survives_jsonb(
    name: str, database: Database, connection: AsyncConnection
) -> None:
    from kaspisa.worker.snapshot_codec import snapshot_from_json, snapshot_to_json

    snapshot = _snapshots()[name]
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    change = await insert_change(connection, tenant, shop, product)
    async with database.tenant(TenantId(tenant)) as scope:
        assert await scope.price_writes.open(
            product,
            snapshot=snapshot_to_json(snapshot),
            expected={"710000000": 100},
            sent_at=NOW,
            price_change_id=change,
            now=NOW,
        )
        await scope.commit()
        row = await scope.price_writes.get(product)
    assert row is not None
    assert snapshot_from_json(row.snapshot) == snapshot
    assert row.expected_prices == {"710000000": 100}
