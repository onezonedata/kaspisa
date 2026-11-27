"""ADR 0025, Т17-Т22, Т24, Т51: API настроек Telegram, проверка, сообщение в прежний чат."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from api_testing import (
    api_cipher,
    api_database_url,
    confirm_password,
    deps,
    raw_engine,
    redis_client,
    redis_prefix,
    signed_in,
    sql_exec,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api.telegram import (
    Notifier,
    text_settings_changed,
    text_settings_deleted,
    text_test,
)
from kaspisa.db import NO_SHOP, TenantId
from kaspisa.security import KeyRing, Secret, SecretCipher, SecretContext, SecretField

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

T = "123456789:" + "A" * 35
T2 = "987654321:" + "B" * 35
API = "https://api.telegram.org"
URL = f"{API}/bot{T}/sendMessage"
URL2 = f"{API}/bot{T2}/sendMessage"
PATH = "/api/notifications/telegram"


@dataclass(frozen=True)
class Sent:
    token: str
    chat_id: str
    text: str


def sent_messages(mock: respx.MockRouter) -> list[Sent]:
    result = []
    for call in mock.calls:
        body = json.loads(call.request.content)
        token = call.request.url.path.removeprefix("/bot").removesuffix("/sendMessage")
        result.append(Sent(token, body["chat_id"], body["text"]))
    return result


def notifier_of(deps: ApiDeps) -> Notifier:
    assert deps.notifier is not None
    return deps.notifier


async def stored(deps: ApiDeps, tenant: TenantId) -> tuple[str, str] | None:
    async with deps.database.tenant(tenant) as scope:
        target = await scope.telegram.get_target()
    return None if target is None else (target.token.reveal(), target.chat_id)


async def configured(
    deps: ApiDeps, raw: AsyncEngine, email: str = "a@example.com", ip: str = "127.0.0.1"
) -> tuple[httpx.AsyncClient, TenantId]:
    """Пользователь с сохранёнными настройками `(T, "42")` и подтверждённым паролем."""
    client = await signed_in(deps, email, ip=ip)
    tenant = TenantId(await tenant_of(raw, email))
    async with deps.database.tenant(tenant) as scope:
        await scope.telegram.save(chat_id="42", token=Secret(T))
        await scope.commit()
    await confirm_password(client)
    return client, tenant


async def test_get_without_settings(deps: ApiDeps) -> None:
    """Т17."""
    client = await signed_in(deps)
    response = await client.get(PATH)
    assert response.status_code == 200
    assert response.json() == {"token_set": False, "chat_id": None}


async def test_save_flow_and_password_for_change(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т18: первая настройка без пароля; смена требует пароль; токен нигде не возвращается."""
    client = await signed_in(deps)
    tenant = TenantId(await tenant_of(raw_engine, "a@example.com"))
    with respx.mock(assert_all_called=False) as mock:
        first = await client.put(PATH, json={"token": T, "chat_id": "42"})
        assert first.status_code == 200
        assert first.json() == {"token_set": True, "chat_id": "42"}
        assert T not in first.text
        assert T not in (await client.get(PATH)).text
        assert await stored(deps, tenant) == (T, "42")

        denied = await client.put(PATH, json={"token": T2, "chat_id": "43"})
        assert (denied.status_code, denied.json()["code"]) == (403, "password_required")
        assert await stored(deps, tenant) == (T, "42")
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []

        await confirm_password(client)
        changed = await client.put(PATH, json={"token": T2, "chat_id": "43"})
        assert changed.status_code == 200
        assert T2 not in changed.text
        assert await stored(deps, tenant) == (T2, "43")
        await notifier_of(deps).drain()


