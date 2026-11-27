"""Т53-Т56, Т76: загрузка каталога магазина (P1)."""

import uuid
from typing import Any

import pytest
from api_testing import (
    ROUTES,
    api_cipher,
    api_database_url,
    connected_shop,
    deps,
    fake_client,
    listed,
    raw_engine,
    read_shop,
    redis_client,
    redis_prefix,
    seed_connect_shop,
    seed_product,
    signed_in,
    sql_exec,
    stored_session,
    tenant_of,
    touch_session,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api import deps as api_deps
from kaspisa.kaspi import (
    KaspiTransportError,
    RateLimitedError,
    SessionExpiredError,
)
from kaspisa.worker.redis_limits import LimiterUnavailableError

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

assert any(r.path.endswith("/products/sync") for r in ROUTES)


async def _products(engine: AsyncEngine, shop_id: uuid.UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT * FROM product WHERE shop_id = :s ORDER BY id"), {"s": shop_id}
        )
        return [dict(row) for row in rows.mappings()]


async def test_first_sync(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant, default_step=3)
    fake_client(deps).queue(
        "list_products", [listed("A", "Телефон"), listed("B"), listed("C", "Кабель")]
    )

    response = await client.post(f"/api/shops/{shop_id}/products/sync")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["outcome"], body["added"], body["updated"], body["missing"]) == (
        "synced",
        3,
        0,
        0,
    )
    assert body["shop"]["id"] == str(shop_id)
    rows = await _products(raw_engine, shop_id)
    assert [(r["sku"], r["title"]) for r in rows] == [
        ("A", "Телефон"),
        ("B", "B"),
        ("C", "Кабель"),
    ]
    for row in rows:
        assert row["tracked"] is False
        assert row["step"] == 3
        assert row["min_price"] is None
        assert row["max_price"] is None
        assert row["current_price"] is None
    call = fake_client(deps).calls_of("list_products")[0]
    assert call["merchant_uid"] == "M1"


