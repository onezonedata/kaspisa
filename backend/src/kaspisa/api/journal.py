"""Журнал изменений цены (J1)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from kaspisa.api.deps import PAGE_MAX, CurrentPrincipal, Deps
from kaspisa.api.errors import ApiError
from kaspisa.api.schemas import ID_MAX, PriceChangePage
from kaspisa.api.views import price_change_out

router = APIRouter(prefix="/api/shops")


@router.get("/{shop_id}/price-changes")
async def list_price_changes(
    shop_id: uuid.UUID,
    principal: CurrentPrincipal,
    deps: Deps,
    product_id: Annotated[int | None, Query(ge=1, le=ID_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PriceChangePage:
    async with deps.database.tenant(principal.tenant_id) as scope:
        if await scope.shops.get(shop_id) is None:
            raise ApiError(404, "not_found")
        if product_id is not None:
            product = await scope.products.get(product_id)
            if product is None or product.shop_id != shop_id:
                raise ApiError(404, "not_found")
            rows = await scope.price_changes.list_for_product(
                product_id, limit=limit, offset=offset
            )
        else:
            rows = await scope.price_changes.list_for_shop(shop_id, limit=limit, offset=offset)
        names: dict[int, tuple[str, str]] = {}
        for row in rows:
            if row.product_id not in names:
                item = await scope.products.get(row.product_id)
                names[row.product_id] = (item.sku, item.title) if item else ("", "")
        items = [
            price_change_out(row, sku=names[row.product_id][0], title=names[row.product_id][1])
            for row in rows
        ]
    return PriceChangePage(items=items, limit=limit, offset=offset)
