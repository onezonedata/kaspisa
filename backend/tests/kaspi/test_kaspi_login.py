"""Вход: код из письма, ошибки, цепочки перенаправлений, повторы, ключи ограничителя
(Т45-Т56, Т64, Т72, Т76, Т77).

Вход на живом Kaspi не проверен. Сняты только два запроса `POST /api/p/login`
(`login-flow.json`) и `GET /s/m` (`session-merchants.json`); перенаправления OAuth не сняты,
поэтому цепочки здесь - синтетика, не снято (`synthetic_oauth_start`, `synthetic_oauth_return`).
"""

# ruff: noqa: F401, F811  (фикстуры импортируются из kaspi_fixtures; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import itertools
import json
import re
from typing import Any

import httpx
import pytest
import respx
from kaspi_fixtures import (
    client,
    fake_time,
    limiter,
    mock_kaspi,
)
from kaspi_testing import (
    AUTH_START_URL,
    CODE_MARKER,
    DEVICE_COOKIE_BLOB,
    DEVICE_MARKER,
    IDMC_MARKER,
    LOGIN_API_URL,
    LOGIN_MARKER,
    LOGIN_PAGE_URL,
    MC_CODE_URL,
    MERCHANT_UID,
    MERCHANTS_URL,
    MFA_SEND_URL,
    NEW_SESSION_MARKER,
    OAUTH_MARKER,
    PASSWORD_MARKER,
    REDIRECT_URL,
    SESSION_COOKIE_VALUE,
    START_AUTHORIZE_URL,
    FakeTime,
    RecordingLimiter,
    authenticated_session,
    captured_login_reply,
    captured_login_step,
    captured_merchants,
    captured_redirect_url,
    load_fixture,
    redirect_response,
    session_blob,
    synthetic_login_error,
    synthetic_login_redirect,
    synthetic_login_staff_mode,
    synthetic_mfa_send_ok,
    synthetic_oauth_return,
    synthetic_oauth_start,
)

from kaspisa.kaspi import (
    CodeRequired,
    KaspiClient,
    KaspiCredentials,
    KaspiSession,
    KaspiTransportError,
    LoggedIn,
    LoginRejectedError,
    RateLimitedError,
    ResponseFormatError,
    SessionPhase,
    SessionStateError,
    UnexpectedStatusError,
)
from kaspisa.kaspi.connection import next_connection_state
from kaspisa.kaspi.transport import limiter_key_for
from kaspisa.security import Secret

EMAIL = "mail-marker@example.invalid"
XSRF_COOKIE_VALUE = "xsrf-cookie-marker-b6c2"


@pytest.fixture
def credentials() -> KaspiCredentials:
    return KaspiCredentials(login=Secret(LOGIN_MARKER), password=Secret(PASSWORD_MARKER))


def _phase(session: KaspiSession) -> SessionPhase:
    """Фаза без сужения типа mypy: `submit_code` меняет её на месте."""
    return session.phase


def _body(route: respx.Route, index: int = -1) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls[index].request.content)
    return body


def _cookie_names(session: KaspiSession) -> set[str]:
    return {c["name"] for c in json.loads(session.dump().reveal())["cookies"]}


def _hosts(mock_kaspi: respx.MockRouter) -> list[tuple[str, str]]:
    return [(c.request.method, c.request.url.host) for c in mock_kaspi.calls]


def _mock_login_ok(mock_kaspi: respx.MockRouter) -> dict[str, Any]:
    """Вход без кода: начало цепочки, POST с `redirectUrl`, возврат, `GET /s/m`."""
    return {
        "start": synthetic_oauth_start(mock_kaspi),
        "post": mock_kaspi.post(LOGIN_API_URL).mock(
            return_value=captured_login_reply("код из письма")
        ),
        "back": synthetic_oauth_return(mock_kaspi),
        "merchants": mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants()),
    }


