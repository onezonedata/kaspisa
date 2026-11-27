"""Т66: в логах слайса нет секретов и тел запросов Kaspi."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import base64
import logging
from datetime import timedelta

import pytest
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    Rig,
    execute,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    worker_cipher,
    worker_database_url,
)
from writer_testing import prepare

from kaspisa.kaspi import (
    KaspiTransportError,
    LoginRejectedError,
    PriceWriteOutcomeUnknownError,
    ResponseFormatError,
    SessionExpiredError,
)
from kaspisa.security import Secret

LOGIN = "LOGIN-MARKER-7f3a"
SECRET_MARKER = "PASSWORD-MARKER-9c1d"  # noqa: S105
BODY = "BODY-MARKER-55aa"


async def test_slice_logs_contain_no_secrets(
    rig: Rig, redis_client: Redis, redis_prefix: str, caplog: pytest.LogCaptureFixture
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, products=2)
    async with p.database.tenant(p.shop.tenant_id) as scope:
        await scope.shops.set_credentials(
            p.shop.shop_id, login=Secret(LOGIN), password=Secret(SECRET_MARKER)
        )
        await scope.commit()
    key = rig.cipher._keyring._keys[1]
    markers = [
        LOGIN,
        SECRET_MARKER,
        BODY,
        "marker-mc-session",
        "marker-mc-sid",
        "session_enc",
        key.hex(),
        base64.b64encode(key).decode(),
    ]
    caplog.set_level(logging.DEBUG)
    client = p.client
    client.errors["find_product"] = KaspiTransportError("find_product", BODY)
    await run_one_slice(p.ctx, p.database, NOW)
    del client.errors["find_product"]
    client.errors["set_price"] = PriceWriteOutcomeUnknownError(p.sku, BODY)
    p.clock.advance(61)
    await run_one_slice(p.ctx, p.database, p.clock())
    del client.errors["set_price"]
    client.errors["find_product"] = SessionExpiredError(BODY)
    client.login_results = [LoginRejectedError("TIMEOUT_EXCEEDED", 30)]
    p.clock.advance(700)
    await run_one_slice(p.ctx, p.database, p.clock())
    client.errors["find_product"] = ResponseFormatError(BODY, (BODY,))
    await execute(
        rig,
        "UPDATE shop SET connection_state='connected', connection_error=NULL, "
        "claimed_by=NULL, claimed_until=NULL WHERE id=:i",
        i=p.shop.shop_id,
    )
    p.clock.advance(700)
    await run_one_slice(p.ctx, p.database, p.clock())
    assert caplog.records  # слайсы что-то писали в лог
    text = "\n".join(
        record.getMessage() + " " + (record.exc_text or "") + str(record.__dict__.get("args", ""))
        for record in caplog.records
    )
    for marker in markers:
        assert marker not in text, marker
