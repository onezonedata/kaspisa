"""ADR 0025, Т25-Т30, Т32-Т34, Т47-Т50, Т53: события, доставка с повтором, фоновый цикл."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from api_notify_testing import (
    GITHUB,
    T2,
    Sent,
    T,
    clock_of,
    halt,
    notifier_of,
    owner,
    save_telegram,
    sent_messages,
    shop_of,
    url_of,
)
from api_testing import (
    KASPI_COOKIE,
    KASPI_LOGIN,
    KASPI_PASSWORD,
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    confirm_password,
    deps,
    make_api,
    raw_engine,
    read_shop,
    redis_client,
    redis_prefix,
    seed_product,
    signed_in,
    sql_exec,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps, notifications, telegram
from kaspisa.api.notifications import (
    background_notifications,
    notification_loop,
    notify_halts,
    notify_new_version,
)
from kaspisa.api.reset_token import issue_reset_token
from kaspisa.api.telegram import (
    text_halted,
    text_live,
    text_min_lowered,
    text_new_device,
    text_new_version,
)
from kaspisa.api.version import ReleaseCheck
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

LOGIN = "/api/auth/login"
CRED = {"email": "a@example.com", "password": SERVICE_PASSWORD}


def device_cookie(response: httpx.Response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("kaspisa_device="):
            return header
    return None


def device_value(response: httpx.Response) -> str:
    header = device_cookie(response)
    assert header is not None
    return header.split(";", 1)[0].removeprefix("kaspisa_device=")


async def log_in(
    deps: ApiDeps, ip: str = "10.1.2.3", *, ua: str | None = "Test-UA", **extra: Any
) -> httpx.Response:
    """Вход новым клиентом без cookie устройства."""
    client = make_api(deps, ip=ip)
    headers = {"User-Agent": ua} if ua is not None else {}
    return await client.post(LOGIN, json=extra.pop("json", CRED), headers=headers, **extra)


# --- Т25. Вход с нового устройства ---


async def test_new_device_login(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    await owner(deps, raw_engine)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        client = make_api(deps, ip="10.1.2.3")
        first = await client.post(LOGIN, json=CRED, headers={"User-Agent": "Test-UA"})
        assert first.status_code == 200
        header = device_cookie(first)
        assert header is not None
        lowered = header.lower()
        for part in ("httponly", "path=/api/auth", "max-age=34560000", "samesite=lax"):
            assert part in lowered
        await notifier_of(deps).drain()
        assert sent_messages(mock) == [Sent(T, "42", text_new_device("10.1.2.3", "Test-UA"))]

        mock.reset()
        again = await client.post(LOGIN, json=CRED, headers={"User-Agent": "Test-UA"})
        assert again.status_code == 200
        assert device_cookie(again) is not None  # cookie выдана заново
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


async def test_new_device_other_user_cookie_and_wrong_password(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    await owner(deps, raw_engine)
    await owner(deps, raw_engine, "b@example.com", ip="10.0.0.2", token=T2, chat_id="99")
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        mock.post(url_of(T2)).respond(200)
        first = await log_in(deps)
        await notifier_of(deps).drain()
        mock.reset()
        # Cookie пользователя A у пользователя B: устройство для B новое.
        cookie = device_value(first)
        client = make_api(deps, ip="10.1.2.4")
        response = await client.post(
            LOGIN,
            json={"email": "b@example.com", "password": SERVICE_PASSWORD},
            headers={"Cookie": f"kaspisa_device={cookie}", "User-Agent": "UA-B"},
        )
        assert response.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == [Sent(T2, "99", text_new_device("10.1.2.4", "UA-B"))]

        mock.reset()
        wrong = await log_in(deps, json={"email": "a@example.com", "password": "x" * 14})
        assert wrong.status_code == 401
        assert device_cookie(wrong) is None
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


async def test_registration_makes_device_known_without_message(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        client = make_api(deps, ip="10.1.2.5")
        registered = await client.post(
            "/api/auth/register", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
        )
        assert registered.status_code == 201
        assert device_cookie(registered) is not None
        tenant = TenantId(await tenant_of(raw_engine, "a@example.com"))
        await save_telegram(deps, tenant)
        await notifier_of(deps).drain()
        response = await client.post(LOGIN, json=CRED)
        assert response.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


async def test_long_device_cookie_is_ignored_and_user_agent_is_cleaned(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    await owner(deps, raw_engine)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        client = make_api(deps, ip="10.1.2.3")
        response = await client.post(
            LOGIN,
            json=CRED,
            headers={"Cookie": "kaspisa_device=" + "x" * 200, "User-Agent": "A\nB" * 100},
        )
        assert response.status_code == 200
        await notifier_of(deps).drain()
        browser = ("AB" * 100)[:120]
        assert sent_messages(mock) == [Sent(T, "42", text_new_device("10.1.2.3", browser))]

        mock.reset()
        no_agent = make_api(deps, ip="10.1.2.3")
        await no_agent.post(LOGIN, json=CRED, headers={"User-Agent": ""})
        await notifier_of(deps).drain()
        assert sent_messages(mock) == [Sent(T, "42", text_new_device("10.1.2.3", "неизвестен"))]


# --- Т26. Опасная смена границ ---


async def _product(
    deps: ApiDeps, raw: AsyncEngine, tenant: TenantId, shop: uuid.UUID, sku: str, **kwargs: Any
) -> int:
    product = await seed_product(raw, tenant, shop, sku=sku, **kwargs)
    anchor = kwargs.get("min_price")
    await sql_exec(
        raw, "UPDATE product SET min_price_anchor = :a WHERE id = :i", a=anchor, i=product
    )
    return product


async def test_min_lowered_notification(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant = await owner(deps, raw_engine)
    shop = await shop_of(deps, raw_engine, tenant)
    path = f"/api/shops/{shop}/products"
    bounds = {"min_price": 15000, "max_price": 30000}
    first = await _product(deps, raw_engine, tenant, shop, "SKU-1", **bounds)
    second = await _product(deps, raw_engine, tenant, shop, "SKU-2", **bounds)
    bare = await _product(deps, raw_engine, tenant, shop, "SKU-3")
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        denied = await client.patch(f"{path}/{first}", json={"min_price": 11000})
        assert (denied.status_code, denied.json()["code"]) == (403, "password_required")
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []

        await confirm_password(client)
        ok = await client.patch(f"{path}/{first}", json={"min_price": 11000})
        assert ok.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == [
            Sent(T, "42", text_min_lowered("30001", "SKU-1", 15000, 11000))
        ]

        mock.reset()
        mild = await client.patch(f"{path}/{second}", json={"min_price": 13000})
        assert mild.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []

        set_new = await client.patch(f"{path}/{bare}", json={"min_price": 500})
        assert set_new.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == [Sent(T, "42", text_min_lowered("30001", "SKU-3", None, 500))]


async def test_dangerous_bulk_edit_does_not_notify(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant = await owner(deps, raw_engine, confirmed=True)
    shop = await shop_of(deps, raw_engine, tenant, bot_mode="live", city_id="710000000")
    product = await _product(deps, raw_engine, tenant, shop, "SKU-1", min_price=100, max_price=900)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        response = await client.patch(
            f"/api/shops/{shop}/products",
            json={"product_ids": [product], "changes": {"tracked": True}},
        )
        assert response.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


# --- Т27. Включение live ---


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("off", "live", True),
        ("dry_run", "live", True),
        ("live", "live", False),
        ("off", "dry_run", False),
        ("live", "dry_run", False),
        ("live", "off", False),
    ],
)
async def test_live_notification(
    deps: ApiDeps, raw_engine: AsyncEngine, old: str, new: str, expected: bool
) -> None:
    client, tenant = await owner(deps, raw_engine)
    shop = await shop_of(deps, raw_engine, tenant, bot_mode=old, city_id="710000000")
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        response = await client.patch(f"/api/shops/{shop}/settings", json={"bot_mode": new})
        assert response.status_code == 200
        await notifier_of(deps).drain()
        assert sent_messages(mock) == ([Sent(T, "42", text_live("30001"))] if expected else [])


async def test_live_without_city_is_rejected_silently(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant = await owner(deps, raw_engine)
    shop = await shop_of(deps, raw_engine, tenant, bot_mode="off", city_id=None)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        response = await client.patch(f"/api/shops/{shop}/settings", json={"bot_mode": "live"})
        assert response.status_code == 422
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


# --- Т28. Остановка ---


async def test_halt_notification_happy_path(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    _, tenant = await owner(deps, raw_engine)
    shop = await shop_of(deps, raw_engine, tenant)
    now = clock_of(deps).now
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        await halt(deps, tenant, shop, "login_errors", now)
        await notify_halts(deps)
        assert sent_messages(mock) == [Sent(T, "42", text_halted("30001", "login_errors"))]
        await notify_halts(deps)
        assert len(sent_messages(mock)) == 1

        async with deps.database.tenant(tenant) as scope:
            await scope.shops.clear_halt(shop)
            await scope.shops.record_write_outcome(
                shop, ok=False, limit=1, now=now + timedelta(seconds=1)
            )
            await scope.commit()
        await notify_halts(deps)
        assert sent_messages(mock)[1:] == [Sent(T, "42", text_halted("30001", "write_errors"))]


async def test_halt_without_settings_is_marked_and_not_sent_later(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    await signed_in(deps)
    tenant = TenantId(await tenant_of(raw_engine, "a@example.com"))
    shop = await shop_of(deps, raw_engine, tenant)
    await halt(deps, tenant, shop, "login_errors", clock_of(deps).now)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        await notify_halts(deps)
        assert route.call_count == 0
        assert (await read_shop(raw_engine, shop))["halt_notified_at"] is not None
        await save_telegram(deps, tenant)
        await notify_halts(deps)
        assert route.call_count == 0


async def test_halt_cleared_before_poll_is_not_sent(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    _, tenant = await owner(deps, raw_engine)
    shop = await shop_of(deps, raw_engine, tenant)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        await halt(deps, tenant, shop, "login_errors", clock_of(deps).now)
        async with deps.database.tenant(tenant) as scope:
            await scope.shops.clear_halt(shop)
            await scope.commit()
        await notify_halts(deps)
        assert route.call_count == 0


# --- Т29. Новая версия ---


def _checked(deps: ApiDeps, repo: str = "o/r") -> tuple[ApiDeps, list[float]]:
    now = [0.0]
    check = ReleaseCheck(repo, "1.1.0", clock=lambda: now[0])
    return dataclasses.replace(deps, release_check=check), now


async def test_new_version_notification(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    await owner(deps, raw_engine)
    custom, now = _checked(deps)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        github = mock.get(GITHUB).respond(json={"tag_name": "v1.2.0"})
        await notify_new_version(custom)
        url = "https://github.com/o/r/releases/tag/v1.2.0"
        assert sent_messages(mock) == [Sent(T, "42", text_new_version("1.1.0", "1.2.0", url))]
        await notify_new_version(custom)
        assert len(sent_messages(mock)) == 1

        now[0] += 86_400
        github.respond(json={"tag_name": "v1.3.0"})
        await notify_new_version(custom)
        url3 = "https://github.com/o/r/releases/tag/v1.3.0"
        assert sent_messages(mock)[1:] == [Sent(T, "42", text_new_version("1.1.0", "1.3.0", url3))]


@pytest.mark.parametrize("case", ["same", "no_repo", "github_500"])
async def test_no_message_without_newer_release(
    deps: ApiDeps, raw_engine: AsyncEngine, case: str
) -> None:
    await owner(deps, raw_engine)
    custom, _ = _checked(deps, "" if case == "no_repo" else "o/r")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        if case == "github_500":
            mock.get(GITHUB).respond(500)
        else:
            mock.get(GITHUB).respond(json={"tag_name": "v1.1.0"})
        await notify_new_version(custom)
        assert route.call_count == 0


# --- Т30. Не блокирует API; сбой не меняет ответ ---


async def test_notification_does_not_block_requests(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant = await owner(deps, raw_engine)
    shop = await shop_of(deps, raw_engine, tenant, bot_mode="off", city_id="710000000")
    release = asyncio.Event()
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).mock(side_effect=hang)
        login = await asyncio.wait_for(log_in(deps), timeout=1)
        assert login.status_code == 200
        patched = await asyncio.wait_for(
            client.patch(f"/api/shops/{shop}/settings", json={"bot_mode": "live"}), timeout=1
        )
        assert patched.status_code == 200
        await asyncio.wait_for(started.wait(), timeout=5)
        assert notifier_of(deps)._tasks
        release.set()
        await notifier_of(deps).drain()
        assert not notifier_of(deps)._tasks


async def test_telegram_failure_does_not_change_response(
    deps: ApiDeps, raw_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await owner(deps, raw_engine)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(500)
        response = await log_in(deps)
        assert response.status_code == 200
        assert device_cookie(response) is not None
        await notifier_of(deps).drain()
        assert route.call_count == 1
    assert "telegram_send_failed: unavailable" in caplog.text
    assert T not in caplog.text


# --- Т32. Секреты не попадают в сообщения ---


async def test_messages_have_no_secrets(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant = await owner(deps, raw_engine, confirmed=True)
    shop = await shop_of(
        deps,
        raw_engine,
        tenant,
        bot_mode="off",
        city_id="710000000",
        login=KASPI_LOGIN,
        password=KASPI_PASSWORD,
    )
    bounds = {"min_price": 15000, "max_price": 30000}
    product = await _product(deps, raw_engine, tenant, shop, "SKU-1", **bounds)
    custom, _ = _checked(deps)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        mock.get(GITHUB).respond(json={"tag_name": "v1.2.0"})
        logged = await log_in(deps)
        session_cookie = client.cookies.get("kaspisa_session") or ""
        await client.patch(f"/api/shops/{shop}/products/{product}", json={"min_price": 11000})
        await client.patch(f"/api/shops/{shop}/settings", json={"bot_mode": "live"})
        await halt(deps, tenant, shop, "write_errors", clock_of(deps).now)
        await notify_halts(custom)
        await notify_new_version(custom)
        await notifier_of(deps).drain()
        messages = sent_messages(mock)
        bodies = " ".join(str(call.request.content) for call in mock.calls)
    assert len(messages) == 5
    for secret in (
        T,
        T2,
        KASPI_LOGIN,
        KASPI_PASSWORD,
        KASPI_COOKIE,
        SERVICE_PASSWORD,
        device_value(logged),
        session_cookie,
    ):
        assert secret
        assert secret not in bodies


# --- Т33-Т34. Цикл ---


async def test_loop_runs_both_steps_and_survives_failures(
    deps: ApiDeps, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"halts": 0, "version": 0, "resets": 0}

    async def halts(_: ApiDeps) -> None:
        calls["halts"] += 1

    async def version(_: ApiDeps) -> None:
        calls["version"] += 1

    async def resets(_: ApiDeps) -> None:
        calls["resets"] += 1

    monkeypatch.setattr(notifications, "notify_halts", halts)
    monkeypatch.setattr(notifications, "notify_new_version", version)
    monkeypatch.setattr(notifications, "notify_stock_resets", resets)
    stop = asyncio.Event()
    task = asyncio.create_task(notification_loop(deps, stop, halt_every=0.01, version_every=3600))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert calls["halts"] >= 2
    assert calls["resets"] >= 2
    assert calls["version"] == 1

    async def failing(_: ApiDeps) -> None:
        calls["halts"] += 1
        raise RuntimeError("secret")

    monkeypatch.setattr(notifications, "notify_halts", failing)
    caplog.set_level(logging.DEBUG)
    before = calls["halts"]
    before_resets = calls["resets"]
    stop = asyncio.Event()
    task = asyncio.create_task(notification_loop(deps, stop, halt_every=0.01, version_every=3600))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert calls["halts"] - before >= 2
    assert calls["resets"] - before_resets >= 2  # сбой остановок не мешает шагу сбросов
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


async def test_background_notifications_cleans_up(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т34 (дельта 1): выход не дольше `DRAIN_SECONDS + 0.5`; прерванная отправка без отметки."""
    _, tenant = await owner(deps, raw_engine)
    shop = await _halted_shop(deps, raw_engine, tenant)
    # Фоновая задача ждёт DRAIN_SECONDS один раз; прежнее ожидание цикла перед этим удвоило бы срок.
    monkeypatch.setattr(telegram, "DRAIN_SECONDS", 1.0)
    never = asyncio.Event()
    started = 0

    async def hang(request: httpx.Request) -> httpx.Response:
        nonlocal started
        started += 1
        await never.wait()
        return httpx.Response(200)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).mock(side_effect=hang)
        began = 0.0
        async with background_notifications(deps):
            notifier_of(deps).notify(tenant, "x")
            for _ in range(500):  # ждём отправку цикла и фоновую задачу
                if started >= 2:
                    break
                await asyncio.sleep(0.01)
            assert started == 2
            loops = [t for t in asyncio.all_tasks() if "notification_loop" in repr(t)]
            assert loops
            began = asyncio.get_running_loop().time()
        spent = asyncio.get_running_loop().time() - began
        assert spent <= 1.0 + 0.5
        assert all(t.done() for t in loops)
    assert not notifier_of(deps)._tasks
    assert notifier_of(deps)._client.is_closed
    assert (await read_shop(raw_engine, shop))["halt_notified_at"] is None


