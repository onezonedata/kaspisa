"""Т80-Т82: очередь хеширования, сброс во время входа, секреты в `repr` (дельта 1 ревью)."""

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable

import httpx
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
    sql_exec,
    sql_scalar,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps, passwords
from kaspisa.api.reset_token import issue_reset_token
from kaspisa.api.schemas import CodeIn, LoginIn, PasswordResetIn, RegisterIn, ShopCredentialsIn
from kaspisa.db import TenantId

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]


async def _add_user(deps: ApiDeps, email: str) -> uuid.UUID:
    """Пользователь с настоящим хешем без вызова асинхронного шлюза хеширования."""
    async with deps.database.system() as system:
        tenant = await system.create_tenant(name=email)
        await system.commit()
    async with deps.database.tenant(TenantId(tenant.id)) as scope:
        user = await scope.users.add(
            email=email, password_hash=passwords.hash_password(SERVICE_PASSWORD)
        )
        await scope.commit()
    return user.id


async def test_hash_queue_is_bounded(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т80."""
    await _add_user(deps, "reset@example.com")
    token = await issue_reset_token(deps, "reset@example.com")
    assert token is not None
    monkeypatch.setattr(passwords, "HASH_CONCURRENCY", 2)
    monkeypatch.setattr(passwords, "HASH_QUEUE_LIMIT", 2)
    release = threading.Event()
    calls: list[str] = []

    def slow_verify(password: str, stored: str) -> bool:
        calls.append("verify")
        release.wait(10)
        return False

    def slow_hash(password: str) -> str:
        calls.append("hash")
        release.wait(10)
        return passwords.DUMMY_HASH

    monkeypatch.setattr(passwords, "verify_password", slow_verify)
    monkeypatch.setattr(passwords, "hash_password", slow_hash)
    tenants_before = await sql_scalar(raw_engine, "SELECT count(*) FROM tenant")
    client = make_api(deps)

    async def login(index: int) -> httpx.Response:
        return await client.post(
            "/api/auth/login", json={"email": f"u{index}@example.com", "password": "x" * 12}
        )

    pending = [asyncio.create_task(login(i)) for i in range(4)]
    deadline = time.monotonic() + 5
    while len(calls) < 2 and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)  # остальные два запроса уже допущены и ждут семафор
    assert len(calls) == 2

    busy = await login(5)
    register = await client.post(
        "/api/auth/register", json={"email": "new@example.com", "password": SERVICE_PASSWORD}
    )
    reset = await client.post(
        "/api/auth/password-reset", json={"token": token, "new_password": "service-pass-new1"}
    )

    for response in (busy, reset):
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "server_busy"
        assert response.json()["retry_after_seconds"] == 1
    # Установка не пуста: регистрация закрыта до хеширования, шлюз ей не нужен (ADR 0027).
    assert register.status_code == 403, register.text
    assert register.json()["code"] == "registration_closed"
    assert register.json()["retry_after_seconds"] is None
    assert len(calls) == 2
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM tenant") == tenants_before
    assert (
        await sql_scalar(
            raw_engine, "SELECT count(*) FROM app_user WHERE email = 'new@example.com'"
        )
        == 0
    )

    release.set()
    done = await asyncio.wait_for(asyncio.gather(*pending), 10)
    assert [r.status_code for r in done] == [401] * 4
    assert {r.json()["code"] for r in done} == {"invalid_credentials"}
    again = await client.post(
        "/api/auth/password-reset", json={"token": token, "new_password": "service-pass-new1"}
    )
    assert again.status_code == 204  # токен не сожжён отказом очереди


async def test_reset_between_check_and_session(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т81."""
    user_id = await _add_user(deps, "a@example.com")
    body = {"email": "a@example.com", "password": SERVICE_PASSWORD}
    control = await make_api(deps).post("/api/auth/login", json=body)
    assert control.status_code == 200, control.text
    await deps.state.revoke_user_sessions(user_id)

    original: Callable[[uuid.UUID, TenantId], Awaitable[str]] = deps.state.create_session

    async def reset_first(uid: uuid.UUID, tenant_id: TenantId) -> str:
        await sql_exec(
            raw_engine, "UPDATE app_user SET password_hash = 'changed' WHERE id = :id", id=uid
        )
        await deps.state.revoke_user_sessions(uid)
        return await original(uid, tenant_id)

    monkeypatch.setattr(deps.state, "create_session", reset_first)
    client = make_api(deps)

    response = await client.post("/api/auth/login", json=body)

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "invalid_credentials"
    assert "set-cookie" not in response.headers
    prefix = deps.state._prefix
    sessions = [key async for key in deps.redis.scan_iter(f"{prefix}:sess:*")]
    assert sessions == []
    members = await deps.redis.smembers(f"{prefix}:user_sess:{user_id}")
    assert not members


def test_secrets_hidden_in_repr() -> None:
    """Т82."""
    secrets = [
        "service-pass-7f3a",
        "TOKEN-7f3a",
        "seller-7f3a@shop.kz",
        "KASPI-PASS-7f3a",
        "482913",
    ]
    models = [
        RegisterIn(email="a@example.com", password="service-pass-7f3a"),
        LoginIn(email="a@example.com", password="service-pass-7f3a"),
        PasswordResetIn(token="TOKEN-7f3a", new_password="service-pass-7f3a"),
        ShopCredentialsIn(login="seller-7f3a@shop.kz", password="KASPI-PASS-7f3a"),
        CodeIn(code="482913"),
    ]
    for model in models:
        for text in (repr(model), str(model)):
            for secret in secrets:
                assert secret not in text, (type(model).__name__, secret)
