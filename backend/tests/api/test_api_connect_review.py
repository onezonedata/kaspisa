"""Т77-Т79: порядок «сеть -> база -> Redis» и перечитывание версии под блокировкой входа."""

import dataclasses
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from api_testing import (
    EMAIL_CODE,
    KASPI_LOGIN,
    KASPI_PASSWORD,
    api_cipher,
    api_database_url,
    code_required,
    deps,
    fake_client,
    kaspi_session,
    make_api,
    raw_engine,
    read_shop,
    redis_client,
    redis_prefix,
    seed_connect_shop,
    signed_in,
    stored_session,
    sync_clock,
    tenant_of,
    touch_session,
)
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.db import TenantId
from kaspisa.kaspi import KaspiTransportError
from kaspisa.worker.redis_limits import LoginLock

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]


async def _broken(*args: Any, **kwargs: Any) -> Any:
    raise RedisConnectionError("redis down")


async def test_timer_failure_after_write_keeps_awaiting_code(
    deps: ApiDeps,
    raw_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Т77."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_connect_shop(deps, raw_engine, tenant, aged=False)
    await sync_clock(deps, raw_engine)
    fake_client(deps).queue("login", code_required())
    monkeypatch.setattr(deps.state, "start_resend_timer", _broken)
    caplog.set_level(logging.WARNING)

    response = await client.post(f"/api/shops/{shop_id}/login")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "code_required"
    assert 170 < body["shop"]["resend_available_in_seconds"] <= 180
    row = await read_shop(raw_engine, shop_id)
    assert (row["connection_state"], row["connection_version"]) == ("awaiting_code", 1)
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert json.loads(blob)["phase"] == "awaiting_code"
    warnings = [r.getMessage() for r in caplog.records if "redis_after_write" in r.getMessage()]
    assert len(warnings) == 1
    assert str(shop_id) in warnings[0]
    assert KASPI_LOGIN not in caplog.text
    assert KASPI_PASSWORD not in caplog.text
    assert await deps.redis.get(f"{deps.state._prefix}:resend:{shop_id}") is None


async def test_mark_failure_after_write_keeps_session(
    deps: ApiDeps,
    raw_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Т78."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    before = (await read_shop(raw_engine, shop_id))["session_enc"]
    fake = fake_client(deps)
    fake.mutate_session = lambda session: touch_session(session, "COOKIE-7f3a")
    fake.queue("submit_code", KaspiTransportError("submit_code", "ReadError"))
    monkeypatch.setattr(deps.state, "mark_restart", _broken)
    caplog.set_level(logging.WARNING)

    response = await client.post(f"/api/shops/{shop_id}/code", json={"code": EMAIL_CODE})

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "restart_required"
    row = await read_shop(raw_engine, shop_id)
    assert row["session_enc"] != before
    assert (row["connection_state"], row["connection_version"]) == ("awaiting_code", 1)
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert "COOKIE-7f3a" in blob
    assert any("redis_after_write" in r.getMessage() for r in caplog.records)
    assert "COOKIE-7f3a" not in response.text
    assert "COOKIE-7f3a" not in caplog.text


class _InterferingLock:
    """Обёртка блокировки входа: перед выдачей блокировки другая область меняет магазин."""

    def __init__(self, inner: LoginLock, before: Callable[[uuid.UUID], Awaitable[None]]) -> None:
        self._inner = inner
        self._before = before

    @asynccontextmanager
    async def hold(self, shop_id: uuid.UUID, ttl_ms: int) -> AsyncIterator[bool]:
        await self._before(shop_id)
        async with self._inner.hold(shop_id, ttl_ms) as acquired:
            yield acquired


async def test_version_reread_under_login_lock(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т79."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    login_shop = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="reauth_required",
        session=kaspi_session(),
        version=2,
        error="session_expired",
    )
    resend_shop = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    versions = {login_shop: 2, resend_shop: 1}

    async def move_shop(shop_id: uuid.UUID) -> None:
        async with deps.database.tenant(TenantId(tenant)) as scope:
            await scope.shops.mark_connected(
                shop_id,
                merchant_uid=f"M-{shop_id.hex[:8]}",
                session_state=kaspi_session().dump(),
                expected_version=versions[shop_id],
            )
            await scope.commit()

    wrapped = dataclasses.replace(deps, login_lock=_InterferingLock(deps.login_lock, move_shop))  # type: ignore[arg-type]
    api = make_api(wrapped)
    api.cookies.update(client.cookies)

    login = await api.post(f"/api/shops/{login_shop}/login")
    resend = await api.post(f"/api/shops/{resend_shop}/code/resend")

    for response in (login, resend):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "state_changed"
    fake = fake_client(deps)
    assert fake.calls_of("login") == []
    assert fake.calls_of("resend_code") == []
    row = await read_shop(raw_engine, login_shop)
    assert (row["connection_state"], row["connection_version"]) == ("connected", 3)
    assert await deps.redis.get(f"{deps.state._prefix}:resend:{resend_shop}") is None