async def test_login_without_code(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т45: цепочка, POST с `_r_d: false`, возврат, `GET /s/m` -> `LoggedIn` из снятых магазинов."""
    routes = _mock_login_ok(mock_kaspi)
    result = await client.login(credentials)
    assert isinstance(result, LoggedIn)
    assert result.session.phase is SessionPhase.AUTHENTICATED
    assert [m.uid for m in result.merchants] == [MERCHANT_UID]
    assert "MANAGE_OFFERS" in result.merchants[0].roles
    assert _body(routes["post"]) == {"_u": LOGIN_MARKER, "_p": PASSWORD_MARKER, "_r_d": False}
    assert _hosts(mock_kaspi) == [
        ("GET", "mc.shop.kaspi.kz"),
        ("GET", "idmc.shop.kaspi.kz"),
        ("GET", "idmc.shop.kaspi.kz"),
        ("POST", "idmc.shop.kaspi.kz"),
        ("GET", "idmc.shop.kaspi.kz"),
        ("GET", "mc.shop.kaspi.kz"),
        ("GET", "kaspi.kz"),
        ("GET", "mc.shop.kaspi.kz"),
    ]
    assert str(mock_kaspi.calls[0].request.url) == AUTH_START_URL
    assert {"mc-session", "mc-sid", "device-id"} <= _cookie_names(result.session)
    assert (
        f"mc-session={NEW_SESSION_MARKER}"
        in routes["merchants"].calls.last.request.headers["cookie"]
    )
    # Пароль уходит только в тело POST на `LOGIN_API_URL` и больше никуда.
    for call in mock_kaspi.calls:
        if call.request.method == "POST":
            continue
        request = call.request
        assert PASSWORD_MARKER not in str(request.url) + request.content.decode()
        assert PASSWORD_MARKER not in str(request.headers)
    restored = KaspiSession.load(result.session.dump())
    assert restored.phase is SessionPhase.AUTHENTICATED


async def test_login_with_code_roundtrip(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т46: снятые тела и заголовки обоих POST; `CodeRequired`; `dump`/`load`; `LoggedIn`."""
    synthetic_oauth_start(mock_kaspi)
    post = mock_kaspi.post(LOGIN_API_URL).mock(
        side_effect=[
            captured_login_reply("пароль", email=EMAIL),
            captured_login_reply("код из письма"),
        ]
    )
    back = synthetic_oauth_return(mock_kaspi)
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants())

    result = await client.login(credentials)
    assert isinstance(result, CodeRequired)
    assert result.session.phase is SessionPhase.AWAITING_CODE
    saved = KaspiSession.load(result.session.dump())
    assert saved.phase is SessionPhase.AWAITING_CODE

    logged_in = await client.submit_code(saved, CODE_MARKER)
    assert isinstance(logged_in, LoggedIn)
    assert logged_in.session is saved
    assert _phase(saved) is SessionPhase.AUTHENTICATED

    password_step = captured_login_step("пароль")
    code_step = captured_login_step("код из письма")
    first, second = _body(post, 0), _body(post, 1)
    assert set(first) == set(password_step["requestBody"])
    assert set(second) == set(code_step["requestBody"])
    assert (first["_r_d"], second["_r_d"]) == (False, True)
    assert second == {"_m_c": CODE_MARKER, "_r_d": True, "_u": EMAIL}
    for call, step in zip(post.calls, (password_step, code_step), strict=True):
        headers = call.request.headers
        for line in step["requestHeaders"]:
            name, value = line.split(": ", 1)
            assert headers[name] == value
        assert headers["content-type"] == "application/json"
        assert "x-xsrf-token" not in headers
        assert headers["origin"] == "https://idmc.shop.kaspi.kz"
        assert headers["referer"] == "https://idmc.shop.kaspi.kz/login"
    # Первый переход после кода - `redirectUrl` из снятого ответа.
    assert str(back["authorize"].calls[0].request.url) == str(httpx.URL(captured_redirect_url()))
    # Адрес e-mail нужен только до конца входа и в готовом блоке не хранится.
    assert json.loads(saved.dump().reveal())["pending_login"] is None


async def test_wrong_code_keeps_session_awaiting_code(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т47: `MFA_CODE_INVALID` - `LoginRejectedError`; сессия в `AWAITING_CODE`, повтор проходит."""
    session = KaspiSession.load(session_blob(phase="awaiting_code", pending_login=EMAIL))
    mock_kaspi.post(LOGIN_API_URL).mock(
        side_effect=[
            synthetic_login_error("MFA_CODE_INVALID"),
            captured_login_reply("код из письма"),
        ]
    )
    synthetic_oauth_return(mock_kaspi)
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants())
    with pytest.raises(LoginRejectedError) as info:
        await client.submit_code(session, "wrong-code-marker")
    assert info.value.code == "MFA_CODE_INVALID"
    assert session.phase is SessionPhase.AWAITING_CODE
    await client.submit_code(session, CODE_MARKER)
    assert _phase(session) is SessionPhase.AUTHENTICATED


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize(
    ("code", "break_time", "expected_wait"),
    [
        ("CREDENTIALS_INVALID", None, None),
        ("MFA_CODE_ATTEMPT_LIMIT", None, None),
        ("MFA_SEND_FLOOD", 120, 120),
        ("FAILED", None, None),
        ("MFA_SEND_FLOOD", "120", None),  # не целое: ожидание не берём
        ("MFA_SEND_FLOOD", True, None),  # bool - не число секунд
    ],
)
async def test_login_errors_carry_code_and_wait(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    status: int,
    code: str,
    break_time: object,
    expected_wait: int | None,
) -> None:
    """Т48: код ошибки и `breakTimeSeconds` при статусах 200 и 400; сессия не возвращается."""
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(
        return_value=synthetic_login_error(code, status=status, break_time=break_time)
    )
    with pytest.raises(LoginRejectedError) as info:
        await client.login(credentials)
    assert info.value.code == code
    assert info.value.retry_after_seconds == expected_wait
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


async def test_login_error_code_with_odd_characters_is_format_error(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Код ошибки не из заглавных латинских букв и цифр в текст исключения не попадает."""
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error(f"bad {LOGIN_MARKER}"))
    with pytest.raises(ResponseFormatError) as info:
        await client.login(credentials)
    assert LOGIN_MARKER not in str(info.value)


