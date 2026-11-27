"""Т30-Т49, Т52, Т72-Т75: подключение магазина к Kaspi (C1-C4) с заглушкой клиента."""

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from api_testing import (
    EMAIL_CODE,
    KASPI_COOKIE,
    KASPI_LOGIN,
    KASPI_PASSWORD,
    ROUTES,
    FakeClock,
    FakeConnectClient,
    api_cipher,
    api_database_url,
    code_required,
    deps,
    fake_client,
    kaspi_session,
    logged_in,
    raw_engine,
    read_shop,
    redis_client,
    redis_prefix,
    seed_connect_shop,
    seed_price_write,
    seed_product,
    seed_shop,
    signed_in,
    sql_exec,
    stored_session,
    sync_clock,
    tenant_of,
    touch_session,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps, create_app
from kaspisa.db import TenantId
from kaspisa.db import create_database as _create_database
from kaspisa.kaspi import (
    KaspiTransportError,
    LoginRejectedError,
    RateLimitedError,
    ResponseFormatError,
    SessionStateError,
    UnexpectedStatusError,
)
from kaspisa.security import KeyRing, Secret, SecretCipher
from kaspisa.worker.redis_limits import LimiterBusyError, LimiterUnavailableError

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

CONNECT_TAILS = {"login", "login/restart", "code", "code/resend"}
CONNECT_ROUTES = [
    r
    for r in ROUTES
    if r.path.startswith("/api/shops/{shop_id}/")
    and r.path.split("/{shop_id}/")[1] in CONNECT_TAILS
]
ALLOWED_ERRORS = {
    "multiple_merchants",
    "no_merchants",
    "merchant_mismatch",
    "shop_already_connected",
    "FAILED",
}


def _clock(deps: ApiDeps) -> FakeClock:
    assert isinstance(deps.clock, FakeClock)
    return deps.clock


async def _setup(
    deps: ApiDeps, engine: AsyncEngine, email: str = "a@example.com"
) -> tuple[httpx.AsyncClient, uuid.UUID]:
    client = await signed_in(deps, email)
    return client, await tenant_of(engine, email)


def _url(shop_id: uuid.UUID, tail: str) -> str:
    return f"/api/shops/{shop_id}/{tail}"


async def _post(client: httpx.AsyncClient, shop_id: uuid.UUID, tail: str, **kw: Any) -> Any:
    return await client.post(_url(shop_id, tail), **kw)


async def _resend_key(deps: ApiDeps, shop_id: uuid.UUID) -> bytes | str | None:
    result: bytes | str | None = await deps.redis.get(f"{deps.state._prefix}:resend:{shop_id}")
    return result


async def _drop_resend_key(deps: ApiDeps, shop_id: uuid.UUID) -> None:
    await deps.redis.delete(f"{deps.state._prefix}:resend:{shop_id}")


async def test_login_requests_code(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т30."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(deps, raw_engine, tenant, aged=False)
    await sync_clock(deps, raw_engine)
    fake_client(deps).queue("login", code_required())

    response = await _post(client, shop_id, "login")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "code_required"
    assert 170 < body["shop"]["resend_available_in_seconds"] <= 180
    row = await read_shop(raw_engine, shop_id)
    assert (row["connection_state"], row["connection_version"]) == ("awaiting_code", 1)
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert json.loads(blob)["phase"] == "awaiting_code"
    calls = fake_client(deps).calls_of("login")
    assert len(calls) == 1
    assert calls[0]["previous"] is None
    assert calls[0]["credentials"].login.reveal() == KASPI_LOGIN
    assert calls[0]["credentials"].password.reveal() == KASPI_PASSWORD


async def test_login_from_reauth_passes_previous_session(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т31."""
    client, tenant = await _setup(deps, raw_engine)
    saved = kaspi_session()
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="reauth_required",
        session=saved,
        version=2,
        error="session_expired",
    )
    fake_client(deps).queue("login", logged_in("M1"))

    response = await _post(client, shop_id, "login")

    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "connected"
    assert body["shop"]["connection_state"] == "connected"
    assert body["shop"]["merchant_uid"] == "M1"
    assert body["shop"]["connection_error"] is None
    assert fake_client(deps).calls_of("login")[0]["previous"] == saved


async def test_login_after_credentials_change_has_no_previous(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т32: правило ADR 0006 действует до первого успешного входа с новыми доступами."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="reauth_required",
        session=kaspi_session(),
        error="session_expired",
    )
    changed = await client.put(
        _url(shop_id, "credentials"), json={"login": "new@shop.kz", "password": "NEW-PASS-1"}
    )
    assert changed.status_code == 200
    assert changed.json()["connection_state"] == "not_connected"
    assert (await read_shop(raw_engine, shop_id))["session_enc"] is None
    fake = fake_client(deps)
    fake.queue("login", LoginRejectedError("FAILED"), code_required())

    first = await _post(client, shop_id, "login")

    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == "rejected"
    assert first.json()["shop"]["connection_state"] == "reauth_required"
    assert first.json()["shop"]["connection_error"] == "FAILED"
    assert (await read_shop(raw_engine, shop_id))["session_enc"] is None

    second = await _post(client, shop_id, "login")

    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "code_required"
    calls = fake.calls_of("login")
    assert [call["previous"] for call in calls] == [None, None]


async def test_credentials_change_conflict_keeps_session(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т32: 409 по открытой записи цены не стирает блок сессии."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps, raw_engine, tenant, state="connected", merchant_uid="M1", session=kaspi_session()
    )
    product = await seed_product(raw_engine, tenant, shop_id)
    await seed_price_write(
        raw_engine,
        tenant,
        shop_id,
        product,
        status="sending",
        sent_at=_clock(deps).now - timedelta(seconds=30),
    )
    before = (await read_shop(raw_engine, shop_id))["session_enc"]
    assert before is not None

    blocked = await client.put(
        _url(shop_id, "credentials"), json={"login": "new@shop.kz", "password": "NEW-PASS-1"}
    )

    assert blocked.status_code == 409
    assert (await read_shop(raw_engine, shop_id))["session_enc"] == before


async def test_operations_not_allowed_from_state(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т33."""
    client, tenant = await _setup(deps, raw_engine)
    connected = await seed_connect_shop(
        deps, raw_engine, tenant, state="connected", merchant_uid="M1", version=3
    )
    awaiting = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    fresh = await seed_connect_shop(deps, raw_engine, tenant)
    cases = [
        (connected, "login", None),
        (awaiting, "login", None),
        (fresh, "login/restart", None),
        (fresh, "code", {"code": EMAIL_CODE}),
        (fresh, "code/resend", None),
    ]

    for shop_id, tail, body in cases:
        response = await _post(client, shop_id, tail, json=body)
        assert response.status_code == 409, (tail, response.text)
        assert response.json()["code"] == "operation_not_allowed"

    assert fake_client(deps).calls == []
    versions = [
        (await read_shop(raw_engine, shop))["connection_version"]
        for shop in (connected, awaiting, fresh)
    ]
    assert versions == [3, 1, 0]


async def test_multiple_and_no_merchants(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т34."""
    client, tenant = await _setup(deps, raw_engine)
    many = await seed_connect_shop(deps, raw_engine, tenant)
    none = await seed_connect_shop(deps, raw_engine, tenant)
    fake_client(deps).queue("login", logged_in("M1", "M2"), logged_in())

    first = await _post(client, many, "login")
    second = await _post(client, none, "login")

    for response, error in ((first, "multiple_merchants"), (second, "no_merchants")):
        body = response.json()
        assert response.status_code == 200
        assert body["outcome"] == "rejected"
        assert body["shop"]["connection_state"] == "reauth_required"
        assert body["shop"]["connection_error"] == error
        assert body["shop"]["merchant_uid"] is None


async def test_other_cabinet_is_rejected(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т35."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="reauth_required",
        merchant_uid="M1",
        session=kaspi_session(),
        error="session_expired",
    )
    fake_client(deps).queue("login", logged_in("M2"))

    response = await _post(client, shop_id, "login")

    body = response.json()
    assert body["outcome"] == "rejected"
    assert body["shop"]["connection_error"] == "merchant_mismatch"
    assert body["shop"]["merchant_uid"] == "M1"


async def test_shop_already_connected_elsewhere(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т36."""
    client_a, tenant_a = await _setup(deps, raw_engine, "a@example.com")
    await _setup(deps, raw_engine, "b@example.com")
    tenant_b = await tenant_of(raw_engine, "b@example.com")
    shop_b = await seed_connect_shop(
        deps, raw_engine, tenant_b, state="connected", merchant_uid="M1", version=2
    )
    before_b = await read_shop(raw_engine, shop_b)
    login_shop = await seed_connect_shop(deps, raw_engine, tenant_a)
    code_shop = await seed_connect_shop(
        deps,
        raw_engine,
        tenant_a,
        state="awaiting_code",
        session=kaspi_session("awaiting_code", cookie="COOKIE-OLD-1"),
        version=1,
    )
    fake = fake_client(deps)
    fake.mutate_session = lambda session: touch_session(session, "COOKIE-NEW-AAA")
    fake.queue("login", logged_in("M1"))
    fake.queue("submit_code", logged_in("M1"))

    via_login = await _post(client_a, login_shop, "login")
    via_code = await _post(client_a, code_shop, "code", json={"code": EMAIL_CODE})

    for response in (via_login, via_code):
        body = response.json()
        assert response.status_code == 200
        assert body["outcome"] == "rejected"
        assert body["shop"]["connection_error"] == "shop_already_connected"
        assert body["shop"]["merchant_uid"] is None
        assert str(tenant_b) not in response.text
        assert str(shop_b) not in response.text
    blob = await stored_session(deps, tenant_a, code_shop)
    assert blob is not None
    assert "COOKIE-NEW-AAA" in blob
    assert await read_shop(raw_engine, shop_b) == before_b


async def test_login_rejected_and_temporary(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т37."""
    client, tenant = await _setup(deps, raw_engine)
    rejected = await seed_connect_shop(deps, raw_engine, tenant)
    flood = await seed_connect_shop(deps, raw_engine, tenant)
    fake_client(deps).queue(
        "login", LoginRejectedError("FAILED"), LoginRejectedError("MFA_SEND_FLOOD", 120)
    )

    first = await _post(client, rejected, "login")
    second = await _post(client, flood, "login")

    body = first.json()
    assert (body["outcome"], body["shop"]["connection_state"]) == ("rejected", "reauth_required")
    assert body["shop"]["connection_error"] == "FAILED"
    assert (await read_shop(raw_engine, rejected))["connection_version"] == 1
    body = second.json()
    assert (body["outcome"], body["retry_after_seconds"]) == ("temporary_error", 120)
    row = await read_shop(raw_engine, flood)
    assert (row["connection_state"], row["connection_version"]) == ("not_connected", 0)


async def test_login_temporary_failures(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т38."""
    client, tenant = await _setup(deps, raw_engine)
    cases: list[tuple[Exception, int | None]] = [
        (KaspiTransportError("login", "ConnectError"), None),
        (UnexpectedStatusError("login", 502), None),
        (ResponseFormatError("login"), None),
        (RateLimitedError("login", 30), 30),
        (LimiterBusyError(7.2), 8),
    ]
    for exc, retry in cases:
        shop_id = await seed_connect_shop(deps, raw_engine, tenant)
        fake_client(deps).queue("login", exc)
        response = await _post(client, shop_id, "login")
        body = response.json()
        assert response.status_code == 200, exc
        assert (body["outcome"], body["retry_after_seconds"]) == ("temporary_error", retry)
        row = await read_shop(raw_engine, shop_id)
        assert (row["connection_state"], row["connection_version"]) == ("not_connected", 0)
        assert row["session_enc"] is None

    shop_id = await seed_connect_shop(deps, raw_engine, tenant)
    fake_client(deps).queue("login", LimiterUnavailableError())
    unavailable = await _post(client, shop_id, "login")
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "service_unavailable"
    assert (await read_shop(raw_engine, shop_id))["connection_version"] == 0


async def test_code_accepted(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т39."""
    client, tenant = await _setup(deps, raw_engine)
    saved = kaspi_session("awaiting_code")
    shop_id = await seed_connect_shop(
        deps, raw_engine, tenant, state="awaiting_code", session=saved, version=1
    )
    fake_client(deps).queue("submit_code", logged_in("M1"))

    response = await _post(client, shop_id, "code", json={"code": EMAIL_CODE})

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["outcome"] == "connected"
    assert body["shop"]["connection_state"] == "connected"
    assert (await read_shop(raw_engine, shop_id))["connection_version"] == 2
    call = fake_client(deps).calls_of("submit_code")[0]
    assert call["code"] == EMAIL_CODE
    assert call["session"] == saved


async def test_wrong_code_keeps_state_and_saves_session(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т40."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    fake = fake_client(deps)
    fake.mutate_session = touch_session
    fake.queue("submit_code", LoginRejectedError("MFA_CODE_INVALID"))

    changed = await _post(client, shop_id, "code", json={"code": EMAIL_CODE})

    body = changed.json()
    assert (body["outcome"], body["shop"]["connection_state"]) == ("code_invalid", "awaiting_code")
    assert (await read_shop(raw_engine, shop_id))["connection_version"] == 1
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert "COOKIE-NEW-9c1d" in blob

    fake.mutate_session = None
    before = (await read_shop(raw_engine, shop_id))["session_enc"]
    fake.queue("submit_code", LoginRejectedError("MFA_CODE_INVALID"))
    unchanged = await _post(client, shop_id, "code", json={"code": EMAIL_CODE})
    assert unchanged.json()["outcome"] == "code_invalid"
    assert (await read_shop(raw_engine, shop_id))["session_enc"] == before


TEMPORARY_SUBMIT_FAILURES: list[Exception] = [
    KaspiTransportError("login", "ConnectError"),
    UnexpectedStatusError("login", 503),
    ResponseFormatError("login"),
    RateLimitedError("login", None),
    LimiterBusyError(3),
    LimiterUnavailableError(),
    LoginRejectedError("TIMEOUT_EXCEEDED"),
    SessionStateError("phase"),
]


@pytest.mark.parametrize("failure", TEMPORARY_SUBMIT_FAILURES, ids=lambda e: type(e).__name__)
async def test_temporary_failure_after_code_requires_restart(
    deps: ApiDeps, raw_engine: AsyncEngine, failure: Exception
) -> None:
    """Т41."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    fake = fake_client(deps)
    fake.mutate_session = touch_session
    fake.queue("submit_code", failure)

    response = await _post(client, shop_id, "code", json={"code": EMAIL_CODE})

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "restart_required"
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert "COOKIE-NEW-9c1d" in blob
    row = await read_shop(raw_engine, shop_id)
    assert (row["connection_state"], row["connection_version"]) == ("awaiting_code", 1)
    shop = (await client.get(f"/api/shops/{shop_id}")).json()
    assert shop["restart_required"] is True

    again = await _post(client, shop_id, "code", json={"code": EMAIL_CODE})
    assert again.status_code == 409
    assert again.json()["code"] == "restart_required"
    assert len(fake.calls_of("submit_code")) == 1

    fake.mutate_session = None
    fake.queue("login", code_required())
    restart = await _post(client, shop_id, "login/restart")
    assert restart.json()["outcome"] == "code_required"
    assert restart.json()["shop"]["restart_required"] is False
    assert (await read_shop(raw_engine, shop_id))["connection_version"] == 2


async def test_rejection_after_code_saves_session_first(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т42."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    fake = fake_client(deps)
    fake.mutate_session = touch_session
    fake.queue("submit_code", LoginRejectedError("FAILED"))

    response = await _post(client, shop_id, "code", json={"code": EMAIL_CODE})

    body = response.json()
    assert body["outcome"] == "rejected"
    assert body["shop"]["connection_state"] == "reauth_required"
    assert body["shop"]["connection_error"] == "FAILED"
    blob = await stored_session(deps, tenant, shop_id)
    assert blob is not None
    assert "COOKIE-NEW-9c1d" in blob


async def test_resend_code_timer(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т43."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(deps, raw_engine, tenant, aged=False)
    await sync_clock(deps, raw_engine)
    fake = fake_client(deps)
    fake.queue("login", code_required())
    await _post(client, shop_id, "login")

    early = await _post(client, shop_id, "code/resend")

    assert early.status_code == 429
    assert early.json()["code"] == "resend_too_early"
    assert 170 < early.json()["retry_after_seconds"] <= 180
    assert fake.calls_of("resend_code") == []

    await _drop_resend_key(deps, shop_id)
    changed_at = (await read_shop(raw_engine, shop_id))["connection_changed_at"]
    _clock(deps).now = changed_at + timedelta(seconds=181)
    fake.queue("resend_code", None)
    sent = await _post(client, shop_id, "code/resend")
    assert sent.json()["outcome"] == "code_sent"
    assert len(fake.calls_of("resend_code")) == 1
    assert 170 < sent.json()["shop"]["resend_available_in_seconds"] <= 180

    again = await _post(client, shop_id, "code/resend")
    assert again.status_code == 429


async def test_resend_rejections(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т44."""
    client, tenant = await _setup(deps, raw_engine)
    flood = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    failed = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    fake = fake_client(deps)
    fake.queue(
        "resend_code", LoginRejectedError("MFA_CODE_TOO_MANY_SEND"), LoginRejectedError("FAILED")
    )

    first = await _post(client, flood, "code/resend")
    second = await _post(client, failed, "code/resend")
    repeat = await _post(client, flood, "code/resend")

    assert first.json()["outcome"] == "temporary_error"
    assert first.json()["shop"]["connection_state"] == "awaiting_code"
    assert repeat.status_code == 429
    assert second.json()["outcome"] == "rejected"
    assert second.json()["shop"]["connection_state"] == "reauth_required"


async def test_restart_login_passes_previous_session(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т45."""
    client, tenant = await _setup(deps, raw_engine)
    saved = kaspi_session("awaiting_code")
    first = await seed_connect_shop(
        deps, raw_engine, tenant, state="awaiting_code", session=saved, version=1
    )
    second = await seed_connect_shop(
        deps, raw_engine, tenant, state="awaiting_code", session=saved, version=1
    )
    fake = fake_client(deps)
    fake.queue("login", code_required(), logged_in("M1"))

    again = await _post(client, first, "login/restart")
    done = await _post(client, second, "login/restart")

    assert [c["previous"] for c in fake.calls_of("login")] == [saved, saved]
    assert again.json()["outcome"] == "code_required"
    assert again.json()["shop"]["connection_state"] == "awaiting_code"
    assert (await read_shop(raw_engine, first))["connection_version"] == 2
    assert done.json()["outcome"] == "connected"


async def test_stale_version_gives_conflict(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т46."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(deps, raw_engine, tenant)
    fake = fake_client(deps)

    async def other_writer() -> None:
        async with deps.database.tenant(TenantId(tenant)) as scope:
            await scope.shops.set_credentials(
                shop_id, login=Secret("x@shop.kz"), password=Secret("pw")
            )
            await scope.commit()

    fake.before_return = other_writer
    fake.queue("login", logged_in("M1"))

    response = await _post(client, shop_id, "login")

    assert response.status_code == 409
    assert response.json()["code"] == "state_changed"
    row = await read_shop(raw_engine, shop_id)
    assert (row["connection_state"], row["merchant_uid"]) == ("not_connected", None)


async def test_login_lock(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т47."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(deps, raw_engine, tenant)
    fake = fake_client(deps)
    token = await deps.login_lock.acquire(shop_id, 60_000)
    assert token is not None

    busy = await _post(client, shop_id, "login")

    assert busy.status_code == 409
    assert busy.json()["code"] == "login_in_progress"
    assert fake.calls == []
    await deps.login_lock.release(shop_id, token)
    fake.queue("login", code_required())
    ok = await _post(client, shop_id, "login")
    assert ok.json()["outcome"] == "code_required"
    assert await deps.login_lock.acquire(shop_id, 1000) is not None


async def test_encryption_keys(
    deps: ApiDeps, raw_engine: AsyncEngine, api_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Т48."""
    client, tenant = await _setup(deps, raw_engine)
    other = _create_database(
        api_database_url, cipher=SecretCipher(KeyRing(2, {2: bytes(range(32))}))
    )
    try:
        async with other.tenant(TenantId(tenant)) as scope:
            shop = await scope.shops.add(login=Secret(KASPI_LOGIN), password=Secret(KASPI_PASSWORD))
            await scope.commit()
    finally:
        await other.dispose()
    caplog.set_level(logging.DEBUG)

    unknown = await _post(client, shop.id, "login")

    assert unknown.status_code == 503
    assert unknown.json()["code"] == "service_unavailable"
    row = await read_shop(raw_engine, shop.id)
    assert (row["connection_state"], row["connection_version"]) == ("not_connected", 0)
    errors = [
        r for r in caplog.records if r.levelno == logging.ERROR and "unknown_key" in r.getMessage()
    ]
    assert errors
    assert str(shop.id) in errors[0].getMessage()
    for secret in (KASPI_LOGIN, KASPI_PASSWORD):
        assert secret not in caplog.text

    broken = await seed_connect_shop(deps, raw_engine, tenant)
    await sql_exec(
        raw_engine,
        # Инверсия последнего байта: присвоение константы не меняет блоб, если байт уже равен ей.
        "UPDATE shop SET password_enc = set_byte(password_enc, length(password_enc) - 1, "
        "get_byte(password_enc, length(password_enc) - 1) # 255) WHERE id = :id",
        id=broken,
    )
    response = await _post(client, broken, "login")
    assert response.status_code == 409
    assert response.json()["code"] == "credentials_unreadable"
    assert (await read_shop(raw_engine, broken))["connection_version"] == 0


async def test_broken_session_blob(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т49."""
    client, tenant = await _setup(deps, raw_engine)
    reauth = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="reauth_required",
        session=kaspi_session(),
        error="session_expired",
    )
    awaiting = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    for shop_id in (reauth, awaiting):
        await sql_exec(
            raw_engine,
            "UPDATE shop SET session_enc = set_byte(session_enc, length(session_enc) - 1, "
            "get_byte(session_enc, length(session_enc) - 1) # 255) WHERE id = :id",
            id=shop_id,
        )
    fake = fake_client(deps)
    fake.queue("login", code_required())

    login = await _post(client, reauth, "login")
    code = await _post(client, awaiting, "code", json={"code": EMAIL_CODE})

    assert login.json()["outcome"] == "code_required"
    assert fake.calls_of("login")[0]["previous"] is None
    assert code.status_code == 200
    assert code.json()["outcome"] == "restart_required"
    assert fake.calls_of("submit_code") == []


async def test_api_never_writes_session_expired_on_login_failure(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т52."""
    client, tenant = await _setup(deps, raw_engine)
    fake = fake_client(deps)
    errors: set[str | None] = set()
    for outcome in (
        logged_in("M1", "M2"),
        logged_in(),
        LoginRejectedError("FAILED"),
    ):
        shop_id = await seed_connect_shop(deps, raw_engine, tenant)
        fake.queue("login", outcome)
        body = (await _post(client, shop_id, "login")).json()
        errors.add(body["shop"]["connection_error"])
    mismatch = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="reauth_required",
        merchant_uid="M7",
        session=kaspi_session(),
        error="session_expired",
    )
    fake.queue("login", logged_in("M8"))
    errors.add((await _post(client, mismatch, "login")).json()["shop"]["connection_error"])
    awaiting = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    fake.queue("resend_code", LoginRejectedError("FAILED"))
    errors.add((await _post(client, awaiting, "code/resend")).json()["shop"]["connection_error"])

    assert errors <= ALLOWED_ERRORS
    assert "session_expired" not in errors


async def test_resend_timer_from_database(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т72."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
        aged=False,
    )
    changed_at = (await read_shop(raw_engine, shop_id))["connection_changed_at"]
    clock = _clock(deps)
    clock.now = changed_at + timedelta(seconds=60)
    fake = fake_client(deps)

    shown = await client.get(f"/api/shops/{shop_id}")
    resend = await _post(client, shop_id, "code/resend")
    restart = await _post(client, shop_id, "login/restart")

    assert shown.json()["resend_available_in_seconds"] == 120
    for response in (resend, restart):
        assert response.status_code == 429
        assert response.json()["code"] == "resend_too_early"
        assert response.json()["retry_after_seconds"] == 120
    assert fake.calls == []
    clock.now = changed_at + timedelta(seconds=181)
    fake.queue("resend_code", None)
    assert (await _post(client, shop_id, "code/resend")).json()["outcome"] == "code_sent"


async def test_timer_released_when_letter_not_sent(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т73."""
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    fake = fake_client(deps)
    fake.queue("resend_code", LimiterBusyError(5))

    busy = await _post(client, shop_id, "code/resend")

    body = busy.json()
    assert (body["outcome"], body["retry_after_seconds"]) == ("temporary_error", 5)
    assert await _resend_key(deps, shop_id) is None
    fake.queue("resend_code", KaspiTransportError("resend", "ReadError"))
    failed = await _post(client, shop_id, "code/resend")
    assert failed.json()["outcome"] == "temporary_error"
    assert await _resend_key(deps, shop_id) is not None
    assert (await _post(client, shop_id, "code/resend")).status_code == 429
    assert len(fake.calls_of("resend_code")) == 2


async def test_hanging_client_call(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т74."""
    monkeypatch.setattr("kaspisa.api.deps.LOGIN_CALL_TIMEOUT_SECONDS", 0.2)
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(deps, raw_engine, tenant)
    awaiting = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    fake = fake_client(deps)
    fake.hang = {"login", "submit_code"}

    login = await _post(client, shop_id, "login")
    code = await _post(client, awaiting, "code", json={"code": EMAIL_CODE})

    assert login.json()["outcome"] == "temporary_error"
    row = await read_shop(raw_engine, shop_id)
    assert (row["connection_state"], row["connection_version"]) == ("not_connected", 0)
    assert await deps.login_lock.acquire(shop_id, 1000) is not None
    assert code.json()["outcome"] == "restart_required"


@pytest.mark.parametrize("operation", ["login", "code", "resend"])
async def test_shop_deleted_during_login(
    deps: ApiDeps, raw_engine: AsyncEngine, operation: str
) -> None:
    """Т75."""
    client, tenant = await _setup(deps, raw_engine)
    fake = fake_client(deps)
    if operation == "login":
        shop_id = await seed_connect_shop(deps, raw_engine, tenant)
        fake.queue("login", code_required())
        request = _post(client, shop_id, "login")
    else:
        shop_id = await seed_connect_shop(
            deps,
            raw_engine,
            tenant,
            state="awaiting_code",
            session=kaspi_session("awaiting_code"),
            version=1,
        )
        if operation == "code":
            fake.queue("submit_code", logged_in("M1"))
            request = _post(client, shop_id, "code", json={"code": EMAIL_CODE})
        else:
            fake.queue("resend_code", None)
            request = _post(client, shop_id, "code/resend")

    async def delete_shop() -> None:
        async with deps.database.tenant(TenantId(tenant)) as scope:
            await scope.shops.delete(shop_id)
            await scope.commit()

    fake.before_return = delete_shop

    response = await request

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_code_validation_and_no_echo(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(
        deps,
        raw_engine,
        tenant,
        state="awaiting_code",
        session=kaspi_session("awaiting_code"),
        version=1,
    )
    for bad in ("12", "12345678901234", "12ab34", ""):
        response = await _post(client, shop_id, "code", json={"code": bad})
        assert response.status_code == 422
        assert "body.code" in response.json()["fields"]
        assert bad not in response.text or bad == ""
    assert fake_client(deps).calls == []


async def test_connect_responses_have_no_secrets(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant = await _setup(deps, raw_engine)
    shop_id = await seed_connect_shop(deps, raw_engine, tenant)
    fake = fake_client(deps)
    fake.queue("login", code_required())
    fake.mutate_session = lambda session: touch_session(session, "COOKIE-NEW-AAA")
    fake.queue("submit_code", logged_in("M1"))

    first = await _post(client, shop_id, "login")
    second = await _post(client, shop_id, "code", json={"code": EMAIL_CODE})

    for response in (first, second):
        for secret in (KASPI_LOGIN, KASPI_PASSWORD, KASPI_COOKIE, EMAIL_CODE, "COOKIE-NEW-AAA"):
            assert secret not in response.text
            assert secret not in str(response.headers)


async def test_connect_routes_hide_foreign_shops(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Каждая точка C1-C4: чужой магазин и случайный UUID - 404, клиент не вызван."""
    app = create_app(deps)
    paths = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/shops/{shop_id}/")
        and path.split("/{shop_id}/")[1] in CONNECT_TAILS
        for method in operations
    }
    assert {(r.method, r.path) for r in CONNECT_ROUTES} == paths
    await _setup(deps, raw_engine, "a@example.com")
    client_b, _ = await _setup(deps, raw_engine, "b@example.com")
    tenant_a = await tenant_of(raw_engine, "a@example.com")
    shop_a = await seed_shop(deps, raw_engine, tenant_a, state="awaiting_code", version=1)
    before = await read_shop(raw_engine, shop_a)

    for spec in CONNECT_ROUTES:
        for shop_id in (shop_a, uuid.uuid4()):
            response = await client_b.request(spec.method, spec.url(shop_id), json=spec.body)
            assert response.status_code == 404, spec
            assert response.json()["code"] == "not_found"

    assert fake_client(deps).calls == []
    assert await read_shop(raw_engine, shop_a) == before
    assert isinstance(deps.client, FakeConnectClient)
