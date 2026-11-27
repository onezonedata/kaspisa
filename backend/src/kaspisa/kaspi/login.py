"""Вход в кабинет по e-mail и паролю: `login`, `submit_code`, `resend_code`.

ВНИМАНИЕ: вход не проверен на живом Kaspi, перенаправления не сняты. Сняты только два запроса
`POST /api/p/login` (`docs/fixtures/kaspi/login-flow.json`) и `GET /s/m`. Начало цепочки
(`AUTH_START_URL`), поля `_u`, `_p`, `_r_d`, `_m_c`, ответы `redirectUrl`, `email`, `errorCode`
и адрес `/api/p/mfa/send` описаны по коду страницы; перенаправления OAuth в тестах -
синтетика. Первый настоящий вход может потребовать правки модуля (предположения П1, П4, П17,
П25).

Правила:
- вход идёт цепочкой перенаправлений сервера: жёстко заданы только `AUTH_START_URL` и
  `LOGIN_API_URL`, остальные адреса берутся из `Location` и `redirectUrl`;
- перенаправления не выполняются автоматически: каждый адрес проверяется до запроса
  (https и один из хостов `ALLOWED_HOSTS`), пароль уходит только на `LOGIN_API_URL`;
- POST входа и отправки кода не повторяются (повторное письмо обесценивает первый код);
- один набор cookie (`KaspiSession`) на всю цепочку, он же переживает ожидание кода;
- сессия меняет фазу только после того, как исход решён (и, для входа, после `GET /s/m`);
- свои исключения поднимаются вне блоков `except`: цепочки исключений нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from kaspisa.kaspi.cabinet import CABINET_HEADERS, MC_URL, is_session_expired
from kaspisa.kaspi.errors import (
    LoginRejectedError,
    RateLimitedError,
    ResponseFormatError,
    SessionStateError,
    UnexpectedStatusError,
)
from kaspisa.kaspi.models import (
    CodeRequired,
    KaspiCredentials,
    LoggedIn,
    Merchant,
    MerchantsResponse,
)
from kaspisa.kaspi.session import KaspiSession, SessionPhase
from kaspisa.kaspi.transport import ALLOWED_HOSTS, Reply, Transport, parse_body, parse_retry_after

IDMC_URL = "https://idmc.shop.kaspi.kz"
LOGIN_PAGE_URL = f"{IDMC_URL}/login"
LOGIN_API_URL = f"{IDMC_URL}/api/p/login"
MFA_SEND_URL = f"{IDMC_URL}/api/p/mfa/send"
MERCHANTS_URL = f"{MC_URL}/s/m"
# Начало цепочки OAuth - из кода страницы кабинета (не снято, предположение).
AUTH_START_URL = f"{MC_URL}/oauth2/authorization/1"
# Цепочка ходит только по хостам `ALLOWED_HOSTS` транспорта (одна константа на весь клиент):
# cookie `Domain=.kaspi.kz` не уходят на другие.
# Сколько запросов подряд в одной цепочке; десятый ответ - снова перенаправление - ошибка.
MAX_CHAIN_REQUESTS = 10
# Код ошибки Kaspi попадает в текст исключения: пропускаем только вид `MFA_CODE_INVALID`.
_ERROR_CODE = re.compile(r"[A-Z0-9_]{1,64}")
_KASPI_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*kaspi\.kz")
_PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml,*/*"}
# Заголовки запросов входа - как в `login-flow.json`; `Content-Type` ставит `json=`.
_LOGIN_HEADERS = {"Accept": "application/json, text/plain, */*"}


@dataclass(frozen=True)
class _Redirect:
    """Вход выполнен: нужно пройти цепочку от адреса."""

    url: str


@dataclass(frozen=True)
class _CodeSent:
    """Kaspi отправил код на `email`."""

    email: str


def is_kaspi_url(url: str) -> bool:
    """Адрес по https на `kaspi.kz` или его поддомен, который `httpx` может собрать.

    Без логина и нестандартного порта; хост - строго по шаблону (буквы, цифры, дефис).
    Любое исключение при проверке - `False`.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        plain = parts.username is None and parts.password is None and parts.port in (None, 443)
        if parts.scheme != "https" or not plain or _KASPI_HOST.fullmatch(host) is None:
            return False
        return httpx.URL(url).host == host
    except Exception:
        return False


