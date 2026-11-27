"""ADR 0020, Т52-Т54: решение бота в товаре, сброс правкой, строки журнала `no_change`."""

import uuid
from datetime import UTC, datetime
from typing import Any

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
    sql_exec,
    sql_scalar,
    tenant_of,
)
from sqlalchemy import text
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

CHECKED = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
DECISION_KEYS = ("decision_reason", "decision_target_price", "decision_write")


async def _setup(deps: ApiDeps, engine: AsyncEngine) -> tuple[Any, uuid.UUID, uuid.UUID]:
    client = await signed_in(deps, "a@example.com")
    tenant = await tenant_of(engine, "a@example.com")
    return client, tenant, await seed_shop(deps, engine, tenant)


def _decision(body: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(body[key] for key in DECISION_KEYS)


async def test_list_and_card_return_decision(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    stuck = await seed_product(
        raw_engine,
        tenant,
        shop_id,
        decision_reason="decrease_disabled",
        decision_target_price=600,
        last_checked_at=CHECKED,
    )
    changed = await seed_product(
        raw_engine,
        tenant,
        shop_id,
        decision_reason="undercut_first",
        decision_target_price=699,
        decision_write="dry_run",
    )
    fresh = await seed_product(raw_engine, tenant, shop_id)
    url = f"/api/shops/{shop_id}/products"

    listing = (await client.get(url)).json()["items"]
    by_id = {item["id"]: item for item in listing}
    assert _decision(by_id[stuck]) == ("decrease_disabled", 600, None)
    assert by_id[stuck]["last_checked_at"] == "2030-01-01T12:00:00Z"
    assert _decision(by_id[changed]) == ("undercut_first", 699, "dry_run")
    assert _decision(by_id[fresh]) == (None, None, None)
    assert all(item["pending_write"] is None for item in listing)
    for product, expected in (
        (stuck, ("decrease_disabled", 600, None)),
        (changed, ("undercut_first", 699, "dry_run")),
        (fresh, (None, None, None)),
    ):
        card = await client.get(f"{url}/{product}")
        assert card.status_code == 200, card.text
        assert _decision(card.json()) == expected


async def _decisions(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT decision_reason, decision_target_price, decision_write FROM product "
                "ORDER BY id"
            )
        )
        return [tuple(row) for row in rows]


async def test_product_edit_resets_decision_but_shop_edit_does_not(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    await confirm_password(client)
    seeded = [
        await seed_product(
            raw_engine,
            tenant,
            shop_id,
            decision_reason="undercut_first",
            decision_target_price=699,
            decision_write="sent",
        )
        for _ in range(3)
    ]
    keep = ("undercut_first", 699, "sent")
    assert await _decisions(raw_engine) == [keep] * 3

    shop = await client.patch(f"/api/shops/{shop_id}/settings", json={"default_step": 5})
    assert shop.status_code == 200, shop.text
    assert await _decisions(raw_engine) == [keep] * 3

    single = await client.patch(
        f"/api/shops/{shop_id}/products/{seeded[0]}", json={"min_price": 500}
    )
    assert single.status_code == 200, single.text
    assert _decision(single.json()) == (None, None, None)
    assert await _decisions(raw_engine) == [(None, None, None), keep, keep]

    bulk = await client.patch(
        f"/api/shops/{shop_id}/products",
        json={"product_ids": seeded[1:], "changes": {"auto_raise": True}},
    )
    assert bulk.status_code == 200, bulk.text
    assert await _decisions(raw_engine) == [(None, None, None)] * 3


async def test_edit_keeps_refused_decision_while_the_fingerprint_still_matches(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """ADR 0020: на пути отпечатка отказа итог остаётся `refused`; правка границ его сбрасывает."""
    client, tenant, shop_id = await _setup(deps, raw_engine)
    refused = ("undercut_first", 699, "refused")
    ids = [
        await seed_product(
            raw_engine,
            tenant,
            shop_id,
            min_price=600,
            max_price=800,
            refusal_reason="stock_counted",
            refusal_since=CHECKED,
            refusal_fingerprint="fp",
            decision_reason="undercut_first",
            decision_target_price=699,
            decision_write="refused",
        )
        for _ in range(3)
    ]
    url = f"/api/shops/{shop_id}/products"

    step = await client.patch(f"{url}/{ids[0]}", json={"step": 5, "auto_raise": True})
    assert step.status_code == 200, step.text
    assert _decision(step.json()) == refused
    same_bounds = await client.patch(f"{url}/{ids[0]}", json={"min_price": 600})
    assert _decision(same_bounds.json()) == refused

    bulk = await client.patch(url, json={"product_ids": ids[1:], "changes": {"step": 3}})
    assert bulk.status_code == 200, bulk.text
    assert await _decisions(raw_engine) == [refused] * 3

    bounds = await client.patch(f"{url}/{ids[0]}", json={"max_price": 900})
    assert _decision(bounds.json()) == (None, None, None)
    assert await _decisions(raw_engine) == [(None, None, None), refused, refused]


async def test_edit_resets_refused_decision_when_min_price_changes(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    product_id = await seed_product(
        raw_engine,
        tenant,
        shop_id,
        min_price=600,
        max_price=800,
        refusal_reason="stock_counted",
        refusal_since=CHECKED,
        refusal_fingerprint="fp",
        decision_reason="undercut_first",
        decision_target_price=699,
        decision_write="refused",
    )
    response = await client.patch(
        f"/api/shops/{shop_id}/products/{product_id}", json={"min_price": 650}
    )
    assert response.status_code == 200, response.text
    assert _decision(response.json()) == (None, None, None)


async def test_edit_resets_decision_without_fingerprint_or_without_refused_write(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Сохраняется только пара «отпечаток отказа и итог refused»: одного признака мало."""
    client, tenant, shop_id = await _setup(deps, raw_engine)
    common: dict[str, Any] = {
        "min_price": 600,
        "max_price": 800,
        "decision_reason": "undercut_first",
        "decision_target_price": 699,
    }
    no_fingerprint = await seed_product(
        raw_engine, tenant, shop_id, **common, decision_write="refused"
    )
    not_refused = await seed_product(
        raw_engine,
        tenant,
        shop_id,
        **common,
        refusal_reason="stock_counted",
        refusal_since=CHECKED,
        refusal_fingerprint="fp",
        decision_write="sent",
    )
    for product_id in (no_fingerprint, not_refused):
        response = await client.patch(
            f"/api/shops/{shop_id}/products/{product_id}", json={"step": 5}
        )
        assert response.status_code == 200, response.text
        assert _decision(response.json()) == (None, None, None)


async def test_journal_returns_no_change_rows(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop_id = await _setup(deps, raw_engine)
    product = await seed_product(raw_engine, tenant, shop_id)
    await sql_exec(
        raw_engine,
        "INSERT INTO price_change (tenant_id, shop_id, product_id, old_price, new_price, "
        "reason, result) VALUES (:t, :s, :p, 590, 590, 'out_of_bounds', 'no_change')",
        t=tenant,
        s=shop_id,
        p=product,
    )
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM price_change") == 1
    response = await client.get(f"/api/shops/{shop_id}/price-changes")
    assert response.status_code == 200, response.text
    (item,) = response.json()["items"]
    assert item["result"] == "no_change"
    assert item["old_price"] == item["new_price"] == 590
    assert item["reason"] == "out_of_bounds"