async def test_repeated_sync(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    first = await seed_product(
        raw_engine,
        tenant,
        shop_id,
        sku="A",
        title="old",
        tracked=True,
        min_price=100,
        max_price=200,
    )
    second = await seed_product(raw_engine, tenant, shop_id, sku="B", title="keep")
    fake_client(deps).queue("list_products", [listed("A", "new"), listed("D")])

    response = await client.post(f"/api/shops/{shop_id}/products/sync")

    body = response.json()
    assert (body["outcome"], body["added"], body["updated"], body["missing"]) == (
        "synced",
        1,
        1,
        1,
    )
    by_sku = {r["sku"]: r for r in await _products(raw_engine, shop_id)}
    assert by_sku["A"]["id"] == first
    assert (by_sku["A"]["title"], by_sku["A"]["tracked"]) == ("new", True)
    assert (by_sku["A"]["min_price"], by_sku["A"]["max_price"]) == (100, 200)
    assert (by_sku["B"]["id"], by_sku["B"]["title"]) == (second, "keep")
    assert set(by_sku) == {"A", "B", "D"}


async def test_sync_failures(deps: ApiDeps, raw_engine: AsyncEngine, redis_prefix: str) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    idle = await seed_connect_shop(deps, raw_engine, tenant)
    denied = await client.post(f"/api/shops/{idle}/products/sync")
    assert denied.status_code == 409
    assert denied.json()["code"] == "operation_not_allowed"

    shop_id = await connected_shop(deps, raw_engine, tenant)
    await seed_product(raw_engine, tenant, shop_id, sku="A", title="old")
    fake = fake_client(deps)
    url = f"/api/shops/{shop_id}/products/sync"

    fake.queue("list_products", KaspiTransportError("list_products", "ConnectError"))
    temporary = await client.post(url)
    fake.queue("list_products", RateLimitedError("list_products", 30))
    limited = await client.post(url)
    assert temporary.json()["outcome"] == "temporary_error"
    assert temporary.json()["retry_after_seconds"] is None
    assert limited.json()["outcome"] == "temporary_error"
    assert limited.json()["retry_after_seconds"] == 30
    assert [r["title"] for r in await _products(raw_engine, shop_id)] == ["old"]
    assert (await read_shop(raw_engine, shop_id))["connection_state"] == "connected"

    # Сессия изменилась, затем сбой: блок сохранён.
    fake.mutate_session = touch_session
    fake.queue("list_products", KaspiTransportError("list_products", "ReadTimeout"))
    await client.post(url)
    fake.mutate_session = None
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert "COOKIE-NEW-9c1d" in blob

    # Т55: отказ ограничителя сохраняет изменённую сессию и остаётся 503.
    before = (await read_shop(raw_engine, shop_id))["session_enc"]
    version = (await read_shop(raw_engine, shop_id))["connection_version"]
    fake.mutate_session = lambda session: touch_session(session, "COOKIE-LIM-5e2b")
    fake.queue("list_products", LimiterUnavailableError())
    unavailable = await client.post(url)
    fake.mutate_session = None
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "service_unavailable"
    row = await read_shop(raw_engine, shop_id)
    assert row["session_enc"] != before
    assert (row["connection_state"], row["connection_version"]) == ("connected", version)
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert "COOKIE-LIM-5e2b" in blob
    assert await deps.redis.get(f"{redis_prefix}:sync:{shop_id}") is None
    assert [r["title"] for r in await _products(raw_engine, shop_id)] == ["old"]
    # Сессия не менялась: 503, блок прежний.
    kept = row["session_enc"]
    fake.queue("list_products", LimiterUnavailableError())
    assert (await client.post(url)).status_code == 503
    assert (await read_shop(raw_engine, shop_id))["session_enc"] == kept

    fake.queue("list_products", SessionExpiredError("list_products"))
    expired = await client.post(url)
    assert expired.json()["outcome"] == "rejected"
    assert expired.json()["shop"]["connection_state"] == "reauth_required"
    assert expired.json()["shop"]["connection_error"] == "session_expired"


async def test_sync_unreadable_session(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    await sql_exec(
        raw_engine,
        # Инверсия последнего байта: присвоение константы не меняет блоб, если байт уже равен ей.
        "UPDATE shop SET session_enc = set_byte(session_enc, length(session_enc) - 1, "
        "get_byte(session_enc, length(session_enc) - 1) # 255) WHERE id = :id",
        id=shop_id,
    )

    response = await client.post(f"/api/shops/{shop_id}/products/sync")

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "rejected"
    assert response.json()["shop"]["connection_error"] == "secret_unreadable"
    assert fake_client(deps).calls_of("list_products") == []


async def test_sync_lock(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    token = await deps.sync_lock.acquire(shop_id, 60_000)
    assert token is not None

    busy = await client.post(f"/api/shops/{shop_id}/products/sync")

    assert busy.status_code == 409
    assert busy.json()["code"] == "sync_in_progress"
    assert fake_client(deps).calls_of("list_products") == []
    await deps.sync_lock.release(shop_id, token)

    fake_client(deps).queue("list_products", [listed("A")])
    done = await client.post(f"/api/shops/{shop_id}/products/sync")
    assert done.json()["outcome"] == "synced"
    assert await deps.sync_lock.acquire(shop_id, 60_000) is not None  # блокировка снята


async def test_catalog_limit_and_deleted_shop(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_deps, "CATALOG_MAX", 3)
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    fake = fake_client(deps)
    url = f"/api/shops/{shop_id}/products/sync"

    fake.queue("list_products", [listed(sku) for sku in ("A", "B", "C", "D")])
    too_large = await client.post(url)
    assert too_large.status_code == 409
    assert too_large.json()["code"] == "catalog_too_large"
    assert await _products(raw_engine, shop_id) == []

    # Сессия изменилась, каталог слишком большой: блок сессии всё равно сохранён.
    fake.mutate_session = touch_session
    fake.queue("list_products", [listed(sku) for sku in ("A", "B", "C", "D")])
    await client.post(url)
    fake.mutate_session = None
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert "COOKIE-NEW-9c1d" in blob

    big = await connected_shop(deps, raw_engine, tenant, merchant_uid="M2")
    for sku in ("A", "B", "C", "D"):
        await seed_product(raw_engine, tenant, big, sku=sku)
    fake.queue("list_products", [listed("A")])
    sync = await client.post(f"/api/shops/{big}/products/sync")
    listing = await client.get(f"/api/shops/{big}/products")
    assert sync.status_code == listing.status_code == 409
    assert sync.json()["code"] == listing.json()["code"] == "catalog_too_large"

    async def delete_shop() -> None:
        await sql_exec(raw_engine, "DELETE FROM shop WHERE id = :id", id=shop_id)

    fake.before_return = delete_shop
    fake.queue("list_products", [listed("A")])
    gone = await client.post(url)
    assert gone.status_code == 404
    assert gone.json()["code"] == "not_found"


async def test_sync_button_stamps_catalog_time(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Н7: ручная загрузка ставит метку, от которой обработчик отсчитывает сутки."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    fake = fake_client(deps)
    url = f"/api/shops/{shop_id}/products/sync"

    async def stamps() -> tuple[Any, Any]:
        async with raw_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT catalog_synced_at, catalog_retry_at FROM shop WHERE id = :i"),
                {"i": shop_id},
            )
            return tuple(row.one())

    assert await stamps() == (None, None)
    fake.queue("list_products", KaspiTransportError("list_products", "ReadTimeout"))
    await client.post(url)
    assert await stamps() == (None, None)  # сбой метку не ставит
    fake.queue("list_products", [listed("A")])
    assert (await client.post(url)).json()["outcome"] == "synced"
    synced, retry = await stamps()
    assert synced == deps.clock()
    assert retry is None


async def test_sync_lock_is_shared_with_worker(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_prefix: str
) -> None:
    """Н7: блокировка загрузки одна на кнопку и обработчик (ключ задаёт `SyncLock`)."""
    from kaspisa.worker.redis_limits import SyncLock

    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    held = await SyncLock(deps.redis, prefix=redis_prefix).acquire(shop_id, 60_000)
    assert held is not None

    busy = await client.post(f"/api/shops/{shop_id}/products/sync")

    assert busy.status_code == 409
    assert busy.json()["code"] == "sync_in_progress"
