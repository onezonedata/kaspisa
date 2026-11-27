"""Дополнение к Т43, Т72: границы таймера письма (180 с) и таймер из Redis."""

import uuid
from datetime import datetime, timedelta

import httpx
from api_testing import (
    FakeClock,
    api_cipher,
    api_database_url,
    code_required,
    deps,
    fake_client,
    kaspi_session,
    raw_engine,
    read_shop,
    redis_client,
    redis_prefix,
    seed_connect_shop,
    signed_in,
    sync_clock,
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


def _clock(deps: ApiDeps) -> FakeClock:
    assert isinstance(deps.clock, FakeClock)
    return deps.clock


async def _awaiting(
    deps: ApiDeps, engine: AsyncEngine
) -> tuple[httpx.AsyncClient, uuid.UUID, datetime]:
    client = await signed_in(deps)
    tenant = await tenant_of(engine, "a@example.com")
    shop_id = await seed_connect_shop(
        deps,
        engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
        aged=False,
    )
    changed_at: datetime = (await read_shop(engine, shop_id))["connection_changed_at"]
    return client, shop_id, changed_at


async def test_resend_is_refused_until_the_180th_second(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, shop_id, changed_at = await _awaiting(deps, raw_engine)
    clock = _clock(deps)
    fake = fake_client(deps)
    url = f"/api/shops/{shop_id}/code/resend"

    clock.now = changed_at + timedelta(seconds=179.5)
    early = await client.post(url)
    assert early.status_code == 429
    assert early.json()["retry_after_seconds"] == 1
    assert fake.calls == []

    clock.now = changed_at + timedelta(seconds=180)
    fake.queue("resend_code", None)
    ready = await client.post(url)
    assert ready.status_code == 200, ready.text
    assert ready.json()["outcome"] == "code_sent"


async def test_letter_sent_at_login_blocks_resend_via_redis_timer(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Письмо ушло при входе: повтор закрыт таймером в Redis, даже если часы ушли вперёд."""
    client = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_connect_shop(deps, raw_engine, tenant)
    await sync_clock(deps, raw_engine)
    fake = fake_client(deps)
    fake.queue("login", code_required())
    assert (await client.post(f"/api/shops/{shop_id}/login")).json()["outcome"] == "code_required"

    _clock(deps).advance(400)
    response = await client.post(f"/api/shops/{shop_id}/code/resend")

    assert response.status_code == 429
    assert response.json()["code"] == "resend_too_early"
    assert fake.calls_of("resend_code") == []