async def test_staff_mode_empty_object_and_email_on_second_step(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т49: `su` - `STAFF_MODE`; пустой объект - формат; `{"email"}` на `submit_code` - формат."""
    synthetic_oauth_start(mock_kaspi)
    post = mock_kaspi.post(LOGIN_API_URL).mock(
        side_effect=[
            synthetic_login_staff_mode(),
            httpx.Response(200, json={}),
            captured_login_reply("пароль", email=EMAIL),
        ]
    )
    with pytest.raises(LoginRejectedError) as staff:
        await client.login(credentials)
    assert staff.value.code == "STAFF_MODE"
    with pytest.raises(ResponseFormatError):
        await client.login(credentials)
    session = KaspiSession.load(session_blob(phase="awaiting_code", pending_login=EMAIL))
    with pytest.raises(ResponseFormatError):
        await client.submit_code(session, CODE_MARKER)
    assert post.call_count == 3
    assert session.phase is SessionPhase.AWAITING_CODE


async def test_non_json_body_and_bad_status(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Разбор ответа входа: не JSON при 200 - формат; при 500 - статус; 429 - лимит."""
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(
        side_effect=[
            httpx.Response(200, text="<html>not json</html>"),
            httpx.Response(500, text="oops"),
            httpx.Response(500, json={"redirectUrl": REDIRECT_URL}),
            httpx.Response(429, headers={"Retry-After": "30"}, json={}),
        ]
    )
    with pytest.raises(ResponseFormatError):
        await client.login(credentials)
    with pytest.raises(UnexpectedStatusError) as unexpected:
        await client.login(credentials)
    assert unexpected.value.status_code == 500
    with pytest.raises(UnexpectedStatusError):  # не 2xx важнее `redirectUrl`
        await client.login(credentials)
    with pytest.raises(RateLimitedError) as limited:
        await client.login(credentials)
    assert limited.value.retry_after == 30


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.example/mc/",
        "http://kaspi.kz/mc/",
        "https://kaspi.kz.evil.example/mc/",
        "https://evilkaspi.kz/mc/",
        "https://kaspi.kz@evil.example/mc/",
        "https://sso.kaspi.kz/mc/",
        "ftp://kaspi.kz/mc/",
        "//evil.example/mc/",
        "/mc/",
        "https://[broken/",
    ],
)
async def test_redirect_url_outside_kaspi_is_refused(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    bad_url: str,
) -> None:
    """Т51: `redirectUrl` вне трёх хостов или не по https - `ResponseFormatError`, запроса нет."""
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_redirect(bad_url))
    with pytest.raises(ResponseFormatError):
        await client.login(credentials)
    assert [method for method, _ in _hosts(mock_kaspi)] == ["GET", "GET", "GET", "POST"]


async def test_redirect_chain_ten_requests_pass_with_relative_location(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т51: десять запросов в цепочке проходят (относительный `Location` тоже), одиннадцатый - нет.

    Синтетика, не снято: цепочка `https://kaspi.kz/h1` ... `/h10`, десятый ответ - 200.
    """
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(
        return_value=synthetic_login_redirect("https://kaspi.kz/h1")
    )
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants())

    def hop(request: httpx.Request) -> httpx.Response:
        number = int(request.url.path[2:])
        if number < 10:
            return redirect_response(f"/h{number + 1}")
        return httpx.Response(200)

    chain = mock_kaspi.get(url__regex=r"https://kaspi\.kz/h\d+").mock(side_effect=hop)
    result = await client.login(credentials)
    assert isinstance(result, LoggedIn)
    assert chain.call_count == 10


async def test_redirect_chain_too_long(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т51: десять ответов 302 подряд - `ResponseFormatError`, одиннадцатого запроса нет."""
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_redirect(REDIRECT_URL))
    loop = mock_kaspi.get(REDIRECT_URL).mock(return_value=redirect_response(REDIRECT_URL))
    with pytest.raises(ResponseFormatError):
        await client.login(credentials)
    assert loop.call_count == 10