# --- Т54-Т57. Известные устройства (дельта 1) ---


async def _login_json(
    deps: ApiDeps, *, device: str | None = None, password: str = SERVICE_PASSWORD
) -> httpx.Response:
    headers = {"User-Agent": "UA"}
    if device is not None:
        headers["Cookie"] = f"kaspisa_device={device}"
    client = make_api(deps, ip="10.1.2.3")
    return await client.post(
        LOGIN, json={"email": "a@example.com", "password": password}, headers=headers
    )


async def _known_device(
    deps: ApiDeps, mock: respx.MockRouter
) -> tuple[str, uuid.UUID, httpx.AsyncClient]:
    """Вход без cookie (сообщение), вход с cookie (тихо); возвращает cookie и клиент с сессией."""
    client = make_api(deps, ip="10.1.2.3")
    first = await client.post(LOGIN, json=CRED, headers={"User-Agent": "UA"})
    await notifier_of(deps).drain()
    assert len(sent_messages(mock)) == 1
    device = device_value(first)
    again = await _login_json(deps, device=device)
    await notifier_of(deps).drain()
    assert len(sent_messages(mock)) == 1
    return device, uuid.UUID(again.json()["id"]), client


def _devices_key(deps: ApiDeps, user_id: uuid.UUID) -> str:
    return f"{deps.state._prefix}:devices:{user_id}"


