"""Помощники сквозного теста входа на настоящем клиенте Kaspi (Т50-Т51).

Копия нужного из tests/kaspi/kaspi_testing.py: из другого каталога тестов модуль не виден,
а одноимённый модуль ломает mypy. Синтетика (не снято) помечена как в оригинале.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import respx

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi"
MC_HOST = "https://mc.shop.kaspi.kz"
MERCHANT_UID = "90100003"
SESSION_COOKIE_VALUE = "marker-session-value-7f3a"
SID_COOKIE_VALUE = "marker-sid-value-91bc"


def load_fixture(name: str) -> Any:
    """Читает пример ответа из `docs/fixtures/kaspi/`."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTime:
    """Поддельные часы и `sleep`: сон двигает время и реально не спит."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        target = self.now + delay
        await asyncio.sleep(0)
        self.now = max(self.now, target)


def captured_merchants() -> httpx.Response:
    """Ответ `GET /s/m` из `session-merchants.json` (снято 2026-11-14): один магазин."""
    return httpx.Response(200, json=load_fixture("session-merchants.json"))


# --- вход ---
# Сняты только два запроса `POST /api/p/login` (`login-flow.json`) и `GET /s/m`. Перенаправления
# OAuth не сняты: цепочки ниже - синтетика, не снято (вход не проверен на живом Kaspi).

IDMC_HOST = "https://idmc.shop.kaspi.kz"
AUTH_START_URL = f"{MC_HOST}/oauth2/authorization/1"
LOGIN_PAGE_URL = f"{IDMC_HOST}/login"
LOGIN_API_URL = f"{IDMC_HOST}/api/p/login"
MFA_SEND_URL = f"{IDMC_HOST}/api/p/mfa/send"
MERCHANTS_URL = f"{MC_HOST}/s/m"
LIST_URL = f"{MC_HOST}/bff/offer-view/v2/list"
REDIRECT_URL = "https://kaspi.kz/mc/"
TICKET_MARKER = "ticket-marker-7f3a"
MC_CODE_URL = f"{MC_HOST}/login/oauth2/code/1?code={TICKET_MARKER}"
START_STATE_MARKER = "start-state-marker"
START_AUTHORIZE_URL = f"{IDMC_HOST}/oauth2/authorize?state={START_STATE_MARKER}"

# Выдуманные значения: настоящих секретов в тестах нет.
LOGIN_MARKER = "seller-marker@example.invalid"
PASSWORD_MARKER = "password-marker-k4z9w2"  # noqa: S105
CODE_MARKER = "code-marker-731905"
NEW_SESSION_MARKER = "new-session-marker-e41d"
NEW_SID_MARKER = "new-sid-marker-20ff"
DEVICE_MARKER = "device-marker-9a77"
REDIRECT_QUERY_MARKER = "redirect-query-marker-d8e1"
OAUTH_MARKER = "oauth-marker-5c1e"
IDMC_MARKER = "idmc-marker-0b44"
REDIRECT_URL_WITH_QUERY = f"{IDMC_HOST}/oauth2/authorize?state={REDIRECT_QUERY_MARKER}"
ALL_SECRET_MARKERS = (
    LOGIN_MARKER,
    PASSWORD_MARKER,
    CODE_MARKER,
    NEW_SESSION_MARKER,
    NEW_SID_MARKER,
    DEVICE_MARKER,
    REDIRECT_QUERY_MARKER,
    OAUTH_MARKER,
    IDMC_MARKER,
    START_STATE_MARKER,
    TICKET_MARKER,
    SESSION_COOKIE_VALUE,
    SID_COOKIE_VALUE,
)

DEVICE_COOKIE_BLOB = {
    "name": "device-id",
    "value": DEVICE_MARKER,
    "domain": ".kaspi.kz",
    "path": "/",
    "secure": True,
    "expires": 4102444800,
    "host_only": False,
}


def captured_login_step(step: str) -> dict[str, Any]:
    """Шаг `login-flow.json` по имени: `"пароль"` или `"код из письма"` (снято)."""
    steps: list[dict[str, Any]] = load_fixture("login-flow.json")
    return next(item for item in steps if item["step"] == step)


def captured_login_reply(step: str, *, email: str | None = None) -> httpx.Response:
    """Ответ входа из шага `login-flow.json` (`status` и `responseBody`, снято).

    В снятом ответе e-mail заменён на `***`; `email` подставляет выдуманный маркер там,
    где тест ищет утечки.
    """
    item = captured_login_step(step)
    body = dict(item["responseBody"])
    if email is not None and "email" in body:
        body["email"] = email
    return httpx.Response(item["status"], json=body)


def captured_redirect_url() -> str:
    """`redirectUrl` из шага «код из письма» (снято; значения параметров заменены на `***`)."""
    url: str = captured_login_step("код из письма")["responseBody"]["redirectUrl"]
    return url


def synthetic_login_redirect(url: str) -> httpx.Response:
    """Синтетика, не снято: ответ входа с подменённым `redirectUrl` (негодные адреса, метки)."""
    return httpx.Response(200, json={"redirectUrl": url})


def synthetic_login_error(
    code: str, *, status: int = 200, break_time: object = None
) -> httpx.Response:
    """Синтетика, не снято: ошибка входа с `errorCode` (и `errorData.breakTimeSeconds`).

    Заменить файлами `login-error-<код>.json` после снятия входа.
    """
    body: dict[str, object] = {"errorCode": code}
    if break_time is not None:
        body["errorData"] = {"breakTimeSeconds": break_time}
    return httpx.Response(status, json=body)


def synthetic_login_staff_mode() -> httpx.Response:
    """Синтетика, не снято: ответ входа с полем `su` (режим сотрудника).

    Заменить файлом `login-staff-mode.json` после снятия входа.
    """
    return httpx.Response(200, json={"su": True})


def synthetic_mfa_send_ok() -> httpx.Response:
    """Синтетика, не снято: успешная повторная отправка кода.

    Заменить файлом `mfa-send.json` после снятия входа.
    """
    return httpx.Response(200, json={})


def redirect_response(location: str, *cookies: str) -> httpx.Response:
    """Синтетика, не снято: 302 с `Location` и, при необходимости, `Set-Cookie`."""
    headers = [("Location", location), *(("Set-Cookie", cookie) for cookie in cookies)]
    return httpx.Response(302, headers=headers)


def synthetic_oauth_start(mock: respx.MockRouter) -> dict[str, respx.Route]:
    """Синтетика, не снято: перенаправления OAuth в начале входа.

    `GET AUTH_START_URL` -> 302 на `idmc.../oauth2/authorize?state=...` (cookie `oauth-marker`
    для хоста `mc.shop.kaspi.kz`, сессионная) -> 302 на `/login` (cookie `idmc-marker` для
    `idmc.shop.kaspi.kz`) -> 200.
    """
    return {
        "start": mock.get(AUTH_START_URL).mock(
            return_value=redirect_response(
                START_AUTHORIZE_URL, f"oauth-marker={OAUTH_MARKER}; Path=/; Secure"
            )
        ),
        "authorize": mock.get(START_AUTHORIZE_URL).mock(
            return_value=redirect_response(
                LOGIN_PAGE_URL, f"idmc-marker={IDMC_MARKER}; Path=/; Secure"
            )
        ),
        "page": mock.get(LOGIN_PAGE_URL).mock(
            return_value=httpx.Response(200, text="<html></html>")
        ),
    }


def synthetic_oauth_return(
    mock: respx.MockRouter, redirect_url: str | None = None
) -> dict[str, respx.Route]:
    """Синтетика, не снято: перенаправления OAuth после `redirectUrl`.

    `GET redirect_url` -> 302 на `mc.shop.kaspi.kz/login/oauth2/code/1?code=<TICKET_MARKER>`
    -> 302 на `https://kaspi.kz/mc/` (cookie `mc-session`, `mc-sid`, cookie устройства) -> 200.
    """
    url = redirect_url or captured_redirect_url()
    return {
        "authorize": mock.get(url).mock(return_value=redirect_response(MC_CODE_URL)),
        "code": mock.get(MC_CODE_URL).mock(return_value=redirect_response(REDIRECT_URL)),
        "final": mock.get(REDIRECT_URL).mock(
            return_value=httpx.Response(
                200,
                headers=[
                    (
                        "Set-Cookie",
                        f"mc-session={NEW_SESSION_MARKER}; Domain=.kaspi.kz; Path=/; Secure",
                    ),
                    ("Set-Cookie", f"mc-sid={NEW_SID_MARKER}; Domain=.kaspi.kz; Path=/; Secure"),
                    (
                        "Set-Cookie",
                        f"device-id={DEVICE_MARKER}; Domain=.kaspi.kz; Path=/; Secure; "
                        "Expires=Wed, 01 Jan 2100 00:00:00 GMT",
                    ),
                ],
                text="<html></html>",
            )
        ),
    }
