"""Снимок записи цены и отпечаток строки списка: перевод в JSON и обратно.

Снимок хранится в базе (jsonb), чтобы подтверждение работало после перезапуска.
Секретов в нём нет: это ответы кабинета о товаре.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from kaspisa.kaspi.models import ListedProduct, ProductSnapshot, ProductState


def snapshot_to_json(snapshot: ProductSnapshot) -> dict[str, Any]:
    """Снимок как словарь из типов JSON (поля — как в ответах Kaspi)."""
    return {
        "state": snapshot.state.model_dump(mode="json", by_alias=True),
        "listing": snapshot.listing.model_dump(mode="json", by_alias=True),
    }


def snapshot_from_json(data: dict[str, Any]) -> ProductSnapshot:
    """Обратное преобразование; равенство с исходным снимком сохраняется."""
    return ProductSnapshot(
        state=ProductState.model_validate(data["state"]),
        listing=ListedProduct.model_validate(data["listing"]),
    )


def listing_fingerprint(
    listing: ListedProduct, min_price: int | None, max_price: int | None
) -> str:
    """SHA-256 от данных строки списка, влияющих на запись, и границ цены.

    Название (`master_title`) в отпечаток не входит: на запись оно не влияет.
    """
    data = listing.model_dump(mode="json", by_alias=True)
    data.pop("masterTitle", None)
    data["cityPrices"] = sorted(data["cityPrices"], key=lambda item: item["cityId"])
    data["availabilities"] = sorted(data["availabilities"], key=lambda item: item["storeId"])
    payload = {"listing": data, "min_price": min_price, "max_price": max_price}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