@pytest.mark.parametrize(
    "location",
    ["https://evil.example/x", "http://kaspi.kz/x", "https://sso.kaspi.kz/x", None],
)
async def test_bad_or_missing_location_is_refused(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    location: str | None,
) -> None:
    """Т51: `Location` на чужой хост, по `http`, на поддомен вне списка или нет - ошибка формата."""
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_redirect(REDIRECT_URL))
    mock_kaspi.get(REDIRECT_URL).mock(
        return_value=redirect_response(location) if location else httpx.Response(302)
    )
    with pytest.raises(ResponseFormatError):
        await client.login(credentials)
    assert [method for method, _ in _hosts(mock_kaspi)] == ["GET", "GET", "GET", "POST", "GET"]


@pytest.mark.parametrize(
    ("merchants_reply", "expected"),
    [
        (httpx.Response(401), ResponseFormatError),
        (httpx.Response(200, json={}), ResponseFormatError),
        (httpx.Response(403), UnexpectedStatusError),
    ],
    ids=["401", "no-merchants", "403"],
)
async def test_login_without_working_session(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    merchants_reply: httpx.Response,
    expected: type[Exception],
) -> None:
    """Т52: `GET /s/m`: 401 и ответ без `merchants` - формат; 403 - `UnexpectedStatusError(403)`."""
    _mock_login_ok(mock_kaspi)
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=merchants_reply)
    with pytest.raises(expected) as info:
        await client.login(credentials)
    if expected is UnexpectedStatusError:
        assert info.value.status_code == 403  # type: ignore[attr-defined]
    assert next_connection_state("not_connected", "login", info.value) is None


async def test_failed_merchants_check_keeps_session_awaiting_code(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т52: ошибка после возврата в `submit_code` оставляет сессию в `AWAITING_CODE`."""
    session = KaspiSession.load(session_blob(phase="awaiting_code", pending_login=EMAIL))
    mock_kaspi.post(LOGIN_API_URL).mock(return_value=captured_login_reply("код из письма"))
    synthetic_oauth_return(mock_kaspi)
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(ResponseFormatError):
        await client.submit_code(session, CODE_MARKER)
    assert session.phase is SessionPhase.AWAITING_CODE
    assert json.loads(session.dump().reveal())["pending_login"] == EMAIL


async def test_login_post_is_not_retried(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т53: POST входа не повторяется ни при сбое сети, ни при 503."""
    synthetic_oauth_start(mock_kaspi)
    post = mock_kaspi.post(LOGIN_API_URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(503)]
    )
    with pytest.raises(KaspiTransportError):
        await client.login(credentials)
    assert post.call_count == 1
    with pytest.raises(UnexpectedStatusError):
        await client.login(credentials)
    assert post.call_count == 2


