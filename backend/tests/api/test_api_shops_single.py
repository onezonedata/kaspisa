"""Этап 20 (ADR 0029): один магазин на арендатора, `409 shop_limit_reached` в `POST /api/shops`."""

import asyncio
import uuid
from typing import Any

import httpx
from api_testing import (
    KASPI_LOGIN,
    KASPI_PASSWORD,
    api_cipher,
    api_database_url,
    code_required,
    connected_shop,
    deps,
    fake_client,
    logged_in,
    raw_engine,
    read_shop,
    redis_client,
    redis_prefix,
    seed_connect_shop,
    seed_product,
    seed_shop,
    signed_in,
    sql_scalar,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.db import ShopRepository

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

BODY = {"login": "other-login-7e1a", "password": "other-password-7e1a"}
LIMIT = {"code": "shop_limit_reached", "retry_after_seconds": None, "fields": None}


async def _count(engine: AsyncEngine, tenant_id: uuid.UUID) -> int:
    return int(
        await sql_scalar(engine, "SELECT count(*) FROM shop WHERE tenant_id = :t", t=tenant_id)
    )


async def test_second_shop_rejected_when_first_not_connected(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т1: 409, доступы и версия существующей записи не меняются."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)
    before = await read_shop(raw_engine, shop_id)

    response = await client.post("/api/shops", json=BODY)

    assert response.status_code == 409
    assert response.json() == LIMIT
    assert await _count(raw_engine, tenant) == 1
    after = await read_shop(raw_engine, shop_id)
    assert after == before
    assert after["connection_version"] == before["connection_version"]


async def test_second_shop_rejected_when_first_connected(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т2: подключённый магазин и его товар не меняются."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await connected_shop(deps, raw_engine, tenant)
    await seed_product(raw_engine, tenant, shop_id)
    before = await read_shop(raw_engine, shop_id)
    products = "SELECT count(*) FROM product WHERE shop_id = :s"

    response = await client.post("/api/shops", json=BODY)

    assert response.status_code == 409
    assert response.json() == LIMIT
    assert await _count(raw_engine, tenant) == 1
    assert await read_shop(raw_engine, shop_id) == before
    assert await sql_scalar(raw_engine, products, s=shop_id) == 1


async def test_old_install_with_two_shops_lists_both_and_rejects_post(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т3: два подключённых магазина старой установки остаются видимыми; новый добавить нельзя."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    first = await connected_shop(deps, raw_engine, tenant, merchant_uid="M1")
    second = await connected_shop(deps, raw_engine, tenant, merchant_uid="M2")

    listed = await client.get("/api/shops")
    response = await client.post("/api/shops", json=BODY)

    assert [item["id"] for item in listed.json()] == [str(first), str(second)]
    assert response.status_code == 409
    assert response.json() == LIMIT
    assert await _count(raw_engine, tenant) == 2


async def test_parallel_posts_give_one_created_and_one_conflict(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: Any
) -> None:
    """Т4: второй запрос встаёт на блокировке арендатора и после первого получает 409."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    locked = asyncio.Event()
    release = asyncio.Event()
    original = ShopRepository.list
    calls = 0

    async def slow_list(self: ShopRepository) -> Any:
        nonlocal calls
        calls += 1
        result = await original(self)
        if calls == 1:
            locked.set()
            await release.wait()
        return result

    monkeypatch.setattr(ShopRepository, "list", slow_list)

    async def post() -> httpx.Response:
        return await client.post("/api/shops", json=BODY)

    first = asyncio.create_task(post())
    second: asyncio.Task[httpx.Response] | None = None
    try:
        await asyncio.wait_for(locked.wait(), 5)
        second = asyncio.create_task(post())
        waiting = 0
        for _ in range(100):
            waiting = int(
                await sql_scalar(
                    raw_engine,
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'",
                )
            )
            if waiting:
                break
            await asyncio.sleep(0.05)
        assert waiting == 1, "второй запрос не ждёт блокировку арендатора"
    finally:
        release.set()
    responses = [await first]
    if second is not None:
        responses.append(await second)

    assert sorted(r.status_code for r in responses) == [201, 409]
    assert await _count(raw_engine, tenant) == 1


async def test_other_tenant_shop_does_not_block_post(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т5: ограничение на арендатора; магазин A не мешает арендатору B."""
    await signed_in(deps, "a@example.com")
    client_b = await signed_in(deps, "b@example.com")
    tenant_a = await tenant_of(raw_engine, "a@example.com")
    tenant_b = await tenant_of(raw_engine, "b@example.com")
    await seed_shop(deps, raw_engine, tenant_a)

    response = await client_b.post("/api/shops", json={"login": KASPI_LOGIN, "password": "pw"})

    assert response.status_code == 201
    assert await _count(raw_engine, tenant_a) == 1
    assert await _count(raw_engine, tenant_b) == 1


async def test_connection_continues_on_the_same_record(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т8: после 409 вход и код работают с той же записью."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_connect_shop(deps, raw_engine, tenant, aged=False)
    fake = fake_client(deps)
    fake.queue("login", code_required())
    fake.queue("submit_code", logged_in("M1"))

    creds = {"login": KASPI_LOGIN, "password": KASPI_PASSWORD}
    rejected = await client.post("/api/shops", json=creds)
    login = await client.post(f"/api/shops/{shop_id}/login")
    code = await client.post(f"/api/shops/{shop_id}/code", json={"code": "123456"})

    assert rejected.status_code == 409
    assert login.json()["outcome"] == "code_required"
    assert code.json()["shop"]["connection_state"] == "connected"
    assert await _count(raw_engine, tenant) == 1
    row = await read_shop(raw_engine, shop_id)
    assert row["connection_state"] == "connected"


async def test_invalid_body_gives_422_not_409(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Проверка тела идёт раньше проверки лимита."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    await seed_shop(deps, raw_engine, tenant)

    response = await client.post("/api/shops", json={"login": ""})

    assert response.status_code == 422
    assert await _count(raw_engine, tenant) == 1


async def test_anonymous_post_gives_401_not_409(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    await seed_shop(deps, raw_engine, tenant)
    client.cookies.clear()

    response = await client.post("/api/shops", json=BODY)

    assert response.status_code == 401


async def test_shop_can_be_added_again_after_delete(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Смена магазина: удалить текущий и добавить новый."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant)

    assert (await client.post("/api/shops", json=BODY)).status_code == 409
    assert (await client.delete(f"/api/shops/{shop_id}")).status_code == 204
    assert (await client.post("/api/shops", json=BODY)).status_code == 201
    assert (await client.post("/api/shops", json=BODY)).status_code == 409
    assert await _count(raw_engine, tenant) == 1
