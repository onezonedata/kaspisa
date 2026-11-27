"""Т67 (без базы), Т68: кодек снимка записи и отпечаток строки списка."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from kaspisa.kaspi.models import ListedProduct, ProductSnapshot, ProductState

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _null_stock() -> ProductSnapshot:
    data = _load("null-stock-product.json")
    return ProductSnapshot(
        state=ProductState.model_validate(data["editPrice"]),
        listing=ListedProduct.model_validate(data["listRow"]),
    )


def _list_and_edit() -> ProductSnapshot:
    return ProductSnapshot(
        state=ProductState.model_validate(_load("edit-price.json")),
        listing=ListedProduct.model_validate(_load("product-list.json")["data"][0]),
    )


@pytest.mark.parametrize("make", [_null_stock, _list_and_edit])
def test_snapshot_roundtrip(make: Any) -> None:
    from kaspisa.worker.snapshot_codec import snapshot_from_json, snapshot_to_json

    snapshot = make()
    data = snapshot_to_json(snapshot)
    assert json.loads(json.dumps(data)) == data  # только типы JSON
    assert snapshot_from_json(json.loads(json.dumps(data))) == snapshot


def test_none_stock_stays_none() -> None:
    from kaspisa.worker.snapshot_codec import snapshot_from_json, snapshot_to_json

    snapshot = _null_stock()
    assert any(a.stock_count is None for a in snapshot.listing.availabilities) or any(
        p.stock is None for c in snapshot.state.cities for p in c.points
    )
    restored = snapshot_from_json(snapshot_to_json(snapshot))
    assert [a.stock_count for a in restored.listing.availabilities] == [
        a.stock_count for a in snapshot.listing.availabilities
    ]
    assert [p.stock for c in restored.state.cities for p in c.points] == [
        p.stock for c in snapshot.state.cities for p in c.points
    ]


def test_snapshot_json_has_no_secrets_keys() -> None:
    from kaspisa.worker.snapshot_codec import snapshot_to_json

    text = json.dumps(snapshot_to_json(_list_and_edit()))
    assert "cookie" not in text.lower()


# --- Т68: отпечаток ---


def _row() -> ListedProduct:
    return _list_and_edit().listing


def test_fingerprint_is_stable() -> None:
    from kaspisa.worker.snapshot_codec import listing_fingerprint

    first = listing_fingerprint(_row(), 100, 200)
    assert first == listing_fingerprint(_row(), 100, 200)
    assert len(first) == 64


def test_fingerprint_ignores_key_order() -> None:
    from kaspisa.worker.snapshot_codec import listing_fingerprint

    raw = dict(_load("product-list.json")["data"][0])
    keys = list(raw)
    random.Random(1).shuffle(keys)  # noqa: S311 -- порядок ключей, не криптография
    shuffled = {k: raw[k] for k in keys}
    assert listing_fingerprint(ListedProduct.model_validate(raw), 1, 2) == listing_fingerprint(
        ListedProduct.model_validate(shuffled), 1, 2
    )


def _changed(mutate: Any) -> ListedProduct:
    raw = json.loads(json.dumps(_load("product-list.json")["data"][0]))
    mutate(raw)
    return ListedProduct.model_validate(raw)


def _stock(raw: dict[str, Any]) -> None:
    raw["availabilities"][0]["stockCount"] = 17
    raw["availabilities"][0]["stockSpecified"] = True


def _city_price(raw: dict[str, Any]) -> None:
    raw["cityPrices"][0]["value"] += 1


def _archived(raw: dict[str, Any]) -> None:
    raw["archivedAt"] = "2026-11-01T00:00:00"


def _preorder(raw: dict[str, Any]) -> None:
    raw["availabilities"][0]["preOrder"] = 5


@pytest.mark.parametrize("mutate", [_stock, _city_price, _archived, _preorder])
def test_fingerprint_changes_with_listing_data(mutate: Any) -> None:
    from kaspisa.worker.snapshot_codec import listing_fingerprint

    assert listing_fingerprint(_changed(mutate), 100, 200) != listing_fingerprint(_row(), 100, 200)


def test_fingerprint_changes_with_bounds() -> None:
    from kaspisa.worker.snapshot_codec import listing_fingerprint

    base = listing_fingerprint(_row(), 100, 200)
    assert listing_fingerprint(_row(), 101, 200) != base
    assert listing_fingerprint(_row(), 100, 201) != base
    assert listing_fingerprint(_row(), None, 200) != base


def test_fingerprint_ignores_master_title() -> None:
    from kaspisa.worker.snapshot_codec import listing_fingerprint

    def rename(raw: dict[str, Any]) -> None:
        raw["masterTitle"] = "Другое название"

    assert listing_fingerprint(_changed(rename), 100, 200) == listing_fingerprint(_row(), 100, 200)
