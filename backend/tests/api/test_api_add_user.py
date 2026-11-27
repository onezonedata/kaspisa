"""Т9, Т10: команда `python -m kaspisa.api.add_user OWNER_EMAIL EMAIL` (ADR 0027)."""

import asyncio

import pytest
from api_testing import (
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    deps,
    make_api,
    raw_engine,
    redis_client,
    redis_port,
    redis_prefix,
    seed_shop,
    signed_in,
    sql_scalar,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps, add_user
from kaspisa.api.add_user import main
from kaspisa.api.redis_state import RedisState
from kaspisa.config import Settings

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]


def _settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, redis_url=f"redis://localhost:{redis_port()}/0")


async def test_add_user_joins_given_account(
    deps: ApiDeps,
    raw_engine: AsyncEngine,
    api_database_url: str,
    redis_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Т9."""
    monkeypatch.setattr(add_user, "REDIS_API_PREFIX", f"{redis_prefix}:api")
    await signed_in(deps, "a@example.com")
    await signed_in(deps, "b@example.com")
    shop_a = await seed_shop(deps, raw_engine, await tenant_of(raw_engine, "a@example.com"))
    shop_b = await seed_shop(deps, raw_engine, await tenant_of(raw_engine, "b@example.com"))
    events_before = await sql_scalar(raw_engine, "SELECT count(*) FROM owner_event")

    code = await asyncio.to_thread(
        main, ["B@example.com", "Staff@Example.com"], _settings(api_database_url)
    )

    out = capsys.readouterr()
    assert code == 0
    token_lines = out.out.splitlines()
    assert len(token_lines) == 1
    token = token_lines[0]
    assert token
    assert "b@example.com" in out.err
    assert "агент" in out.err
    assert token not in out.err
    assert await tenant_of(raw_engine, "staff@example.com") == await tenant_of(
        raw_engine, "b@example.com"
    )
    # Т26: команда создаёт обычного пользователя и событий в журнал не пишет
    assert (
        await sql_scalar(
            raw_engine, "SELECT is_admin FROM app_user WHERE email = :e", e="staff@example.com"
        )
        is False
    )
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM owner_event") == events_before
    client = make_api(deps)
    credentials = {"email": "staff@example.com", "password": SERVICE_PASSWORD}
    assert (await client.post("/api/auth/login", json=credentials)).status_code == 401
    reset = await client.post(
        "/api/auth/password-reset", json={"token": token, "new_password": SERVICE_PASSWORD}
    )
    assert reset.status_code == 204
    assert (await client.post("/api/auth/login", json=credentials)).status_code == 200
    shops = await client.get("/api/shops")
    assert [item["id"] for item in shops.json()] == [str(shop_b)]
    assert str(shop_a) not in shops.text


async def test_add_user_redis_failure_reports_created_user(
    deps: ApiDeps,
    raw_engine: AsyncEngine,
    api_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Сбой Redis после создания сотрудника: сообщение про `reset_token`, без секретов."""
    await signed_in(deps, "a@example.com")

    async def broken_put(*_args: object, **_kwargs: object) -> str:
        raise ConnectionError("redis://:redis-secret-pw@10.9.8.7:6379")

    monkeypatch.setattr(RedisState, "put_reset_token", broken_put)

    code = await asyncio.to_thread(
        main, ["a@example.com", "staff@example.com"], _settings(api_database_url)
    )

    out = capsys.readouterr()
    assert code == 2
    assert out.out == ""
    assert "staff@example.com" in out.err
    assert "создан" in out.err
    assert "kaspisa.api.reset_token" in out.err
    for leaked in ("redis-secret-pw", "10.9.8.7"):
        assert leaked not in out.err
    created = await sql_scalar(
        raw_engine, "SELECT count(*) FROM app_user WHERE email = :e", e="staff@example.com"
    )
    assert created == 1


async def test_add_user_errors(
    deps: ApiDeps,
    raw_engine: AsyncEngine,
    api_database_url: str,
    redis_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Т10."""
    monkeypatch.setattr(add_user, "REDIS_API_PREFIX", f"{redis_prefix}:api")
    await signed_in(deps, "a@example.com")
    settings = _settings(api_database_url)
    users_before = await sql_scalar(raw_engine, "SELECT count(*) FROM app_user")

    async def run(*args: str, with_settings: Settings = settings) -> tuple[int, str]:
        code = await asyncio.to_thread(main, list(args), with_settings)
        return code, capsys.readouterr().err

    assert (await run("nobody@example.com", "staff@example.com"))[0] == 1
    assert (await run("a@example.com", "A@example.com"))[0] == 1  # адрес занят
    assert (await run("a@example.com"))[0] == 2
    assert (await run("a@example.com", "b@example.com", "c@example.com"))[0] == 2
    assert (await run("no-at-sign", "staff@example.com"))[0] == 2
    assert (await run("a@example.com", "no at sign"))[0] == 2
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM app_user") == users_before

    broken = _settings("postgresql+asyncpg://svc:topsecret-pw@127.0.0.1:1/nowhere")
    code, err = await run("a@example.com", "staff@example.com", with_settings=broken)
    assert code == 2
    assert err.startswith("ошибка запуска: ")
    assert len(err.strip().splitlines()) == 1
    for leaked in ("topsecret-pw", "127.0.0.1", "svc", "nowhere"):
        assert leaked not in err