async def test_submit_code_and_resend_are_not_retried(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т53: `submit_code` и `resend_code` при сетевом сбое - один запрос и `KaspiTransportError`."""
    session = KaspiSession.load(session_blob(phase="awaiting_code", pending_login=EMAIL))
    post = mock_kaspi.post(LOGIN_API_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    send = mock_kaspi.post(MFA_SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(KaspiTransportError):
        await client.submit_code(session, CODE_MARKER)
    with pytest.raises(KaspiTransportError):
        await client.resend_code(session)
    assert post.call_count == 1
    assert send.call_count == 1
    assert session.phase is SessionPhase.AWAITING_CODE


async def test_chain_and_merchants_are_retried(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Таблица повторов: GET цепочки и `/s/m` повторяются, POST - нет."""
    routes = _mock_login_ok(mock_kaspi)
    start = routes["start"]["start"]
    start.mock(
        side_effect=[
            httpx.ConnectError("x"),
            redirect_response(START_AUTHORIZE_URL),
        ]
    )
    back = routes["back"]["authorize"]
    back.mock(side_effect=[httpx.Response(503), redirect_response(MC_CODE_URL)])
    merchants = routes["merchants"]
    merchants.mock(side_effect=[httpx.ConnectError("x"), captured_merchants()])
    assert isinstance(await client.login(credentials), LoggedIn)
    assert (start.call_count, routes["post"].call_count) == (2, 1)
    assert (back.call_count, merchants.call_count) == (2, 2)


async def test_resend_code(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т54: POST `/api/p/mfa/send` с cookie сессии без `X-XSRF-TOKEN`; `MFA_SEND_FLOOD` - отказ."""
    session = KaspiSession.load(
        session_blob(
            phase="awaiting_code",
            pending_login=EMAIL,
            cookies=[
                {
                    "name": "XSRF-TOKEN",
                    "value": XSRF_COOKIE_VALUE,
                    "domain": "idmc.shop.kaspi.kz",
                    "path": "/",
                    "secure": True,
                    "expires": None,
                    "host_only": True,
                }
            ],
        )
    )
    route = mock_kaspi.post(MFA_SEND_URL).mock(
        side_effect=[
            synthetic_mfa_send_ok(),
            synthetic_login_error("MFA_SEND_FLOOD", break_time=180),
            httpx.Response(500),
        ]
    )
    await client.resend_code(session)
    request = route.calls.last.request
    assert request.content == b""
    assert f"XSRF-TOKEN={XSRF_COOKIE_VALUE}" in request.headers["cookie"]
    assert "x-xsrf-token" not in request.headers
    with pytest.raises(LoginRejectedError) as info:
        await client.resend_code(session)
    assert (info.value.code, info.value.retry_after_seconds) == ("MFA_SEND_FLOOD", 180)
    with pytest.raises(UnexpectedStatusError):
        await client.resend_code(session)
    assert session.phase is SessionPhase.AWAITING_CODE


async def test_previous_session_cookies_are_sent_on_relogin(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т55: `login(previous=...)` отправляет cookie устройства; `mc-session` - только на `mc`."""
    previous = authenticated_session(
        cookies=[
            DEVICE_COOKIE_BLOB,
            {
                "name": "mc-session",
                "value": SESSION_COOKIE_VALUE,
                "domain": "mc.shop.kaspi.kz",
                "path": "/",
                "secure": True,
                "expires": None,
                "host_only": True,
            },
        ]
    )
    before = previous.dump().reveal()
    start = synthetic_oauth_start(mock_kaspi)
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    with pytest.raises(LoginRejectedError):
        await client.login(credentials, previous=previous)
    cookie_header = post.calls.last.request.headers["cookie"]
    assert f"device-id={DEVICE_MARKER}" in cookie_header
    assert "mc-session" not in cookie_header
    start_cookie = start["start"].calls.last.request.headers["cookie"]
    assert f"mc-session={SESSION_COOKIE_VALUE}" in start_cookie
    assert previous.dump().reveal() == before
    assert previous.phase is SessionPhase.AUTHENTICATED


async def test_relogin_result_keeps_device_cookie(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т55: после повторного входа cookie устройства из `previous` остаётся в новой сессии."""
    previous = authenticated_session(cookies=[DEVICE_COOKIE_BLOB])
    routes = _mock_login_ok(mock_kaspi)
    routes["back"]["final"].mock(return_value=httpx.Response(200))
    result = await client.login(credentials, previous=previous)
    assert isinstance(result, LoggedIn)
    assert result.session is not previous
    assert "device-id" in _cookie_names(result.session)


@pytest.mark.parametrize("method", ["submit_code", "resend_code"])
async def test_code_steps_refuse_wrong_phase(
    client: KaspiClient, mock_kaspi: respx.MockRouter, method: str
) -> None:
    """Т54, Т56: `submit_code` и `resend_code` не в фазе `AWAITING_CODE` - ошибка без запросов."""
    route = mock_kaspi.route().mock(return_value=httpx.Response(200))
    for session in (KaspiSession(), authenticated_session()):
        with pytest.raises(SessionStateError):
            if method == "submit_code":
                await client.submit_code(session, CODE_MARKER)
            else:
                await client.resend_code(session)
    assert route.call_count == 0


async def test_submit_code_without_pending_login_refused(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т56: блок `awaiting_code` без e-mail - `SessionStateError`, запросов нет."""
    route = mock_kaspi.route().mock(return_value=httpx.Response(200))
    session = KaspiSession.load(session_blob(phase="awaiting_code", pending_login=None))
    with pytest.raises(SessionStateError):
        await client.submit_code(session, CODE_MARKER)
    assert route.call_count == 0


async def test_cabinet_calls_refuse_new_session(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т56: запрос кабинета на сессии фазы `NEW` - `SessionStateError` без запросов."""
    route = mock_kaspi.route().mock(return_value=httpx.Response(200))
    with pytest.raises(SessionStateError):
        await client.list_products(KaspiSession(), MERCHANT_UID)
    assert route.call_count == 0


async def test_limiter_keys_follow_hosts_during_concurrent_login(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    limiter: RecordingLimiter,
) -> None:
    """Т64: вход (8 запросов) и два `get_offers` одновременно - шаг по каждому ключу от 1 с."""
    _mock_login_ok(mock_kaspi)
    mock_kaspi.post(url__startswith="https://kaspi.kz/yml/offer-view/offers/").mock(
        return_value=httpx.Response(200, json=load_fixture("offers.json"))
    )
    results = await asyncio.gather(
        client.login(credentials),
        client.get_offers("900000005", "710000000", "90100003"),
        client.get_offers("900000005", "710000000", "90100003"),
    )
    assert isinstance(results[0], LoggedIn)

    def moments(key: str) -> list[float]:
        return sorted(at for k, at in limiter.passed if k == key)

    kaspi = moments("kaspi.kz")
    idmc = moments("idmc.shop.kaspi.kz")
    unknown = moments("mc:unknown")
    assert len(kaspi) == 3  # два запроса продавцов и возврат на `kaspi.kz/mc/`
    assert len(idmc) == 4  # `authorize`, страница входа, POST, `redirectUrl`
    assert len(unknown) == 3  # начало цепочки, возврат с кодом, `GET /s/m`
    for series in (kaspi, idmc, unknown):
        assert all(b - a >= 1.0 for a, b in itertools.pairwise(series))


@pytest.mark.parametrize(
    ("host", "merchant_uid", "key"),
    [
        ("mc.shop.kaspi.kz", "90100003", "mc:90100003"),
        ("mc.shop.kaspi.kz", None, "mc:unknown"),
        ("kaspi.kz", None, "kaspi.kz"),
        ("KASPI.KZ", None, "kaspi.kz"),
        ("idmc.shop.kaspi.kz", None, "idmc.shop.kaspi.kz"),
    ],
)
def test_limiter_key_for(host: str, merchant_uid: str | None, key: str) -> None:
    """Т64: ключ ограничителя выводится из хоста."""
    assert limiter_key_for(host, merchant_uid) == key


def test_results_refuse_sessions_in_wrong_phase() -> None:
    """Пункт 7.5.2: `LoggedIn` и `CodeRequired` не собираются из сессий не той фазы."""
    awaiting = KaspiSession.load(session_blob(phase="awaiting_code", pending_login=EMAIL))
    with pytest.raises(SessionStateError):
        LoggedIn(session=awaiting, merchants=[])
    with pytest.raises(SessionStateError):
        LoggedIn(session=KaspiSession(), merchants=[])
    with pytest.raises(SessionStateError):
        CodeRequired(session=authenticated_session())
    assert LoggedIn(session=authenticated_session(), merchants=[]).merchants == []
    assert CodeRequired(session=awaiting).session is awaiting


_BAD_URLS = [
    "https://xn--kaspi-.kaspi.kz/",
    "https://a\tb.kaspi.kz/",
    "https://a_b.kaspi.kz/",
    "https://a%2fb.kaspi.kz/",
]


@pytest.mark.parametrize("bad_url", _BAD_URLS)
async def test_unbuildable_redirect_url_rejected_before_request(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    bad_url: str,
) -> None:
    """Т72: адрес перехода, который `httpx` не собирает, - `ResponseFormatError` до запроса."""
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_redirect(bad_url))
    with pytest.raises(ResponseFormatError) as info:
        await client.login(credentials)
    assert info.value.operation == "login"
    assert info.value.fields == ("redirectUrl",)
    assert [method for method, _ in _hosts(mock_kaspi)] == ["GET", "GET", "GET", "POST"]
    assert next_connection_state("not_connected", "login", info.value) is None


async def test_unbuildable_location_is_transport_error(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т72: `httpx` сам собирает адрес из `Location` и падает - `KaspiTransportError`, без цепочки.

    Отступление от проекта (там `ResponseFormatError`): сбой происходит внутри `httpx`
    до того, как клиент увидит ответ. Состояние подключения не меняется в обоих случаях.
    """
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_redirect(REDIRECT_URL))
    mock_kaspi.get(REDIRECT_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "https://xn--kaspi-.kaspi.kz/"})
    )
    with pytest.raises(KaspiTransportError) as info:
        await client.login(credentials)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert info.value.kind == "IDNAError"
    assert not hasattr(info.value, "request")
    assert vars(info.value).keys() <= {"operation", "kind"}
    for text in (str(info.value), repr(info.value)):
        assert credentials.password.reveal() not in text
        assert credentials.login.reveal() not in text
    assert [method for method, _ in _hosts(mock_kaspi)] == ["GET", "GET", "GET", "POST", "GET"]
    assert next_connection_state("not_connected", "login", info.value) is None


async def test_start_of_login_reaches_login_page_and_posts_once(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т76 (а): цепочка от `AUTH_START_URL` кончается 200 на `idmc` - POST входа один раз."""
    synthetic_oauth_start(mock_kaspi)
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    with pytest.raises(LoginRejectedError):
        await client.login(credentials)
    assert post.call_count == 1


def _previous_session_chain(mock_kaspi: respx.MockRouter) -> None:
    """Синтетика, не снято: прежние cookie приняты, цепочка кончается на `kaspi.kz/mc/`."""
    mock_kaspi.get(AUTH_START_URL).mock(return_value=redirect_response(REDIRECT_URL))
    mock_kaspi.get(REDIRECT_URL).mock(return_value=httpx.Response(200))


async def test_previous_session_accepted_skips_password(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т76 (б): цепочка не дошла до страницы входа - пароль не отправляется, `LoggedIn`."""
    _previous_session_chain(mock_kaspi)
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants())
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    result = await client.login(credentials, previous=authenticated_session())
    assert isinstance(result, LoggedIn)
    assert [m.uid for m in result.merchants] == [MERCHANT_UID]
    assert post.call_count == 0
    for call in mock_kaspi.calls:
        assert PASSWORD_MARKER not in str(call.request.url) + call.request.content.decode()


async def test_previous_session_rejected_by_merchants_check(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т76 (в): цепочка не дошла до страницы входа, но `GET /s/m` - 401: ошибка, POST нет."""
    _previous_session_chain(mock_kaspi)
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=httpx.Response(401))
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    with pytest.raises(ResponseFormatError) as info:
        await client.login(credentials, previous=authenticated_session())
    assert post.call_count == 0
    assert next_connection_state("not_connected", "login", info.value) is None


async def test_start_chain_ending_in_server_error(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т76 (г): цепочка кончается 500 на `idmc` - `UnexpectedStatusError(500)`, POST нет."""
    mock_kaspi.get(AUTH_START_URL).mock(return_value=redirect_response(LOGIN_PAGE_URL))
    mock_kaspi.get(LOGIN_PAGE_URL).mock(return_value=httpx.Response(500))
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    with pytest.raises(UnexpectedStatusError) as info:
        await client.login(credentials)
    assert info.value.status_code == 500
    assert post.call_count == 0
    assert next_connection_state("not_connected", "login", info.value) is None


@pytest.mark.parametrize("target", ["https://sso.kaspi.kz/x", "https://evil.example/x"])
async def test_start_chain_to_foreign_host_is_refused(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    target: str,
) -> None:
    """Т76 (д): `Location` на поддомен вне списка или чужой хост - ошибка, запроса туда нет."""
    mock_kaspi.get(AUTH_START_URL).mock(
        return_value=redirect_response(target, f"oauth-marker={OAUTH_MARKER}; Path=/; Secure")
    )
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    with pytest.raises(ResponseFormatError) as info:
        await client.login(credentials)
    assert post.call_count == 0
    assert [call.request.url.host for call in mock_kaspi.calls] == ["mc.shop.kaspi.kz"]
    assert next_connection_state("not_connected", "login", info.value) is None


async def test_start_chain_redirect_without_location(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т76 (е): ответ 302 без `Location` - `ResponseFormatError`, POST нет."""
    mock_kaspi.get(AUTH_START_URL).mock(return_value=httpx.Response(302))
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    with pytest.raises(ResponseFormatError) as info:
        await client.login(credentials)
    assert post.call_count == 0
    assert next_connection_state("not_connected", "login", info.value) is None


async def test_chain_cookies_survive_waiting_for_code(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Т77: cookie цепочки переживают ожидание кода (`dump`/`load`) и уходят только по месту."""
    synthetic_oauth_start(mock_kaspi)
    post = mock_kaspi.post(LOGIN_API_URL).mock(
        side_effect=[
            captured_login_reply("пароль", email=EMAIL),
            captured_login_reply("код из письма"),
        ]
    )
    back = synthetic_oauth_return(mock_kaspi)
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants())

    first = await client.login(credentials)
    assert isinstance(first, CodeRequired)
    restored = KaspiSession.load(first.session.dump())
    done = await client.submit_code(restored, CODE_MARKER)
    assert isinstance(done, LoggedIn)

    code_cookie = post.calls[1].request.headers["cookie"]
    assert f"idmc-marker={IDMC_MARKER}" in code_cookie
    assert "oauth-marker" not in code_cookie
    assert f"oauth-marker={OAUTH_MARKER}" in back["code"].calls.last.request.headers["cookie"]
    kaspi_cookie = back["final"].calls.last.request.headers.get("cookie", "")
    assert "oauth-marker" not in kaspi_cookie
    assert "idmc-marker" not in kaspi_cookie


@pytest.mark.parametrize("url", [AUTH_START_URL, LOGIN_PAGE_URL], ids=["mc", "idmc"])
@pytest.mark.parametrize("status", [400, 403, 404])
async def test_chain_ending_in_client_error_is_unexpected_status(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    url: str,
    status: int,
) -> None:
    """Дополнено, не снято: цепочка кончается 4xx - `UnexpectedStatusError`, ни POST, ни `/s/m`."""
    if url == LOGIN_PAGE_URL:
        mock_kaspi.get(AUTH_START_URL).mock(return_value=redirect_response(LOGIN_PAGE_URL))
    mock_kaspi.get(url).mock(return_value=httpx.Response(status))
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    merchants = mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants())
    with pytest.raises(UnexpectedStatusError) as info:
        await client.login(credentials)
    assert info.value.status_code == status
    assert post.call_count == 0
    assert merchants.call_count == 0


async def test_empty_location_header_is_format_error(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Дополнено, не снято: 302 с пустым `Location` - `ResponseFormatError`, дальше не идём."""
    mock_kaspi.get(AUTH_START_URL).mock(return_value=httpx.Response(302, headers={"Location": ""}))
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    with pytest.raises(ResponseFormatError):
        await client.login(credentials)
    assert post.call_count == 0
    assert len(mock_kaspi.calls) == 1


@pytest.mark.parametrize("where", ["start", "after-post"])
async def test_429_inside_chain_is_rate_limited(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    where: str,
) -> None:
    """Дополнено, не снято: 429 на переходе цепочки - `RateLimitedError`, без повтора."""
    limited = httpx.Response(429, headers={"Retry-After": "17"})
    if where == "start":
        route = mock_kaspi.get(AUTH_START_URL).mock(return_value=limited)
    else:
        synthetic_oauth_start(mock_kaspi)
        mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_redirect(REDIRECT_URL))
        route = mock_kaspi.get(REDIRECT_URL).mock(return_value=limited)
    with pytest.raises(RateLimitedError) as info:
        await client.login(credentials)
    assert info.value.retry_after == 17
    assert route.call_count == 1


@pytest.mark.parametrize("status", [300, 301, 399])
async def test_chain_follows_every_3xx_status(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    credentials: KaspiCredentials,
    status: int,
) -> None:
    """Дополнено, не снято: граница перенаправления - 300 и 399 идут по `Location`."""
    mock_kaspi.get(AUTH_START_URL).mock(
        return_value=httpx.Response(status, headers={"Location": LOGIN_PAGE_URL})
    )
    mock_kaspi.get(LOGIN_PAGE_URL).mock(return_value=httpx.Response(200))
    post = mock_kaspi.post(LOGIN_API_URL).mock(return_value=synthetic_login_error("FAILED"))
    with pytest.raises(LoginRejectedError):
        await client.login(credentials)
    assert post.call_count == 1


async def test_chain_does_not_follow_non_3xx_location(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Дополнено, не снято: 400 с `Location` - ошибка статуса, по `Location` не идём."""
    mock_kaspi.get(AUTH_START_URL).mock(
        return_value=httpx.Response(400, headers={"Location": LOGIN_PAGE_URL})
    )
    login_page = mock_kaspi.get(LOGIN_PAGE_URL).mock(return_value=httpx.Response(200))
    with pytest.raises(UnexpectedStatusError):
        await client.login(credentials)
    assert login_page.call_count == 0


async def test_previous_session_accepted_makes_exactly_chain_and_merchants(
    client: KaspiClient, mock_kaspi: respx.MockRouter, credentials: KaspiCredentials
) -> None:
    """Дополнено, не снято: ветка «не дошла до idmc» - три запроса, цепочка не повторяется."""
    _previous_session_chain(mock_kaspi)
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants())
    await client.login(credentials, previous=authenticated_session())
    assert _hosts(mock_kaspi) == [
        ("GET", "mc.shop.kaspi.kz"),
        ("GET", "kaspi.kz"),
        ("GET", "mc.shop.kaspi.kz"),
    ]