async def test_logout_all_forgets_devices(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    await owner(deps, raw_engine)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        device, user_id, client = await _known_device(deps, mock)
        assert await deps.redis.exists(_devices_key(deps, user_id))
        assert (await client.post("/api/auth/logout-all")).status_code == 204
        assert not await deps.redis.exists(_devices_key(deps, user_id))
        mock.reset()
        again = await _login_json(deps, device=device)
        await notifier_of(deps).drain()
        assert [m.text for m in sent_messages(mock)] == [text_new_device("10.1.2.3", "UA")]
        fresh = device_value(again)
        assert fresh != device  # прежняя cookie не становится известной
        mock.reset()
        await _login_json(deps, device=device)  # D по-прежнему неизвестна
        await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 1
        mock.reset()
        await _login_json(deps, device=fresh)
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


async def test_password_reset_forgets_devices(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    await owner(deps, raw_engine)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        device, user_id, _ = await _known_device(deps, mock)
        token = await issue_reset_token(deps, "a@example.com")
        assert token is not None
        new_password = "service-pass-new-7f3a"  # noqa: S105
        reset = await make_api(deps).post(
            "/api/auth/password-reset", json={"token": token, "new_password": new_password}
        )
        assert reset.status_code == 204
        assert not await deps.redis.exists(_devices_key(deps, user_id))
        mock.reset()
        client = make_api(deps, ip="10.1.2.3")
        entered = await client.post(
            LOGIN,
            json={"email": "a@example.com", "password": new_password},
            headers={"Cookie": f"kaspisa_device={device}", "User-Agent": "UA"},
        )
        assert entered.status_code == 200
        await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 1
        device = device_value(entered)  # выдана новая cookie, прежняя неизвестна
        # Выход одной сессии устройства не забывает.
        assert (await client.post("/api/auth/logout")).status_code == 204
        assert await deps.redis.exists(_devices_key(deps, user_id))
        mock.reset()
        await _login_json(deps, device=device, password=new_password)
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


async def test_device_list_is_limited_to_20(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    await owner(deps, raw_engine)
    clock = clock_of(deps)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        cookies: list[str] = []
        user_id = uuid.uuid4()
        for _ in range(21):
            clock.advance(1)
            response = await _login_json(deps)
            user_id = uuid.UUID(response.json()["id"])
            cookies.append(device_value(response))
            await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 21
        key = _devices_key(deps, user_id)
        assert await deps.redis.zcard(key) == 20

        mock.reset()
        clock.advance(1)
        await _login_json(deps, device=cookies[20])
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []

        clock.advance(1)
        await _login_json(deps, device=cookies[0])  # вытеснено
        await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 1
        assert await deps.redis.zcard(key) == 20

        mock.reset()
        clock.advance(1)
        await _login_json(deps, device=cookies[1])  # теперь вытеснено самое старое
        await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 1


async def test_each_device_expires_after_400_days(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    await owner(deps, raw_engine)
    clock = clock_of(deps)
    ttl = 34_560_000
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        a = device_value(await _login_json(deps))
        await notifier_of(deps).drain()
        clock.advance(ttl - 1)
        b = device_value(await _login_json(deps))
        await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 2

        mock.reset()
        clock.advance(1)  # ровно 400 дней от входа с A
        await _login_json(deps, device=a)
        await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 1
        mock.reset()
        await _login_json(deps, device=b)
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


async def test_expired_devices_are_dropped_from_the_list(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Просроченные устройства не копятся в списке: следующий вход их удаляет."""
    await owner(deps, raw_engine)
    clock = clock_of(deps)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        for _ in range(3):
            response = await _login_json(deps)
            clock.advance(1)
        user_id = uuid.UUID(response.json()["id"])
        key = _devices_key(deps, user_id)
        assert await deps.redis.zcard(key) >= 3  # и вход при создании владельца
        clock.advance(34_560_000)
        await _login_json(deps)
        assert await deps.redis.zcard(key) == 1


async def test_unknown_device_cookie_is_replaced(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Н-4: неизвестная cookie не сохраняется: выдаётся новая, чужая остаётся неизвестной."""
    await owner(deps, raw_engine)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        first = await _login_json(deps, device="foreign-device-cookie")
        issued = device_value(first)
        assert issued != "foreign-device-cookie"
        await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 1
        mock.reset()
        await _login_json(deps, device="foreign-device-cookie")
        await notifier_of(deps).drain()
        assert len(sent_messages(mock)) == 1
        mock.reset()
        await _login_json(deps, device=issued)
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []


async def test_old_chat_failure_logs_tenant(
    deps: ApiDeps, raw_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Ревью №3: сбой отправки в прежний чат и переполнение очереди называют арендатора."""
    client, tenant = await owner(deps, raw_engine, confirmed=True)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(500)
        response = await client.put(
            "/api/notifications/telegram", json={"token": T2, "chat_id": "43"}
        )
        assert response.status_code == 200
        await notifier_of(deps).drain()
    assert f"telegram_send_failed: unavailable tenant={tenant}" in caplog.text
    assert T not in caplog.text

    # Удаление настроек: сбой отправки в прежний чат тоже называет арендатора.
    caplog.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T2)).respond(500)
        assert (await client.delete("/api/notifications/telegram")).status_code == 204
        await notifier_of(deps).drain()
    assert f"telegram_send_failed: unavailable tenant={tenant}" in caplog.text

    from kaspisa.db import TelegramTarget

    caplog.clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(telegram, "MAX_PENDING", 0)
        notifier_of(deps).notify_target(TelegramTarget(Secret(T), "42"), "x", tenant_id=tenant)
    assert f"telegram_queue_full tenant={tenant}" in caplog.text


# --- Т47-Т50, Т53. Повтор ---


async def _halted_shop(
    deps: ApiDeps, raw: AsyncEngine, tenant: TenantId, uid: str = "30001", age: float = 10
) -> uuid.UUID:
    shop = await shop_of(deps, raw, tenant, uid)
    await halt(deps, tenant, shop, "write_errors", clock_of(deps).now - timedelta(seconds=age))
    return shop


@pytest.mark.parametrize("first", ["500", "timeout"])
async def test_halt_is_retried_until_delivered(
    deps: ApiDeps, raw_engine: AsyncEngine, first: str
) -> None:
    _, tenant = await owner(deps, raw_engine)
    shop = await _halted_shop(deps, raw_engine, tenant)
    failure = httpx.Response(500) if first == "500" else httpx.ReadTimeout("x")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).mock(side_effect=[failure, httpx.Response(200)])
        await notify_halts(deps)
        assert route.call_count == 1
        assert (await read_shop(raw_engine, shop))["halt_notified_at"] is None
        await notify_halts(deps)
        assert route.call_count == 2
        assert (await read_shop(raw_engine, shop))["halt_notified_at"] is not None
        await notify_halts(deps)
        assert route.call_count == 2


@pytest.mark.parametrize("status", [401, 403, 400, 404])
async def test_halt_final_failure_marks(
    deps: ApiDeps, raw_engine: AsyncEngine, status: int
) -> None:
    _, tenant = await owner(deps, raw_engine)
    shop = await _halted_shop(deps, raw_engine, tenant)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(status)
        await notify_halts(deps)
        assert route.call_count == 1
        assert (await read_shop(raw_engine, shop))["halt_notified_at"] is not None
        await notify_halts(deps)
        assert route.call_count == 1


async def test_halt_with_unreadable_token_marks_without_request(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    _, tenant = await owner(deps, raw_engine)
    shop = await _halted_shop(deps, raw_engine, tenant)
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
        route = mock.post(url_of(T)).respond(200)
        await notify_halts(deps)
        assert route.call_count == 0
        assert (await read_shop(raw_engine, shop))["halt_notified_at"] is not None


async def test_halt_retry_expires_after_24_hours(
    deps: ApiDeps, raw_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    _, tenant = await owner(deps, raw_engine)
    old = await _halted_shop(deps, raw_engine, tenant, "30001", age=24 * 3600 + 1)
    fresh = await _halted_shop(deps, raw_engine, tenant, "30002", age=23 * 3600)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        await notify_halts(deps)
        assert [m.text for m in sent_messages(mock)] == [text_halted("30002", "write_errors")]
        assert route.call_count == 1
    assert (await read_shop(raw_engine, old))["halt_notified_at"] is not None
    assert (await read_shop(raw_engine, fresh))["halt_notified_at"] is not None
    assert "telegram_halt_expired" in caplog.text


async def test_version_is_retried_until_delivered(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    _, tenant = await owner(deps, raw_engine)
    custom, _ = _checked(deps)

    async def noted() -> str | None:
        async with deps.database.tenant(tenant) as scope:
            row = await scope.telegram.get()
        return None if row is None else row.notified_version

    with respx.mock(assert_all_called=False) as mock:
        mock.get(GITHUB).respond(json={"tag_name": "v1.2.0"})
        route = mock.post(url_of(T)).mock(side_effect=[httpx.Response(502), httpx.Response(200)])
        await notify_new_version(custom)
        assert route.call_count == 1
        assert await noted() is None
        await notify_new_version(custom)
        assert route.call_count == 2
        assert await noted() == "1.2.0"
        await notify_new_version(custom)
        assert route.call_count == 2

    # 401 - окончательный исход: отметка с первого раза.
    other_custom, _ = _checked(deps)
    await sql_exec(raw_engine, "UPDATE telegram_settings SET notified_version = NULL")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(GITHUB).respond(json={"tag_name": "v1.2.0"})
        route = mock.post(url_of(T)).respond(401)
        await notify_new_version(other_custom)
        assert route.call_count == 1
        assert await noted() == "1.2.0"


async def test_failed_tenant_is_skipped_for_the_pass(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    _, tenant_a = await owner(deps, raw_engine)
    _, tenant_b = await owner(deps, raw_engine, "b@example.com", ip="10.0.0.2", token=T2)
    a1 = await _halted_shop(deps, raw_engine, tenant_a, "30001", age=60)
    a2 = await _halted_shop(deps, raw_engine, tenant_a, "30002", age=50)
    b1 = await _halted_shop(deps, raw_engine, tenant_b, "30003", age=40)
    with respx.mock(assert_all_called=False) as mock:
        route_a = mock.post(url_of(T)).respond(500)
        route_b = mock.post(url_of(T2)).respond(200)
        await notify_halts(deps)
        assert (route_a.call_count, route_b.call_count) == (1, 1)
        assert (await read_shop(raw_engine, a1))["halt_notified_at"] is None
        assert (await read_shop(raw_engine, a2))["halt_notified_at"] is None
        assert (await read_shop(raw_engine, b1))["halt_notified_at"] is not None

        route_a.respond(200)
        await notify_halts(deps)
        assert route_a.call_count == 1 + 2
        assert (await read_shop(raw_engine, a1))["halt_notified_at"] is not None
        assert (await read_shop(raw_engine, a2))["halt_notified_at"] is not None
