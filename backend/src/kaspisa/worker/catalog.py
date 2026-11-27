"""Ядро слияния каталога магазина: общий код кнопки API и суточной загрузки обработчика.

Новые товары добавляются с выключенным отслеживанием, названия обновляются, пропавшие не
удаляются (настройки и журнал сохраняются). Журнал цен и настройки товаров не трогаются.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from kaspisa.db import Product, TenantScope
from kaspisa.kaspi.models import ListedProduct

# Предел каталога: больше - ошибка (неполный список хуже явной ошибки).
CATALOG_MAX = 10_000


class CatalogTooLargeError(Exception):
    """В каталоге больше `limit` товаров."""


async def load_catalog(scope: TenantScope, shop_id: uuid.UUID, *, limit: int) -> Sequence[Product]:
    """Все товары магазина; больше предела - `CatalogTooLargeError`."""
    rows = await scope.products.list_for_shop(shop_id, limit=limit + 1)
    if len(rows) > limit:
        raise CatalogTooLargeError
    return rows


async def merge_catalog(
    scope: TenantScope,
    shop_id: uuid.UUID,
    listed: list[ListedProduct],
    *,
    limit: int,
    now: datetime,
) -> tuple[int, int, int]:
    """Добавляет новые товары, обновляет названия, ставит метку загрузки.

    Возвращает (добавлено, обновлено, пропало). Слишком большой каталог: `commit()` (чтобы
    сохранить изменения сессии, сделанные вызывающим) и `CatalogTooLargeError`; каталог и
    метка не меняются. Коммит успеха - за вызывающим: метка уходит в нём же.
    """
    unique: dict[str, ListedProduct] = {}
    for row in listed:
        unique.setdefault(row.sku, row)
    if len(unique) > limit:
        await scope.commit()
        raise CatalogTooLargeError
    try:
        rows = await load_catalog(scope, shop_id, limit=limit)
    except CatalogTooLargeError:
        await scope.commit()
        raise
    existing = {product.sku: product for product in rows}
    added = updated = 0
    for sku, row in unique.items():
        title = row.master_title or sku
        product = existing.get(sku)
        if product is None:
            await scope.products.add(
                shop_id=shop_id, sku=sku, master_sku=row.master_sku, title=title
            )
            added += 1
        elif (product.master_sku, product.title) != (row.master_sku, title):
            product.master_sku = row.master_sku
            product.title = title
            updated += 1
    await scope.shops.mark_catalog_synced(shop_id, now)
    return added, updated, len(existing.keys() - unique.keys())
