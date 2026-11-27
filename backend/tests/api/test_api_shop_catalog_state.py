"""ADR 0017, Т10-Т13, Т16, Т32, Т33, Т38: состояние каталога магазина и его сводка."""

import uuid
from datetime import UTC, datetime

import pytest
from api_testing import (
    api_cipher,
    api_database_url,
    connected_shop,
    deps,
    fake_client,
    listed,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_connect_shop,
    seed_product,
    seed_shop,
    signed_in,
    sql_exec,
    tenant_of,
)
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api import deps as api_deps
from kaspisa.api.catalog_state import ApiSyncLock
from kaspisa.worker.redis_limits import SyncLock

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]


async def _shop(client: object, shop_id: uuid.UUID) -> dict[str, object]:
    response = await client.get(f"/api/shops/{shop_id}")  # type: ignore[attr-defined]
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def test_shop_exposes_catalog_synced_at(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    assert (await _shop(client, shop_id))["catalog_synced_at"] is None
    moment = datetime(2030, 5, 1, 12, 30, tzinfo=UTC)
    await sql_exec(
        raw_engine, "UPDATE shop SET catalog_synced_at = :t WHERE id = :i", t=moment, i=shop_id
    )
    got = (await _shop(client, shop_id))["catalog_synced_at"]
    assert isinstance(got, str)
    assert datetime.fromisoformat(got) == moment


async def test_catalog_loading_follows_the_worker_lock(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_prefix: str
) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    idle = await seed_connect_shop(deps, raw_engine, tenant)  # не подключён
    worker_lock = SyncLock(deps.redis, prefix=redis_prefix)
    assert (await _shop(client, shop_id))["catalog_loading"] is False

    token = await worker_lock.acquire(shop_id, 60_000)
    assert token is not None
    assert await worker_lock.acquire(idle, 60_000) is not None
    assert (await _shop(client, shop_id))["catalog_loading"] is True
    assert (await _shop(client, idle))["catalog_loading"] is False  # не подключён: без проверки
    listing = await client.get("/api/shops")
    by_id = {row["id"]: row for row in listing.json()}
    assert by_id[str(shop_id)]["catalog_loading"] is True

    await worker_lock.release(shop_id, token)
    assert (await _shop(client, shop_id))["catalog_loading"] is False


async def test_redis_failure_does_not_break_the_shop(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: ApiSyncLock, shop_id: uuid.UUID) -> bool:
        raise RedisError("down")

    monkeypatch.setattr(ApiSyncLock, "held", broken)
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    assert (await _shop(client, shop_id))["catalog_loading"] is False


async def test_reading_the_shop_does_not_take_the_lock(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_prefix: str
) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    await _shop(client, shop_id)
    token = await SyncLock(deps.redis, prefix=redis_prefix).acquire(shop_id, 60_000)
    assert token is not None


async def test_sync_response_is_built_after_the_lock_is_released(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    fake_client(deps).queue("list_products", [listed("A")])
    response = await client.post(f"/api/shops/{shop_id}/products/sync")
    assert response.json()["outcome"] == "synced"
    assert response.json()["shop"]["catalog_loading"] is False
    assert response.json()["shop"]["catalog_synced_at"] is not None


async def test_catalog_summary_counts(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    empty = await seed_shop(deps, raw_engine, tenant)
    for index in range(3):
        await seed_product(raw_engine, tenant, shop_id, tracked=index == 0)
    response = await client.get(f"/api/shops/{shop_id}/catalog-summary")
    assert response.status_code == 200, response.text
    assert response.json() == {"total": 3, "tracked": 1}
    response = await client.get(f"/api/shops/{empty}/catalog-summary")
    assert response.json() == {"total": 0, "tracked": 0}

    other = await signed_in(deps, email="b@example.com")
    foreign = await other.get(f"/api/shops/{shop_id}/catalog-summary")
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"
    missing = await client.get(f"/api/shops/{uuid.uuid4()}/catalog-summary")
    assert missing.status_code == 404


async def test_catalog_summary_over_limit_still_has_numbers(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_deps, "CATALOG_MAX", 2)
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    for _ in range(3):
        await seed_product(raw_engine, tenant, shop_id)
    response = await client.get(f"/api/shops/{shop_id}/catalog-summary")
    assert response.status_code == 200, response.text
    assert response.json() == {"total": 3, "tracked": 0}


async def test_sync_while_worker_loads_is_conflict_and_rejected_sees_the_lock(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_prefix: str
) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    token = await SyncLock(deps.redis, prefix=redis_prefix).acquire(shop_id, 60_000)
    assert token is not None
    busy = await client.post(f"/api/shops/{shop_id}/products/sync")
    assert busy.status_code == 409
    assert busy.json()["code"] == "sync_in_progress"

    # Блок сессии нечитаем: ветка `rejected` до `acquire` не берёт и не снимает чужую блокировку.
    # Магазин после этого `reauth_required`: `catalog_loading` у неподключённого всегда false.
    await sql_exec(
        raw_engine,
        # Инверсия последнего байта: присвоение константы не меняет блоб, если байт уже равен ей.
        "UPDATE shop SET session_enc = set_byte(session_enc, length(session_enc) - 1, "
        "get_byte(session_enc, length(session_enc) - 1) # 255) WHERE id = :id",
        id=shop_id,
    )
    rejected = await client.post(f"/api/shops/{shop_id}/products/sync")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["outcome"] == "rejected"
    assert rejected.json()["shop"]["connection_state"] == "reauth_required"
    assert rejected.json()["shop"]["catalog_loading"] is False
    assert await SyncLock(deps.redis, prefix=redis_prefix).acquire(shop_id, 60_000) is None
