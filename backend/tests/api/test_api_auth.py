"""Т3-Т6, Т8, Т9, Т11, Т12: регистрация, вход, выход, сброс пароля."""

import asyncio
import hashlib
from typing import Any

import pytest
from api_testing import (
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    deps,
    make_api,
    raw_engine,
    redis_client,
    redis_prefix,
    signed_in,
    sql_scalar,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api.reset_token import issue_reset_token, main
from kaspisa.config import Settings

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]


async def _keys(deps: ApiDeps, pattern: str) -> list[str]:
    found: list[Any] = [key async for key in deps.redis.scan_iter(pattern)]
    return [key.decode() if isinstance(key, bytes) else str(key) for key in found]


async def test_register_sets_cookie_and_opens_session(deps: ApiDeps) -> None:
    client = make_api(deps)

    response = await client.post(
        "/api/auth/register", json={"email": " A@Example.com ", "password": SERVICE_PASSWORD}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "a@example.com"
    assert set(body) == {"id", "email"}
    cookie = response.headers["set-cookie"]
    assert "kaspisa_session=" in cookie
    for attribute in ("HttpOnly", "Secure", "Max-Age=2592000", "Path=/"):
        assert attribute in cookie
    assert "samesite=lax" in cookie.lower()
    assert SERVICE_PASSWORD not in response.text
    assert "scrypt$" not in response.text
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "a@example.com"


async def test_register_taken_email(deps: ApiDeps) -> None:
    await signed_in(deps, "a@example.com")

    taken = await make_api(deps).post(
        "/api/auth/register", json={"email": "a@EXAMPLE.com", "password": SERVICE_PASSWORD}
    )
    other = await make_api(deps).post(
        "/api/auth/register", json={"email": "b@example.com", "password": SERVICE_PASSWORD}
    )
    short = await make_api(deps).post(
        "/api/auth/register", json={"email": "c@example.com", "password": "short"}
    )

    # ADR 0027: ответ «e-mail занят» недостижим, любое верное тело получает один и тот же 403.
    assert taken.status_code == 403
    assert taken.json()["code"] == "registration_closed"
    assert taken.content == other.content
    assert short.status_code == 422
    assert short.json()["code"] == "validation_error"
    async with deps.database.system() as system:
        assert (await system.find_user_by_email("a@example.com")) is not None


async def test_register_validation_hides_input(deps: ApiDeps) -> None:
    client = make_api(deps)

    short = await client.post(
        "/api/auth/register", json={"email": "a@example.com", "password": "sp-7f3a-x"}
    )
    bad_email = await client.post(
        "/api/auth/register", json={"email": "no-at-sign", "password": SERVICE_PASSWORD}
    )
    extra = await client.post(
        "/api/auth/register",
        json={"email": "a@example.com", "password": SERVICE_PASSWORD, "tenant_id": "x"},
    )

    assert short.status_code == 422
    assert "body.password" in short.json()["fields"]
    assert "sp-7f3a-x" not in short.text
    assert bad_email.status_code == 422
    assert "body.email" in bad_email.json()["fields"]
    assert extra.status_code == 422


async def test_login_success_and_failures(deps: ApiDeps) -> None:
    await signed_in(deps)
    client = make_api(deps)

    good = await client.post(
        "/api/auth/login", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )
    wrong = await client.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "wrong-password-1"}
    )
    unknown = await client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-password-1"}
    )

    assert good.status_code == 200
    assert "kaspisa_session=" in good.headers["set-cookie"]
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_credentials"
    assert unknown.status_code == 401
    assert unknown.content == wrong.content


async def test_logout_revokes_session(deps: ApiDeps, redis_prefix: str) -> None:
    client = await signed_in(deps)
    assert len(await _keys(deps, f"{redis_prefix}:api:sess:*")) == 1

    response = await client.post("/api/auth/logout")

    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert await _keys(deps, f"{redis_prefix}:api:sess:*") == []
    old = make_api(deps)
    old.cookies.set("kaspisa_session", "stale")
    assert (await old.get("/api/auth/me")).status_code == 401


async def test_logout_invalidates_old_cookie_value(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    token = client.cookies["kaspisa_session"]
    await client.post("/api/auth/logout")

    replay = make_api(deps)
    replay.cookies.set("kaspisa_session", token)

    assert (await replay.get("/api/auth/me")).status_code == 401


async def test_logout_without_cookie_is_204(deps: ApiDeps) -> None:
    response = await make_api(deps).post("/api/auth/logout")

    assert response.status_code == 204


async def test_me_without_valid_session(deps: ApiDeps) -> None:
    client = make_api(deps)
    without = await client.get("/api/auth/me")
    client.cookies.set("kaspisa_session", "forged")
    forged = await client.get("/api/auth/me")

    assert without.status_code == 401
    assert without.json()["code"] == "unauthenticated"
    assert forged.status_code == 401


async def test_password_reset_flow(deps: ApiDeps) -> None:
    first = await signed_in(deps)
    second = make_api(deps)
    await second.post(
        "/api/auth/login", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )
    token = await issue_reset_token(deps, "a@example.com")
    assert token is not None
    anon = make_api(deps)

    done = await anon.post(
        "/api/auth/password-reset", json={"token": token, "new_password": "service-pass-new1"}
    )
    again = await anon.post(
        "/api/auth/password-reset", json={"token": token, "new_password": "service-pass-new2"}
    )
    nope = await anon.post(
        "/api/auth/password-reset", json={"token": "nope", "new_password": "service-pass-new1"}
    )
    old_login = await anon.post(
        "/api/auth/login", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )
    new_login = await anon.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "service-pass-new1"}
    )

    assert done.status_code == 204
    assert (await first.get("/api/auth/me")).status_code == 401
    assert (await second.get("/api/auth/me")).status_code == 401
    assert again.status_code == 400
    assert again.json()["code"] == "invalid_token"
    assert nope.status_code == 400
    assert old_login.status_code == 401
    assert new_login.status_code == 200


async def test_reset_token_for_unknown_email(
    deps: ApiDeps, api_database_url: str, redis_prefix: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await issue_reset_token(deps, "x@none.kz") is None
    settings = Settings(database_url=api_database_url)

    code = await asyncio.to_thread(main, ["x@none.kz"], settings)

    out = capsys.readouterr()
    assert code == 1
    assert "x@none.kz" not in out.out + out.err


async def test_ttl_and_no_raw_tokens_in_redis(deps: ApiDeps, redis_prefix: str) -> None:
    client = await signed_in(deps)
    token = client.cookies["kaspisa_session"]
    reset = await issue_reset_token(deps, "a@example.com")
    assert reset is not None

    sess_keys = await _keys(deps, f"{redis_prefix}:api:sess:*")
    reset_keys = await _keys(deps, f"{redis_prefix}:api:reset:*")
    sess_ttl = await deps.redis.ttl(sess_keys[0])
    reset_ttl = await deps.redis.ttl(reset_keys[0])

    assert 2591990 < sess_ttl <= 2592000
    assert 3590 < reset_ttl <= 3600
    assert sess_keys[0].endswith(hashlib.sha256(token.encode()).hexdigest())
    for key in await _keys(deps, f"{redis_prefix}:api:*"):
        assert token not in key
        assert reset not in key


async def test_closed_registration_makes_no_tenant(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    await signed_in(deps, "a@example.com")
    before = await sql_scalar(raw_engine, "SELECT count(*) FROM tenant")

    again = await make_api(deps).post(
        "/api/auth/register", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )

    assert again.status_code == 403
    assert again.json()["code"] == "registration_closed"
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM tenant") == before
