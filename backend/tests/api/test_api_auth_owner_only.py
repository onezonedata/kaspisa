"""Т1, Т4, Т7, Т21: режим «только владелец» (ADR 0027)."""

import asyncio

from api_testing import (
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    deps,
    make_api,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_shop,
    signed_in,
    sql_scalar,
    tenant_of,
)
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


def _body(email: str) -> dict[str, str]:
    return {"email": email, "password": SERVICE_PASSWORD}


async def test_register_after_first_account_is_403(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т1."""
    client = make_api(deps)

    first = await client.post("/api/auth/register", json=_body("a@example.com"))
    second = await make_api(deps).post("/api/auth/register", json=_body("b@example.com"))

    assert first.status_code == 201
    assert second.status_code == 403
    assert second.json()["code"] == "registration_closed"
    assert "set-cookie" not in second.headers
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM app_user") == 1
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM tenant") == 1


async def test_parallel_first_registrations(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т4."""
    clients = [make_api(deps, ip=f"10.4.0.{i}") for i in range(4)]

    responses = await asyncio.gather(
        *(
            client.post("/api/auth/register", json=_body(f"u{i}@example.com"))
            for i, client in enumerate(clients)
        )
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 403, 403, 403]
    assert {r.json()["code"] for r in responses if r.status_code == 403} == {"registration_closed"}
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM app_user") == 1
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM tenant") == 1


async def test_empty_tenant_does_not_close_registration(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т7: арендатор без пользователей (остаток прежней гонки) регистрацию не закрывает."""
    async with deps.database.system() as system:
        await system.create_tenant(name="orphan")
        await system.commit()

    response = await make_api(deps).post("/api/auth/register", json=_body("a@example.com"))

    assert response.status_code == 201, response.text
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM app_user") == 1


async def test_second_signed_in_user_is_isolated(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т21: второй пользователь `signed_in` живёт в своей учётной записи."""
    client_a = await signed_in(deps, "a@example.com")
    tenant_a = await tenant_of(raw_engine, "a@example.com")
    shop_a = await seed_shop(deps, raw_engine, tenant_a)
    client_b = await signed_in(deps, "b@example.com")

    me = await client_b.get("/api/auth/me")
    shops = await client_b.get("/api/shops")
    foreign = await client_b.get(f"/api/shops/{shop_a}")

    assert (await client_a.get("/api/auth/me")).json()["email"] == "a@example.com"
    assert me.status_code == 200
    assert me.json()["email"] == "b@example.com"
    assert shops.status_code == 200
    assert shops.json() == []
    assert foreign.status_code == 404
    assert await tenant_of(raw_engine, "b@example.com") != tenant_a