async def test_negative_group_chat_id_is_accepted(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т53 (ADR 0031): chat id группы отрицателен; одинокий минус и двойной минус - 422."""
    client = await signed_in(deps)
    tenant = TenantId(await tenant_of(raw_engine, "a@example.com"))
    with respx.mock(assert_all_called=False):
        response = await client.put(PATH, json={"token": T, "chat_id": "-1001234567890"})
        assert response.status_code == 200
        assert response.json() == {"token_set": True, "chat_id": "-1001234567890"}
        assert await stored(deps, tenant) == (T, "-1001234567890")
        await confirm_password(client)
        for bad in ("-", "--1"):
            denied = await client.put(PATH, json={"token": T, "chat_id": bad})
            assert denied.status_code == 422
            assert denied.json()["fields"] == ["body.chat_id"]
        assert await stored(deps, tenant) == (T, "-1001234567890")
        await notifier_of(deps).drain()


async def test_chat_id_only_keeps_token(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т19: без токена меняется только chat id; без настроек - 422 `telegram_token_required`."""
    bare = await signed_in(deps, "b@example.com", ip="10.0.0.2")
    missing = await bare.put(PATH, json={"chat_id": "44"})
    assert missing.status_code == 422
    assert missing.json()["code"] == "telegram_token_required"
    assert missing.json()["fields"] == ["body.token"]

    client, _ = await configured(deps, raw_engine)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(URL).respond(200)
        assert (await client.put(PATH, json={"chat_id": "44"})).status_code == 200
        await notifier_of(deps).drain()
        mock.reset()
        assert (await client.post(f"{PATH}/test")).status_code == 204
        assert sent_messages(mock) == [Sent(T, "44", text_test())]


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"token": "123/abc", "chat_id": "1"}, ["body.token"]),
        ({"token": "123456789:AAAA?x=1", "chat_id": "1"}, ["body.token"]),
        ({"token": T + "/../x", "chat_id": "1"}, ["body.token"]),
        ({"token": "bot123:abc", "chat_id": "1"}, ["body.token"]),
        # граница длины секретной части: 29 знаков - мало, 65 - много
        ({"token": "123456789:" + "A" * 29, "chat_id": "1"}, ["body.token"]),
        ({"token": "123456789:" + "A" * 65, "chat_id": "1"}, ["body.token"]),
        ({"token": T, "chat_id": "@channel"}, ["body.chat_id"]),
        ({"token": T, "chat_id": "12a"}, ["body.chat_id"]),
        ({"token": T, "chat_id": "1", "extra": 1}, ["body.extra"]),
    ],
)
async def test_input_validation(
    deps: ApiDeps, raw_engine: AsyncEngine, body: dict[str, Any], fields: list[str]
) -> None:
    """Т20: 422 без значений ввода, строка не создана."""
    client = await signed_in(deps)
    response = await client.put(PATH, json=body)
    assert response.status_code == 422
    assert response.json()["fields"] == fields
    for value in body.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in response.text
    assert (await client.get(PATH)).json() == {"token_set": False, "chat_id": None}


