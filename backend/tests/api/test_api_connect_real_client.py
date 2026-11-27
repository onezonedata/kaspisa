"""Т50-Т51: вход через API на настоящем `KaspiClient` с `respx` и снятыми ответами.

Переходы OAuth синтетические (не снято): тест не подтверждает живой вход Kaspi (риск Р12).
После входа до `connected` на том же клиенте идёт загрузка каталога (P1, хвост Т50).
"""

import dataclasses
import logging

import httpx
import pytest
import respx
from api_kaspi_testing import (
    DEVICE_MARKER,
    IDMC_MARKER,
    LIST_URL,
    LOGIN_API_URL,
    MERCHANT_UID,
    MERCHANTS_URL,
    NEW_SESSION_MARKER,
    NEW_SID_MARKER,
    OAUTH_MARKER,
    START_STATE_MARKER,
    TICKET_MARKER,
    FakeTime,
    captured_login_reply,
    captured_merchants,
    load_fixture,
    synthetic_oauth_return,
    synthetic_oauth_start,
)
from api_testing import (
    EMAIL_CODE,
    KASPI_LOGIN,
    KASPI_PASSWORD,
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    deps,
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
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.kaspi import KaspiClient
from kaspisa.worker.redis_limits import RedisIntervalLimiter

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

MAIL_MARKER = "mail-7f3a@example.invalid"


async def test_full_login_on_real_client_without_secret_leaks(
    deps: ApiDeps, raw_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Т50 (вход до `connected`) и Т51 (секреты не попадают в журнал)."""
    fake_time = FakeTime()
    limiter = RedisIntervalLimiter(
        deps.redis,
        prefix=deps.login_lock._prefix,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        max_wait=10,
    )
    signed = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_connect_shop(deps, raw_engine, tenant, aged=False)
    await sync_clock(deps, raw_engine)
    caplog.set_level(logging.DEBUG)
    transcript: list[str] = []

    async with KaspiClient(limiter=limiter, sleep=fake_time.sleep) as kaspi:
        client = make_api(dataclasses.replace(deps, client=kaspi))
        client.cookies.update(signed.cookies)
        with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
            synthetic_oauth_start(mock)
            mock.post(LOGIN_API_URL).mock(
                side_effect=[
                    captured_login_reply("пароль", email=MAIL_MARKER),
                    captured_login_reply("код из письма"),
                ]
            )
            synthetic_oauth_return(mock)
            mock.get(MERCHANTS_URL).mock(return_value=captured_merchants())

            first = await client.post(f"/api/shops/{shop_id}/login")
            second = await client.post(f"/api/shops/{shop_id}/code", json={"code": EMAIL_CODE})
            page = load_fixture("product-list.json")
            page["total"] = len(page["data"])  # в примере total=998: обходим одну страницу
            mock.post(url__startswith=LIST_URL).mock(return_value=httpx.Response(201, json=page))
            third = await client.post(f"/api/shops/{shop_id}/products/sync")
        transcript += [first.text, second.text, str(first.headers), str(second.headers)]
        transcript += [third.text, str(third.headers)]

    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == "code_required"
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "connected"
    assert second.json()["shop"]["merchant_uid"] == MERCHANT_UID
    assert third.status_code == 200, third.text
    assert third.json()["outcome"] == "synced"
    assert third.json()["added"] == len(page["data"])
    assert third.json()["added"] > 0
    row = await read_shop(raw_engine, shop_id)
    assert (row["connection_state"], row["connection_version"]) == ("connected", 2)
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert NEW_SESSION_MARKER in blob

    secrets = [
        KASPI_LOGIN,
        KASPI_PASSWORD,
        EMAIL_CODE,
        SERVICE_PASSWORD,
        MAIL_MARKER,
        NEW_SESSION_MARKER,
        NEW_SID_MARKER,
        DEVICE_MARKER,
        OAUTH_MARKER,
        IDMC_MARKER,
        TICKET_MARKER,
        START_STATE_MARKER,
    ]
    for secret in secrets:
        assert secret not in caplog.text, secret
        assert all(secret not in text for text in transcript), secret
