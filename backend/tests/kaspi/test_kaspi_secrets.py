"""Секреты и логи: `repr`, логи, исключения, уровни логгеров, код `httpcore` (Т57-Т59, Т62, Т63).

Значения-секреты - константы из `kaspi_testing.py`; в строках вызовов они упоминаются только
по имени: `traceback` печатает исходные строки кадров, и литерал дал бы ложную находку.
"""

# ruff: noqa: F401, F811  (фикстуры импортируются из kaspi_fixtures; conftest.py здесь нельзя)

from __future__ import annotations

import copy
import logging
import pickle
import traceback

import httpcore
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
    ALL_SECRET_MARKERS,
    CITY_ID,
    CODE_MARKER,
    DEVICE_COOKIE_BLOB,
    EDIT_PRICE_URL,
    LIST_URL,
    LOGIN_API_URL,
    LOGIN_MARKER,
    MERCHANT_UID,
    MERCHANTS_URL,
    NEW_SESSION_MARKER,
    PASSWORD_MARKER,
    REDIRECT_QUERY_MARKER,
    REDIRECT_URL,
    REDIRECT_URL_WITH_QUERY,
    SESSION_COOKIE_VALUE,
    SKU,
    TICKET_MARKER,
    WRITE_URL,
    authenticated_session,
    captured_login_reply,
    captured_merchants,
    captured_null_stock,
    expired_response,
    load_fixture,
    null_stock_snapshot,
    redirect_response,
    session_blob,
    synthetic_login_error,
    synthetic_login_redirect,
    synthetic_oauth_return,
    synthetic_oauth_start,
)

import kaspisa.kaspi
from kaspisa.kaspi import (
    CodeRequired,
    KaspiClient,
    KaspiCredentials,
    KaspiError,
    KaspiSession,
    LoggedIn,
    SessionStateError,
)
from kaspisa.kaspi.models import Merchant
from kaspisa.kaspi.transport import Reply
from kaspisa.security import Secret

COOKIE_MARKER = "cookie-marker-q7x3v9"


