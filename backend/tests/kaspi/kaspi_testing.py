"""Общие помощники тестов клиента Kaspi: поддельное время, примеры ответов, синтетика.

Синтетические ответы (там, где настоящих примеров нет) собираются только здесь:
после снятия настоящих примеров правится один этот файл.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import respx

from kaspisa.kaspi import IntervalRateLimiter, KaspiSession
from kaspisa.kaspi.models import ListedProduct, ProductSnapshot, ProductState
from kaspisa.security import Secret

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi"


def load_fixture(name: str) -> Any:
    """Читает пример ответа из `docs/fixtures/kaspi/`."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTime:
    """Поддельные часы и `sleep`: сон двигает время и записывается, реально не спит."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        target = self.now + delay
        await asyncio.sleep(0)  # даём поработать другим задачам, как при настоящем сне
        self.now = max(self.now, target)


class RecordingLimiter(IntervalRateLimiter):
    """Настоящий ограничитель, который дополнительно записывает ключи и моменты пропуска."""

    def __init__(self, fake: FakeTime, min_interval: float = 1.0) -> None:
        super().__init__(min_interval, clock=fake.clock, sleep=fake.sleep)
        self.fake = fake
        self.keys: list[str] = []
        self.passed: list[tuple[str, float]] = []

    async def acquire(self, key: str) -> None:
        self.keys.append(key)
        await super().acquire(key)
        self.passed.append((key, self.fake.now))


def synthetic_offers_page(offers: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """Синтетика, не снято: страница продавцов с заданным `total`.

    Заменить настоящим постраничным ответом после снятия (`docs/fixtures/kaspi/offers.json`
    содержит только одну страницу).
    """
    return {"offers": offers, "total": total, "offersCount": total}


MERCHANT_UID = "90100003"
SKU = "900000005_900000024"
CITY_ID = "710000000"
MC_HOST = "https://mc.shop.kaspi.kz"
EDIT_PRICE_URL = f"{MC_HOST}/bff/offer-view/edit-price"
LIST_URL = f"{MC_HOST}/bff/offer-view/v2/list"
WRITE_URL = f"{MC_HOST}/pricefeed/upload/merchant/process"

# Выдуманные значения cookie: настоящих секретов в тестах нет.
SESSION_COOKIE_VALUE = "marker-session-value-7f3a"
SID_COOKIE_VALUE = "marker-sid-value-91bc"


def session_blob(
    *,
    phase: str = "authenticated",
    pending_login: str | None = None,
    cookies: list[dict[str, Any]] | None = None,
) -> Secret:
    """Блок сессии в формате `KaspiSession.dump()`; по умолчанию - сессия кабинета."""
    if cookies is None:
        cookies = [
            {
                "name": "mc-session",
                "value": SESSION_COOKIE_VALUE,
                "domain": "mc.shop.kaspi.kz",
                "path": "/",
                "secure": True,
                "expires": None,
                "host_only": True,
            },
            {
                "name": "mc-sid",
                "value": SID_COOKIE_VALUE,
                "domain": "mc.shop.kaspi.kz",
                "path": "/",
                "secure": True,
                "expires": 4102444800,
                "host_only": True,
            },
        ]
    return Secret(
        json.dumps(
            {"v": 1, "phase": phase, "pending_login": pending_login, "cookies": cookies},
            ensure_ascii=False,
        )
    )


def authenticated_session(**kwargs: Any) -> KaspiSession:
    """Сессия в фазе `AUTHENTICATED` (как после `KaspiSession.load` из хранилища)."""
    return KaspiSession.load(session_blob(**kwargs))


def snapshot_from(state_data: dict[str, Any], listing_data: dict[str, Any]) -> ProductSnapshot:
    """Собирает `ProductSnapshot` из сырых словарей (как их вернул бы Kaspi)."""
    return ProductSnapshot(
        state=ProductState.model_validate(state_data),
        listing=ListedProduct.model_validate(listing_data),
    )


def null_stock_snapshot() -> ProductSnapshot:
    """Снимок записи из снятого НО (`null-stock-product.json`): чтение и строка списка."""
    sample = captured_null_stock()
    return snapshot_from(sample["editPrice"], sample["listRow"])


def expired_response() -> httpx.Response:
    """Ответ кабинета при истёкшей сессии: 401 с пустым телом.

    Проверено 2026-11-14 (`GET /s/m`, список, чтение товара, счётчики); файла примера нет,
    потому что тело пустое. Для запроса записи ответ при истёкшей сессии не снят.
    """
    return httpx.Response(401)


def synthetic_redirect_response() -> httpx.Response:
    """Синтетика, не снято, предположение: перенаправление на вход при истёкшей сессии.

    Заменить настоящим ответом, если Kaspi когда-нибудь отдаст его в кабинете.
    """
    return httpx.Response(302, headers={"Location": "https://idmc.shop.kaspi.kz/login"})


def captured_null_stock() -> dict[str, Any]:
    """Товар с неуказанным остатком (`null-stock-product.json`) без правок.

    Ключи: `listRequestBody`, `listRow`, `editPrice`, `priceUpdateRequest`,
    `priceUpdateResponse`. Строка списка сокращена в самом примере до нужных полей.
    """
    data: dict[str, Any] = load_fixture("null-stock-product.json")
    return data


def captured_point_off() -> tuple[dict[str, Any], dict[str, Any]]:
    """Товар с выключенной точкой PP3 (`point-off-product.json`): строка списка и чтение.

    Дополнено, не снято (в примере этих полей нет): в строке списка `sku` (из корня примера),
    `masterSku: "900000001"`, `merchantUid: "90100003"`, `available: true`, `archivedAt: null`;
    в чтении `sku`, `masterSku`, `title: "Товар с выключенной точкой"`.
    """
    data = load_fixture("point-off-product.json")
    list_row: dict[str, Any] = {
        **data["listRow"],
        "sku": data["sku"],
        "masterSku": "900000001",
        "merchantUid": "90100003",
        "available": True,
        "archivedAt": None,
    }
    edit_price: dict[str, Any] = {
        **data["editPrice"],
        "sku": data["sku"],
        "masterSku": "900000001",
        "title": "Товар с выключенной точкой",
    }
    return list_row, edit_price


def captured_stock_counted() -> tuple[dict[str, Any], dict[str, Any]]:
    """Товар с указанным остатком (`stock-counted-product.json`): строка списка и чтение.

    Собрано по наблюдению (ADR 0036), не полный снятый ответ: PP1 в строке 100, в чтении 94;
    PP3 `no`, 0. Цена 490, город 710000000. Порядок точек чтения - PP3, PP1.
    """
    data = load_fixture("stock-counted-product.json")
    return data["listRow"], data["editPrice"]


def point_off_unspecified() -> tuple[dict[str, Any], dict[str, Any]]:
    """ВТн: `captured_point_off()`, у которого точка PP1 приведена к «остаток не указан».

    Правка снятого примера, не снято: в строке списка PP1 - `available: "yes"`,
    `stockCount: null`, `stockSpecified: false`; в чтении PP1 - `stock: null`. PP3 как в
    примере: в списке `no`, 0, в чтении 0. Цена 1660.
    """
    list_row, edit_price = captured_point_off()
    for availability in list_row["availabilities"]:
        if availability["storeId"] == "90100003_PP1":
            availability.update(available="yes", stockCount=None, stockSpecified=False)
    for city in edit_price["cities"]:
        for point in city["points"]:
            if point["id"] == "90100003_PP1":
                point["stock"] = None
    return list_row, edit_price


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