async def test_delete(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т21: без настроек 204; с настройками нужен пароль."""
    bare = await signed_in(deps, "b@example.com", ip="10.0.0.2")
    assert (await bare.delete(PATH)).status_code == 204

    client = await signed_in(deps)
    tenant = TenantId(await tenant_of(raw_engine, "a@example.com"))
    async with deps.database.tenant(tenant) as scope:
        await scope.telegram.save(chat_id="42", token=Secret(T))
        await scope.commit()
    with respx.mock(assert_all_called=False):
        denied = await client.delete(PATH)
        assert (denied.status_code, denied.json()["code"]) == (403, "password_required")
        assert await stored(deps, tenant) == (T, "42")
        await confirm_password(client)
        assert (await client.delete(PATH)).status_code == 204
        await notifier_of(deps).drain()
    assert (await client.get(PATH)).json()["token_set"] is False


@pytest.mark.parametrize(
    ("outcome", "status", "code"),
    [
        (401, 409, "telegram_token_invalid"),
        (400, 409, "telegram_chat_invalid"),
        (403, 409, "telegram_chat_invalid"),
        (500, 502, "telegram_unavailable"),
        (httpx.ConnectError("x"), 502, "telegram_unavailable"),
        (httpx.ReadTimeout("x"), 502, "telegram_unavailable"),
    ],
)
async def test_check_errors(
    deps: ApiDeps,
    raw_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    outcome: Any,
    status: int,
    code: str,
) -> None:
    """Т22: ошибки проверки; сессия жива; токена нет ни в ответе, ни в журнале."""
    client, _ = await configured(deps, raw_engine)
    caplog.set_level(logging.DEBUG)
    with respx.mock() as mock:
        route = mock.post(URL)
        if isinstance(outcome, Exception):
            route.mock(side_effect=outcome)
        else:
            route.respond(outcome)
        response = await client.post(f"{PATH}/test")
    assert (response.status_code, response.json()["code"]) == (status, code)
    assert T not in response.text
    assert T not in caplog.text
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_check_success_and_not_configured(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т22: успех шлёт `text_test()` на chat id настроек; без настроек - 409."""
    bare = await signed_in(deps, "b@example.com", ip="10.0.0.2")
    with respx.mock(assert_all_called=False) as mock:
        mock.post(URL).respond(200)
        response = await bare.post(f"{PATH}/test")
        assert (response.status_code, response.json()["code"]) == (409, "telegram_not_configured")
        assert sent_messages(mock) == []

        client, _ = await configured(deps, raw_engine)
        ok = await client.post(f"{PATH}/test")
        assert ok.status_code == 204
        assert sent_messages(mock) == [Sent(T, "42", text_test())]


async def test_check_unreadable_token(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т22: ключ вне связки - 409 `telegram_token_unreadable`."""
    client, tenant = await configured(deps, raw_engine)
    foreign = SecretCipher(KeyRing(9, {9: os.urandom(32)})).encrypt(
        Secret(T), SecretContext(tenant, NO_SHOP, SecretField.TELEGRAM_TOKEN)
    )
    await sql_exec(
        raw_engine,
        "UPDATE telegram_settings SET token_enc = :b WHERE tenant_id = :t",
        b=foreign,
        t=tenant,
    )
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(URL).respond(200)
        response = await client.post(f"{PATH}/test")
    assert (response.status_code, response.json()["code"]) == (409, "telegram_token_unreadable")
    assert route.call_count == 0


async def test_tenant_isolation(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т24: настройки арендатора A не видны, не проверяются и не удаляются арендатором B."""
    a, tenant_a = await configured(deps, raw_engine)
    b = await signed_in(deps, "b@example.com", ip="10.0.0.2")
    with respx.mock(assert_all_called=False) as mock:
        mock.post(URL).respond(200)
        assert (await b.get(PATH)).json()["token_set"] is False
        denied = await b.post(f"{PATH}/test")
        assert (denied.status_code, denied.json()["code"]) == (409, "telegram_not_configured")
        assert (await b.delete(PATH)).status_code == 204
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []
    assert await stored(deps, tenant_a) == (T, "42")
    assert (await a.get(PATH)).json() == {"token_set": True, "chat_id": "42"}


async def test_message_to_old_chat(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т51: смена и отключение шлют сообщение в прежний чат; первая настройка - нет."""
    fresh = await signed_in(deps, "b@example.com", ip="10.0.0.2")
    with respx.mock(assert_all_called=False) as mock:
        mock.post(URL).respond(200)
        mock.post(URL2).respond(200)
        assert (await fresh.put(PATH, json={"token": T, "chat_id": "42"})).status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []

        client, _ = await configured(deps, raw_engine)
        mock.reset()
        response = await client.put(PATH, json={"token": T2, "chat_id": "43"})
        assert response.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == [Sent(T, "42", text_settings_changed())]

        mock.reset()
        again = await client.put(PATH, json={"chat_id": "44"})  # теперь прежний - (T2, "43")
        assert again.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == [Sent(T2, "43", text_settings_changed())]

        mock.reset()
        assert (await client.delete(PATH)).status_code == 204
        await notifier_of(deps).drain()
        assert sent_messages(mock) == [Sent(T2, "44", text_settings_deleted())]


async def test_no_message_when_old_token_unreadable(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т51: прежний токен нечитаем - PUT проходит, сообщений нет."""
    client, tenant = await configured(deps, raw_engine)
    foreign = SecretCipher(KeyRing(9, {9: os.urandom(32)})).encrypt(
        Secret(T), SecretContext(tenant, NO_SHOP, SecretField.TELEGRAM_TOKEN)
    )
    await sql_exec(
        raw_engine,
        "UPDATE telegram_settings SET token_enc = :b WHERE tenant_id = :t",
        b=foreign,
        t=tenant,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post(URL2).respond(200)
        response = await client.put(PATH, json={"token": T2, "chat_id": "43"})
        assert response.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []
    assert await stored(deps, tenant) == (T2, "43")


def test_user_event_texts_are_clean_and_have_no_token() -> None:
    """Т25: адрес очищается и обрезается до 100 символов; в тексте есть путь к отзыву сессий."""
    from kaspisa.api.telegram import _NOT_YOU_SESSIONS, text_user_added, text_user_removed

    messy = "a\n  b\x00c@example.com"
    long_email = "x" * 150 + "@example.com"
    for build, phrase in (
        (text_user_added, "добавлен пользователь"),
        (text_user_removed, "удалён пользователь"),
    ):
        text = build(messy)
        assert "a b c@example.com" in text
        assert "\n" not in text
        assert phrase in text
        assert _NOT_YOU_SESSIONS in text
        cut = build(long_email)
        assert "x" * 99 + "…" in cut
        assert "x" * 100 not in cut