def test_noisy_loggers_are_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Т62: после импорта пакета `httpx`, `httpcore`, `hpack` на `WARNING`."""
    for name in ("httpx", "httpcore", "hpack"):
        assert logging.getLogger(name).level == logging.WARNING
    caplog.set_level(logging.DEBUG)
    assert not logging.getLogger("httpcore.http11").isEnabledFor(logging.DEBUG)


async def _request_with_cookie_in_response() -> None:
    backend = httpcore.AsyncMockBackend(
        [
            f"HTTP/1.1 200 OK\r\nSet-Cookie: mc-session={COOKIE_MARKER}\r\n"
            "Content-Length: 2\r\n\r\n{}".encode()
        ]
    )
    async with httpcore.AsyncConnectionPool(network_backend=backend) as pool:
        response = await pool.request("GET", "https://mc.shop.kaspi.kz/s/m")
        assert response.status == 200


async def test_httpcore_does_not_log_cookie(caplog: pytest.LogCaptureFixture) -> None:
    """Т63: настоящий код `httpcore` на подставном сетевом слое не пишет cookie в лог."""
    caplog.set_level(logging.DEBUG)
    await _request_with_cookie_in_response()
    assert COOKIE_MARKER not in caplog.text


async def test_control_cookie_leaks_when_level_removed(caplog: pytest.LogCaptureFixture) -> None:
    """Т63, контроль: при сброшенном уровне значение в лог попадает (тест видит код httpcore)."""
    logger = logging.getLogger("httpcore")
    previous = logger.level
    caplog.set_level(logging.DEBUG)
    logger.setLevel(logging.NOTSET)
    try:
        await _request_with_cookie_in_response()
    finally:
        logger.setLevel(previous)
    assert COOKIE_MARKER in caplog.text


def _all_secret_views() -> list[object]:
    awaiting = KaspiSession.load(
        session_blob(
            phase="awaiting_code",
            pending_login=LOGIN_MARKER,
            cookies=json_cookies_with_device(),
        )
    )
    credentials = KaspiCredentials(login=Secret(LOGIN_MARKER), password=Secret(PASSWORD_MARKER))
    return [
        awaiting,
        credentials,
        LoggedIn(session=authenticated_session(), merchants=[Merchant(uid=MERCHANT_UID)]),
        CodeRequired(session=awaiting),
    ]


def json_cookies_with_device() -> list[dict[str, object]]:
    return [
        {
            "name": "mc-session",
            "value": SESSION_COOKIE_VALUE,
            "domain": "mc.shop.kaspi.kz",
            "path": "/",
            "secure": True,
            "expires": None,
            "host_only": True,
        },
        DEVICE_COOKIE_BLOB,
    ]


def test_reprs_hide_secrets() -> None:
    """Т57: `repr`, `str`, f-строка не содержат пароля, логина, cookie, e-mail из блока."""
    for obj in _all_secret_views():
        for text in (repr(obj), str(obj), f"{obj}", f"{obj!r}", format(obj, "")):
            for marker in ALL_SECRET_MARKERS:
                assert marker not in text


def test_session_cannot_be_pickled_or_copied() -> None:
    """Т57: `pickle` и `deepcopy` сессии и результатов входа - `TypeError`."""
    for obj in _all_secret_views():
        if isinstance(obj, KaspiCredentials):
            continue  # поля-`Secret` и так не сериализуются; проверяется ниже
        with pytest.raises(TypeError):
            pickle.dumps(obj)
        with pytest.raises(TypeError):
            copy.deepcopy(obj)
    with pytest.raises(TypeError):
        copy.deepcopy(
            KaspiCredentials(login=Secret(LOGIN_MARKER), password=Secret(PASSWORD_MARKER))
        )


def test_reply_repr_shows_only_status() -> None:
    """Т57: `repr` и `str` `Reply` с `Set-Cookie` и e-mail в теле - `Reply(status_code=200)`."""
    reply = Reply(
        200,
        httpx.Headers([("Set-Cookie", f"mc-session={NEW_SESSION_MARKER}; Path=/")]),
        f'{{"email": "{LOGIN_MARKER}"}}'.encode(),
    )
    assert repr(reply) == "Reply(status_code=200)"
    assert str(reply) == "Reply(status_code=200)"


async def _scenario(client: KaspiClient, mock_kaspi: respx.MockRouter) -> list[KaspiError]:
    """Полный сценарий (Т58): вход с кодом, чтения, запись, сбои. Возвращает пойманные ошибки."""
    caught: list[KaspiError] = []
    credentials = KaspiCredentials(login=Secret(LOGIN_MARKER), password=Secret(PASSWORD_MARKER))
    state = load_fixture("edit-price.json")
    snapshot = null_stock_snapshot()
    ok = httpx.Response(
        200,
        json=state,
        headers=[("Set-Cookie", f"mc-session={NEW_SESSION_MARKER}; Domain=.kaspi.kz; Path=/")],
    )
    synthetic_oauth_start(mock_kaspi)
    mock_kaspi.post(LOGIN_API_URL).mock(
        side_effect=[
            captured_login_reply("пароль", email=LOGIN_MARKER),
            synthetic_login_error("MFA_CODE_INVALID"),
            synthetic_login_redirect(REDIRECT_URL_WITH_QUERY),
            synthetic_login_redirect(REDIRECT_URL_WITH_QUERY),
            synthetic_login_error("CREDENTIALS_INVALID"),
            synthetic_login_redirect("https://evil.example/?t=" + REDIRECT_QUERY_MARKER),
        ]
    )
    # Одноразовый код возврата (`TICKET_MARKER`) стоит в адресе возврата на `mc.shop.kaspi.kz`.
    back = synthetic_oauth_return(mock_kaspi, REDIRECT_URL_WITH_QUERY)
    # Сбой на шаге цепочки после кода: адрес с одноразовым кодом не должен попасть в ошибку.
    code_step = back["code"].mock(
        side_effect=[httpx.ConnectError(TICKET_MARKER)] * 3 + [redirect_response(REDIRECT_URL)]
    )
    mock_kaspi.get(MERCHANTS_URL).mock(return_value=captured_merchants())
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        side_effect=[
            ok,
            httpx.ConnectError(PASSWORD_MARKER),
            httpx.ConnectError(PASSWORD_MARKER),
            httpx.ConnectError(PASSWORD_MARKER),
            expired_response(),
            httpx.Response(200, json={"unexpected": CODE_MARKER}),
        ]
    )
    mock_kaspi.post(WRITE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("price-update-response.json"))
    )
    # Подтверждение записи идёт по списку товаров (ADR 0006); строка - из снимка записи.
    mock_kaspi.post(url__startswith=LIST_URL).mock(
        return_value=httpx.Response(
            201, json={"data": [captured_null_stock()["listRow"]], "total": 1}
        )
    )

    first = await client.login(credentials)
    assert isinstance(first, CodeRequired)
    awaiting = first.session
    for _ in range(2):  # неверный код, затем сбой цепочки после кода
        try:
            await client.submit_code(awaiting, CODE_MARKER)
        except KaspiError as exc:
            caught.append(exc)
    assert code_step.call_count == 3
    done = await client.submit_code(awaiting, CODE_MARKER)
    session = done.session
    await client.get_product(session, MERCHANT_UID, SKU)
    await client.set_price(session, snapshot, {CITY_ID: 2600})
    await client.verify_price(session, snapshot, {CITY_ID: 2998})
    for call in (
        lambda: client.get_product(session, MERCHANT_UID, SKU),  # сетевой сбой
        lambda: client.get_product(session, MERCHANT_UID, SKU),  # истёкшая сессия
        lambda: client.get_product(session, MERCHANT_UID, SKU),  # формат ответа
        lambda: client.login(credentials),  # отказ входа
        lambda: client.login(credentials),  # адрес перехода вне Kaspi
        lambda: client.submit_code(session, CODE_MARKER),  # не та фаза
    ):
        try:
            await call()
        except KaspiError as exc:
            caught.append(exc)
    return caught


async def test_scenario_logs_contain_no_secrets(
    client: KaspiClient, mock_kaspi: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """Т58: в логах на уровне DEBUG нет ни одного секрета.

    Ограничение: `respx` подменяет пул соединений, поэтому код `httpcore` здесь не выполняется;
    этот путь закрывают Т62 и Т63.
    """
    caplog.set_level(logging.DEBUG)
    await _scenario(client, mock_kaspi)
    assert caplog.records, "тест не видит ни одной записи лога"
    texts = [caplog.text]
    for record in caplog.records:
        texts += [record.getMessage(), str(record.args), record.exc_text or ""]
    for text in texts:
        for marker in ALL_SECRET_MARKERS:
            assert marker not in text
        assert TICKET_MARKER not in text
    messages = [record.getMessage() for record in caplog.records]
    assert any(" GET kaspi.kz " in m for m in messages)
    assert any("get_product" in m and "/bff/offer-view/edit-price" in m for m in messages)


async def test_scenario_exceptions_contain_no_secrets(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т59: у каждой ошибки сценария текст, `repr` и трассировка чисты, цепочки нет."""
    caught = await _scenario(client, mock_kaspi)
    assert len(caught) == 8
    for exc in caught:
        rendered = [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
        for text in rendered:
            for marker in ALL_SECRET_MARKERS:
                assert marker not in text
        assert exc.__cause__ is None
        assert exc.__context__ is None
