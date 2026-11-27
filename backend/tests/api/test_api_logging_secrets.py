"""ADR 0017, Т18: журнал API с уровнем INFO не содержит секретов и данных каталога.

Сценарий 1: вход с кодом и загрузка каталога на настоящем `KaspiClient` с `respx`.
Сценарий 2: Kaspi отклонил логин и пароль. Обработчик читает вывод логгеров `kaspisa.kaspi`
и `kaspisa.api` при уровнях, которые выставил `configure_logging`.
"""

import dataclasses
import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import respx
from api_kaspi_testing import (
    DEVICE_MARKER,
    IDMC_MARKER,
    LIST_URL,
    LOGIN_API_URL,
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
    synthetic_login_error,
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
    redis_client,
    redis_prefix,
    seed_connect_shop,
    signed_in,
    sql_scalar,
    sync_clock,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.kaspi import KaspiClient
from kaspisa.main import configure_logging
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
SECRETS = [
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
LOGGERS = ("kaspisa.kaspi", "kaspisa.api")


@contextmanager
def _stream() -> Iterator[io.StringIO]:
    """Тестовый обработчик на обоих логгерах; уровни остаются теми, что дал `configure_logging`."""
    configure_logging()
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    for name in LOGGERS:
        logging.getLogger(name).addHandler(handler)
    try:
        yield out
    finally:
        for name in LOGGERS:
            logging.getLogger(name).removeHandler(handler)


def _limiter(deps: ApiDeps, fake_time: FakeTime) -> RedisIntervalLimiter:
    return RedisIntervalLimiter(
        deps.redis,
        prefix=deps.login_lock._prefix,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        max_wait=10,
    )


def _login_lines(text: str, operation: str) -> list[str]:
    return [line for line in text.splitlines() if f"kaspi {operation} POST" in line]


async def test_api_log_has_login_steps_and_no_secrets_on_success(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    fake_time = FakeTime()
    signed = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_connect_shop(deps, raw_engine, tenant, aged=False)
    await sync_clock(deps, raw_engine)

    with _stream() as out:
        async with KaspiClient(limiter=_limiter(deps, fake_time), sleep=fake_time.sleep) as kaspi:
            client = make_api(dataclasses.replace(deps, client=kaspi))
            client.cookies.update(signed.cookies)
            with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
                synthetic_oauth_start(mock)
                login = mock.post(LOGIN_API_URL).mock(
                    side_effect=[
                        captured_login_reply("пароль", email=MAIL_MARKER),
                        captured_login_reply("код из письма"),
                    ]
                )
                synthetic_oauth_return(mock)
                mock.get(MERCHANTS_URL).mock(return_value=captured_merchants())
                await client.post(f"/api/shops/{shop_id}/login")
                await client.post(f"/api/shops/{shop_id}/code", json={"code": EMAIL_CODE})
                page = load_fixture("product-list.json")
                page["total"] = len(page["data"])
                mock.post(url__startswith=LIST_URL).mock(
                    return_value=httpx.Response(201, json=page)
                )
                synced = await client.post(f"/api/shops/{shop_id}/products/sync")
        text = out.getvalue()
        posts = login.call_count

    assert synced.json()["outcome"] == "synced"
    assert posts == 2
    assert text.count("kaspi ") >= posts
    assert len(_login_lines(text, "login")) + len(_login_lines(text, "submit_code")) == posts
    assert "kaspi list_products" in text
    for secret in SECRETS:
        assert secret not in text, secret
    for row in page["data"]:
        for value in (row.get("sku"), row.get("masterTitle")):
            if isinstance(value, str) and len(value) >= 6:
                assert value not in text, value


async def test_api_log_has_no_secrets_when_password_is_rejected(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    fake_time = FakeTime()
    signed = await signed_in(deps)
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_connect_shop(deps, raw_engine, tenant, aged=False)
    await sync_clock(deps, raw_engine)

    with _stream() as out:
        async with KaspiClient(limiter=_limiter(deps, fake_time), sleep=fake_time.sleep) as kaspi:
            client = make_api(dataclasses.replace(deps, client=kaspi))
            client.cookies.update(signed.cookies)
            with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
                synthetic_oauth_start(mock)
                login = mock.post(LOGIN_API_URL).mock(
                    return_value=synthetic_login_error("WRONG_PASSWORD")
                )
                response = await client.post(f"/api/shops/{shop_id}/login")
        text = out.getvalue()
        posts = login.call_count

    assert response.json()["outcome"] == "rejected"
    assert posts == 1
    assert len(_login_lines(text, "login")) == posts
    for secret in SECRETS:
        assert secret not in text, secret


async def test_reset_token_of_added_user_is_shown_once_and_not_logged(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т24: токен из `POST /api/users` не в логах, уведомлении, `repr`, списке и журнале."""
    from api_notify_testing import notifier_of, owner, sent_messages, url_of

    from kaspisa.api.schemas import UserAddedOut

    client, _ = await owner(deps, raw_engine, "admin@example.com", confirmed=True)
    with _stream() as out, respx.mock(assert_all_called=False) as mock:
        mock.post(url_of("123456789:" + "A" * 35)).respond(200)
        added = await client.post("/api/users", json={"email": "new@example.com"})
        await notifier_of(deps).drain()
        log_text = out.getvalue()
        messages = sent_messages(mock)
    assert added.status_code == 201
    token = added.json()["reset_token"]
    assert token not in log_text
    assert [token in m.text for m in messages] == [False]
    assert token not in repr(UserAddedOut.model_validate(added.json()))
    second = await client.post("/api/users", json={"email": "other@example.com"})
    assert second.status_code == 201
    for path in ("/api/users", "/api/events"):
        assert token not in (await client.get(path)).text
    assert token not in second.text
    dump = await sql_scalar(
        raw_engine, "SELECT string_agg(CAST(e AS text), '|') FROM owner_event e"
    )
    assert token not in dump


async def test_confirm_password_is_not_logged_or_stored(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т25: пароль подтверждения (верный и неверный) не в логах, `repr` модели и журнале."""
    from kaspisa.api.schemas import PasswordConfirmIn

    wrong = "WRONG-confirm-9e1b7c"
    client = await signed_in(deps)
    with _stream() as out:
        bad = await client.post("/api/auth/confirm-password", json={"password": wrong})
        good = await client.post("/api/auth/confirm-password", json={"password": SERVICE_PASSWORD})
        text = out.getvalue()
    assert (bad.status_code, good.status_code) == (403, 204)
    dump = "SELECT string_agg(CAST(e AS text), '|') FROM owner_event e"
    rows = await sql_scalar(raw_engine, dump)
    assert rows  # неверный пароль оставил событие
    for secret in (wrong, SERVICE_PASSWORD):
        assert secret not in text
        assert secret not in repr(PasswordConfirmIn(password=secret))
        assert secret not in bad.text + good.text
        assert secret not in rows
