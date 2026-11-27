"""`KaspiSession`: сохраняемый блок, изоляция по хосту, посторонние cookie (Т40-Т44)."""

# ruff: noqa: F401, F811  (фикстуры импортируются из kaspi_fixtures; conftest.py здесь нельзя)

from __future__ import annotations

import json
import os
import uuid

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
    EDIT_PRICE_URL,
    MERCHANT_UID,
    SESSION_COOKIE_VALUE,
    SKU,
    authenticated_session,
    load_fixture,
    session_blob,
)

from kaspisa.kaspi import KaspiClient, KaspiSession, SessionBlobError, SessionStateError
from kaspisa.kaspi.session import SessionPhase
from kaspisa.security import KeyRing, Secret, SecretCipher, SecretContext, SecretField

DEVICE_COOKIE = {
    "name": "device-id",
    "value": "marker-device-value-55aa",
    "domain": ".kaspi.kz",
    "path": "/",
    "secure": True,
    "expires": 4102444800,
    "host_only": False,
}


def _full_blob() -> Secret:
    base = json.loads(session_blob().reveal())
    base["cookies"].append(DEVICE_COOKIE)
    base["phase"] = "awaiting_code"
    base["pending_login"] = "marker@example.invalid"
    return Secret(json.dumps(base))


def test_dump_load_roundtrip_keeps_everything() -> None:
    """Т40: фаза, `pending_login` и все поля cookie сохраняются при `dump` и `load`."""
    original = json.loads(_full_blob().reveal())
    session = KaspiSession.load(_full_blob())
    assert session.phase is SessionPhase.AWAITING_CODE
    assert session.changed is False
    restored = json.loads(session.dump().reveal())
    assert restored["v"] == 1
    assert restored["phase"] == "awaiting_code"
    assert restored["pending_login"] == "marker@example.invalid"
    key = lambda c: c["name"]  # noqa: E731
    assert sorted(restored["cookies"], key=key) == sorted(original["cookies"], key=key)


async def test_host_only_cookie_not_sent_to_other_host(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т40: после `load` cookie хоста `mc.shop.kaspi.kz` не уходит на `idmc.shop.kaspi.kz`."""
    session = KaspiSession.load(_full_blob())
    mc = mock_kaspi.get("https://mc.shop.kaspi.kz/s/m").mock(return_value=httpx.Response(200))
    idmc = mock_kaspi.get("https://idmc.shop.kaspi.kz/login").mock(return_value=httpx.Response(200))
    transport = client._transport
    await transport.send(
        "GET", "https://mc.shop.kaspi.kz/s/m", operation="t", retry=False, session=session
    )
    await transport.send(
        "GET", "https://idmc.shop.kaspi.kz/login", operation="t", retry=False, session=session
    )
    mc_cookie = mc.calls.last.request.headers["cookie"]
    idmc_cookie = idmc.calls.last.request.headers["cookie"]
    assert "mc-session=" in mc_cookie
    assert "device-id=" in mc_cookie
    assert "mc-session" not in idmc_cookie
    assert "device-id=" in idmc_cookie  # cookie домена `.kaspi.kz` уходит на все хосты Kaspi


def test_dump_returns_secret_and_new_phase_refuses() -> None:
    """Т41: `dump()` возвращает `Secret`; в фазе `NEW` - `SessionStateError`."""
    assert isinstance(authenticated_session().dump(), Secret)
    with pytest.raises(SessionStateError):
        KaspiSession().dump()


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all marker-garbage",
        json.dumps({"v": 2, "phase": "authenticated", "pending_login": None, "cookies": []}),
        json.dumps({"v": 1, "phase": "mystery", "pending_login": None, "cookies": []}),
        json.dumps({"v": 1, "phase": "authenticated", "pending_login": None}),
        json.dumps({"v": 1, "phase": "new", "pending_login": None, "cookies": []}),
        json.dumps(
            {"v": 1, "phase": "authenticated", "pending_login": None, "cookies": [{"name": "x"}]}
        ),
        json.dumps(
            {
                "v": 1,
                "phase": "authenticated",
                "pending_login": None,
                "cookies": [{**DEVICE_COOKIE, "domain": "evil.example"}],
            }
        ),
        json.dumps([1, 2, 3]),
    ],
)
def test_load_rejects_bad_blob_without_leaking_content(raw: str) -> None:
    """Т42: плохой блок - `SessionBlobError`, содержимого блока в тексте ошибки нет."""
    with pytest.raises(SessionBlobError) as info:
        KaspiSession.load(Secret(raw))
    assert "marker-" not in str(info.value)
    assert "evil.example" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


async def test_foreign_domain_cookie_not_saved(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т43: cookie постороннего домена из `Set-Cookie` не попадает в сессию."""
    session = authenticated_session()
    mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        return_value=httpx.Response(
            200,
            json=load_fixture("edit-price.json"),
            headers=[
                ("Set-Cookie", "foreign-cookie=marker-foreign; Domain=evil.example; Path=/"),
                ("Set-Cookie", "kept-cookie=marker-kept; Path=/; Secure"),
            ],
        )
    )
    await client.get_product(session, MERCHANT_UID, SKU)
    names = {c["name"] for c in json.loads(session.dump().reveal())["cookies"]}
    assert "foreign-cookie" not in names
    assert "kept-cookie" in names
    assert session.changed is True


def test_blob_passes_through_secret_cipher() -> None:
    """Т44: `encrypt(dump())` -> `decrypt` -> `load` даёт равную сессию."""
    cipher = SecretCipher(KeyRing(1, {1: os.urandom(32)}))
    context = SecretContext(uuid.uuid4(), uuid.uuid4(), SecretField.SHOP_SESSION)
    session = KaspiSession.load(_full_blob())
    blob = cipher.encrypt(session.dump(), context)
    assert SESSION_COOKIE_VALUE.encode() not in blob
    restored = KaspiSession.load(cipher.decrypt(blob, context))
    assert restored == session
    assert restored.phase is SessionPhase.AWAITING_CODE


async def test_host_only_cookie_from_apex_not_sent_to_subdomain(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Cookie без `Domain`, выданный `kaspi.kz`, не уходит на поддомен (флаг `strict_ns_domain`).

    Вход идёт через `kaspi.kz/mc/`: такой cookie не должен попасть на `mc.shop.kaspi.kz`.
    """
    session = KaspiSession()
    apex = mock_kaspi.get("https://kaspi.kz/mc/").mock(
        side_effect=[
            httpx.Response(200, headers={"Set-Cookie": "apex-cookie=marker-apex; Path=/"}),
            httpx.Response(200),
        ]
    )
    mc = mock_kaspi.get("https://mc.shop.kaspi.kz/s/m").mock(return_value=httpx.Response(200))
    transport = client._transport
    await transport.send("GET", "https://kaspi.kz/mc/", operation="t", retry=False, session=session)
    await transport.send(
        "GET", "https://mc.shop.kaspi.kz/s/m", operation="t", retry=False, session=session
    )
    await transport.send("GET", "https://kaspi.kz/mc/", operation="t", retry=False, session=session)
    assert "cookie" not in mc.calls.last.request.headers
    assert "apex-cookie=marker-apex" in apex.calls.last.request.headers["cookie"]