def _login_headers() -> dict[str, str]:
    """`Origin` и `Referer` страницы входа добавляет браузер; `X-XSRF-TOKEN` не отправляется."""
    return {**_LOGIN_HEADERS, "Origin": IDMC_URL, "Referer": LOGIN_PAGE_URL}


def _chain_url_allowed(url: str) -> bool:
    """Адрес по https на один из `ALLOWED_HOSTS`, который `httpx` может собрать."""
    return is_kaspi_url(url) and (urlsplit(url).hostname or "").lower() in ALLOWED_HOSTS


def _json_object(reply: Reply) -> dict[str, Any] | None:
    data: Any = None
    try:
        data = reply.json()
    except (ValueError, RecursionError):
        data = None
    return data if isinstance(data, dict) else None


def _break_seconds(data: dict[str, Any]) -> int | None:
    extra = data.get("errorData")
    if not isinstance(extra, dict):
        return None
    seconds = extra.get("breakTimeSeconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        return None
    return seconds


def _raise_rejection(data: dict[str, Any], operation: str) -> None:
    code = data.get("errorCode")
    if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
        raise ResponseFormatError(operation, ("errorCode",))
    raise LoginRejectedError(code, _break_seconds(data))


def _classify(reply: Reply, operation: str) -> _Redirect | _CodeSent:
    """Разбор ответа входа; порядок проверок важен (раздел 5.8 проекта). Сессию не меняет."""
    if reply.status_code == 429:
        raise RateLimitedError(operation, parse_retry_after(reply))
    success = 200 <= reply.status_code <= 299
    data = _json_object(reply)
    if data is None:
        if not success:
            raise UnexpectedStatusError(operation, reply.status_code)
        raise ResponseFormatError(operation, ("<body>",))
    if "errorCode" in data:
        _raise_rejection(data, operation)
    if not success:
        raise UnexpectedStatusError(operation, reply.status_code)
    if "redirectUrl" in data:
        url = data["redirectUrl"]
        if not isinstance(url, str) or not url:
            raise ResponseFormatError(operation, ("redirectUrl",))
        return _Redirect(url)
    if "email" in data:
        email = data["email"]
        if not isinstance(email, str) or not email:
            raise ResponseFormatError(operation, ("email",))
        return _CodeSent(email)
    if "su" in data:
        raise LoginRejectedError("STAFF_MODE")
    raise ResponseFormatError(operation, ())


async def _follow_chain(
    transport: Transport, session: KaspiSession, url: str, operation: str
) -> tuple[str, Reply]:
    """Цепочка перенаправлений вручную: возвращает хост и ответ, не являющийся 3xx.

    Каждый адрес проверяется до запроса. Один набор cookie сессии на всю цепочку.
    """
    current = url
    for number in range(MAX_CHAIN_REQUESTS):
        if not _chain_url_allowed(current):
            raise ResponseFormatError(operation, ("redirectUrl" if number == 0 else "Location",))
        host = (urlsplit(current).hostname or "").lower()
        reply = await transport.send(
            "GET",
            current,
            operation=operation,
            retry=True,
            session=session,
            headers=_PAGE_HEADERS,
            hide_path=True,
        )
        if reply.status_code == 429:
            raise RateLimitedError(operation, parse_retry_after(reply))
        if not 300 <= reply.status_code <= 399:
            return host, reply
        location = reply.headers.get("Location")
        if not location:
            raise ResponseFormatError(operation, ("Location",))
        current = urljoin(current, location)
    raise ResponseFormatError(operation, ("redirectUrl",))


async def _fetch_merchants(
    transport: Transport, session: KaspiSession, operation: str
) -> list[Merchant]:
    """`GET /s/m`: подтверждает рабочую сессию и даёт список магазинов."""
    reply = await transport.send(
        "GET",
        MERCHANTS_URL,
        operation=operation,
        retry=True,
        session=session,
        headers=CABINET_HEADERS,
    )
    if reply.status_code == 429:
        raise RateLimitedError(operation, parse_retry_after(reply))
    if is_session_expired(reply.status_code, reply.headers):
        raise ResponseFormatError(operation, ("вход не дал рабочей сессии",))
    if not 200 <= reply.status_code <= 299:
        raise UnexpectedStatusError(operation, reply.status_code)
    return parse_body(reply, operation, MerchantsResponse).merchants


async def _complete(transport: Transport, session: KaspiSession, operation: str) -> LoggedIn:
    """Успех входа - `GET /s/m` со списком магазинов; только тогда фаза `AUTHENTICATED`."""
    merchants = await _fetch_merchants(transport, session, operation)
    session._mark_authenticated()
    return LoggedIn(session=session, merchants=merchants)


async def _finish_redirect(
    transport: Transport, session: KaspiSession, outcome: _Redirect, operation: str
) -> LoggedIn:
    """Переход по `redirectUrl` до `mc.shop.kaspi.kz`; статус последнего ответа не важен."""
    await _follow_chain(transport, session, outcome.url, operation)
    return await _complete(transport, session, operation)


async def login(
    transport: Transport, credentials: KaspiCredentials, previous: KaspiSession | None
) -> LoggedIn | CodeRequired:
    """Вход по e-mail и паролю. Не проверено на живом Kaspi, перенаправления не сняты.

    Цепочка идёт от `AUTH_START_URL` по ответам сервера. Если она не дошла до страницы входа
    на `idmc.shop.kaspi.kz`, сервер принял прежние cookie: пароль не отправляется, сразу
    `GET /s/m`. Иначе POST с `_r_d: false` (флаг «запомнить» уходит на шаге с кодом).
    Возвращает `LoggedIn` или `CodeRequired`: нужен код из письма - это результат, не ошибка.
    Cookie `previous` (в том числе запомненного устройства) копируются в новую сессию;
    `previous` не меняется.
    """
    session = KaspiSession()
    if previous is not None:
        session._copy_cookies_from(previous)
    host, reached = await _follow_chain(transport, session, AUTH_START_URL, "login")
    if not 200 <= reached.status_code <= 299:
        raise UnexpectedStatusError("login", reached.status_code)
    if host != "idmc.shop.kaspi.kz":
        return await _complete(transport, session, "login")
    reply = await transport.send(
        "POST",
        LOGIN_API_URL,
        operation="login",
        retry=False,
        session=session,
        headers=_login_headers(),
        json={"_u": credentials.login.reveal(), "_p": credentials.password.reveal(), "_r_d": False},
    )
    outcome = _classify(reply, "login")
    if isinstance(outcome, _CodeSent):
        session._mark_code_sent(outcome.email)
        return CodeRequired(session=session)
    return await _finish_redirect(transport, session, outcome, "login")


async def submit_code(transport: Transport, session: KaspiSession, code: str) -> LoggedIn:
    """Второй шаг входа: код из письма. Сессия обязана быть в фазе `AWAITING_CODE`.

    Не проверено на живом Kaspi. Флаг «запомнить устройство» (`_r_d: true`) уходит здесь.
    При любой ошибке сессия остаётся в `AWAITING_CODE`. После отказа `MFA_CODE_INVALID` код
    можно ввести ещё раз; после сбоя на переходах или `GET /s/m` код мог быть уже принят -
    нужен `login_restart`.
    """
    if session.phase is not SessionPhase.AWAITING_CODE:
        raise SessionStateError("submit_code требует сессию в фазе awaiting_code")
    email = session._pending()
    if email is None:
        raise SessionStateError("в сессии нет адреса, на который отправлен код")
    reply = await transport.send(
        "POST",
        LOGIN_API_URL,
        operation="submit_code",
        retry=False,
        session=session,
        headers=_login_headers(),
        json={"_m_c": code, "_r_d": True, "_u": email},
    )
    outcome = _classify(reply, "submit_code")
    if isinstance(outcome, _CodeSent):
        raise ResponseFormatError("submit_code", ("email",))
    return await _finish_redirect(transport, session, outcome, "submit_code")


async def resend_code(transport: Transport, session: KaspiSession) -> None:
    """Повторная отправка кода (Kaspi: не чаще раза в 180 секунд). Фаза `AWAITING_CODE`."""
    if session.phase is not SessionPhase.AWAITING_CODE:
        raise SessionStateError("resend_code требует сессию в фазе awaiting_code")
    reply = await transport.send(
        "POST",
        MFA_SEND_URL,
        operation="resend_code",
        retry=False,
        session=session,
        headers=_login_headers(),
    )
    if reply.status_code == 429:
        raise RateLimitedError("resend_code", parse_retry_after(reply))
    data = _json_object(reply)
    if data is not None and "errorCode" in data:
        _raise_rejection(data, "resend_code")
    if not 200 <= reply.status_code <= 299:
        raise UnexpectedStatusError("resend_code", reply.status_code)
