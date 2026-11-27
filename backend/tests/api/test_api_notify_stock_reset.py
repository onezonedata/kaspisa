"""Т10-Т22, Т24 этапа 24: уведомление в Telegram о сбросе остатка (ADR 0038)."""

from __future__ import annotations

import dataclasses
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from api_notify_testing import (
    T2,
    Sent,
    T,
    clock_of,
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
    api_cipher,
    api_database_url,
    deps,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_product,
    signed_in,
    sql_exec,
    tenant_of,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api.notifications import notify_stock_resets
from kaspisa.api.telegram import text_stock_reset
from kaspisa.db import NO_SHOP, TenantId
from kaspisa.security import KeyRing, Secret, SecretCipher, SecretContext, SecretField
from kaspisa.worker.confirm import untrack_stock_reset

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]


async def _reset(
    deps: ApiDeps,
    raw: AsyncEngine,
    tenant: TenantId,
    shop: uuid.UUID,
    *,
    title: str = "Чехол",
    points: tuple[str, ...] = ("PP1",),
    at: datetime | None = None,
    sku: str | None = None,
) -> int:
    """Товар, выключенный обработчиком из-за сброса остатка в момент `at`."""
    pid = await seed_product(raw, tenant, shop, title=title, tracked=True, sku=sku)
    async with deps.database.tenant(tenant) as scope:
        moment = at or clock_of(deps).now - timedelta(seconds=10)
        assert await untrack_stock_reset(scope, pid, points, moment)
        await scope.commit()
    return pid


async def _columns(raw: AsyncEngine, pid: int) -> tuple[Any, ...]:
    async with raw.connect() as conn:
        row = await conn.execute(
            text("SELECT stock_reset_at, stock_reset_points FROM product WHERE id = :i"),
            {"i": pid},
        )
        return tuple(row.one())


async def _setup(deps: ApiDeps, raw: AsyncEngine) -> tuple[TenantId, uuid.UUID]:
    _, tenant = await owner(deps, raw)
    return tenant, await shop_of(deps, raw, tenant)


async def test_reset_gives_one_message(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т10: сброс даёт одно сообщение, повторный проход ничего не шлёт."""
    tenant, shop = await _setup(deps, raw_engine)
    pid = await _reset(deps, raw_engine, tenant, shop)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        await notify_stock_resets(deps)
        assert sent_messages(mock) == [Sent(T, "42", text_stock_reset("Чехол", ("PP1",)))]
        assert await _columns(raw_engine, pid) == (None, None)
        await notify_stock_resets(deps)
        assert len(sent_messages(mock)) == 1


@pytest.mark.parametrize("failure", ["500", "429", "timeout"])
async def test_reset_is_retried_until_delivered(
    deps: ApiDeps, raw_engine: AsyncEngine, failure: str
) -> None:
    """Т11: временный сбой не снимает сброс, повтор доставляет."""
    tenant, shop = await _setup(deps, raw_engine)
    pid = await _reset(deps, raw_engine, tenant, shop)
    before = await _columns(raw_engine, pid)
    first: Any = {
        "500": httpx.Response(500),
        "429": httpx.Response(429),
        "timeout": httpx.ReadTimeout("x"),
    }[failure]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).mock(side_effect=[first, httpx.Response(200)])
        await notify_stock_resets(deps)
        assert route.call_count == 1
        assert await _columns(raw_engine, pid) == before
        await notify_stock_resets(deps)
        assert route.call_count == 2
        assert await _columns(raw_engine, pid) == (None, None)
        await notify_stock_resets(deps)
        assert route.call_count == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_reset_final_failure_marks(
    deps: ApiDeps, raw_engine: AsyncEngine, status: int
) -> None:
    """Т12: окончательный отказ Telegram снимает сброс."""
    tenant, shop = await _setup(deps, raw_engine)
    pid = await _reset(deps, raw_engine, tenant, shop)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(status)
        await notify_stock_resets(deps)
        assert route.call_count == 1
        assert await _columns(raw_engine, pid) == (None, None)
        await notify_stock_resets(deps)
        assert route.call_count == 1


async def test_reset_without_settings_is_marked_and_not_sent_later(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т13: без настроек Telegram отправки нет, сброс снимается и позже не приходит."""
    client = await signed_in(deps, "a@example.com")
    del client
    tenant = TenantId(await tenant_of(raw_engine, "a@example.com"))
    shop = await shop_of(deps, raw_engine, tenant)
    pid = await _reset(deps, raw_engine, tenant, shop)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        await notify_stock_resets(deps)
        assert route.call_count == 0
        assert await _columns(raw_engine, pid) == (None, None)
        await save_telegram(deps, tenant)
        await notify_stock_resets(deps)
        assert route.call_count == 0


async def test_reset_with_unreadable_token_marks_without_request(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т14: токен нечитаем: запроса нет, сброс снимается."""
    tenant, shop = await _setup(deps, raw_engine)
    pid = await _reset(deps, raw_engine, tenant, shop)
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
        await notify_stock_resets(deps)
        assert route.call_count == 0
        assert await _columns(raw_engine, pid) == (None, None)


async def test_reset_retry_expires_after_24_hours(
    deps: ApiDeps, raw_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Т15: сброс старше 24 ч снимается без отправки, свежий уходит следующим проходом."""
    tenant, shop = await _setup(deps, raw_engine)
    now = clock_of(deps).now
    old = await _reset(
        deps, raw_engine, tenant, shop, title="Старый", at=now - timedelta(hours=24, seconds=1)
    )
    fresh = await _reset(
        deps, raw_engine, tenant, shop, title="Свежий", at=now - timedelta(hours=23)
    )
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        await notify_stock_resets(deps)
        assert route.call_count == 0
        assert await _columns(raw_engine, old) == (None, None)
        assert (await _columns(raw_engine, fresh))[1] == "PP1"
        assert "telegram_stock_reset_expired" in caplog.text
        await notify_stock_resets(deps)
        assert [m.text for m in sent_messages(mock)] == [text_stock_reset("Свежий", ("PP1",))]
        assert await _columns(raw_engine, fresh) == (None, None)


async def test_retracked_product_is_not_sent(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т16: товар снова включён до прохода: сообщения нет, позднее выключение без сброса тоже."""
    tenant, shop = await _setup(deps, raw_engine)
    pid = await _reset(deps, raw_engine, tenant, shop)
    await sql_exec(raw_engine, "UPDATE product SET tracked = true WHERE id = :i", i=pid)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        await notify_stock_resets(deps)
        assert route.call_count == 0
        assert await _columns(raw_engine, pid) == (None, None)
        await sql_exec(raw_engine, "UPDATE product SET tracked = false WHERE id = :i", i=pid)
        await notify_stock_resets(deps)
        assert route.call_count == 0


async def test_retracked_old_reset_is_dropped_without_warning(
    deps: ApiDeps, raw_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Включённый товар со сбросом старше 24 ч: сброс снят, предупреждения об истечении нет."""
    tenant, shop = await _setup(deps, raw_engine)
    at = clock_of(deps).now - timedelta(hours=25)
    pid = await _reset(deps, raw_engine, tenant, shop, at=at)
    await sql_exec(raw_engine, "UPDATE product SET tracked = true WHERE id = :i", i=pid)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        await notify_stock_resets(deps)
        assert route.call_count == 0
    assert await _columns(raw_engine, pid) == (None, None)
    assert "telegram_stock_reset_expired" not in caplog.text


async def test_new_reset_after_retrack_is_sent_again(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т17: новый сброс после повторного включения даёт второе сообщение."""
    tenant, shop = await _setup(deps, raw_engine)
    t1 = clock_of(deps).now - timedelta(seconds=100)
    pid = await _reset(deps, raw_engine, tenant, shop, at=t1)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        await notify_stock_resets(deps)
        assert len(sent_messages(mock)) == 1
        await sql_exec(raw_engine, "UPDATE product SET tracked = true WHERE id = :i", i=pid)
        async with deps.database.tenant(tenant) as scope:
            assert await untrack_stock_reset(scope, pid, ("PP2",), t1 + timedelta(seconds=60))
            await scope.commit()
        await notify_stock_resets(deps)
        messages = sent_messages(mock)
        assert len(messages) == 2
        assert "склады: PP2" in messages[1].text


async def test_one_message_per_tenant_per_pass(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т18: за проход один товар на арендатора, по порядку времени сброса."""
    tenant, shop = await _setup(deps, raw_engine)
    t0 = clock_of(deps).now - timedelta(seconds=100)
    for index, title in enumerate(["А", "Б", "В"]):
        await _reset(deps, raw_engine, tenant, shop, title=title, at=t0 + timedelta(seconds=index))
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        for count in (1, 2, 3, 3):
            await notify_stock_resets(deps)
            assert len(sent_messages(mock)) == count
        assert [m.text for m in sent_messages(mock)] == [
            text_stock_reset(title, ("PP1",)) for title in ("А", "Б", "В")
        ]


async def test_failing_tenant_does_not_starve_other(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т19: сбой Telegram у A не мешает B; названия не пересекаются между арендаторами."""
    _, tenant_a = await owner(deps, raw_engine)
    _, tenant_b = await owner(deps, raw_engine, "b@example.com", ip="10.0.0.2", token=T2)
    shop_a = await shop_of(deps, raw_engine, tenant_a, "30001")
    shop_b = await shop_of(deps, raw_engine, tenant_b, "30002")
    t0 = clock_of(deps).now - timedelta(seconds=1000)
    products_a = [
        await _reset(
            deps, raw_engine, tenant_a, shop_a, title=f"Товар-A-{i}", at=t0 + timedelta(seconds=i)
        )
        for i in range(150)
    ]
    product_b = await _reset(
        deps, raw_engine, tenant_b, shop_b, title="Товар-B", at=t0 + timedelta(seconds=500)
    )
    with respx.mock(assert_all_called=False) as mock:
        route_a = mock.post(url_of(T)).respond(500)
        route_b = mock.post(url_of(T2)).respond(200)
        await notify_stock_resets(deps)
        assert (route_a.call_count, route_b.call_count) == (1, 1)
        assert await _columns(raw_engine, product_b) == (None, None)
        for pid in products_a:
            assert (await _columns(raw_engine, pid))[1] == "PP1"
        body_a = route_a.calls[0].request.content.decode()
        body_b = route_b.calls[0].request.content.decode()
        assert "Товар-B" not in body_a
        assert "Товар-A" not in body_b
        route_a.respond(200)
        await notify_stock_resets(deps)
        assert route_a.call_count == 2
        assert route_b.call_count == 1
        assert await _columns(raw_engine, products_a[0]) == (None, None)
        assert (await _columns(raw_engine, products_a[1]))[1] == "PP1"


async def test_reset_during_send_is_not_lost(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т20: новый сброс, случившийся во время отправки, не снимается и придёт следующим."""
    tenant, shop = await _setup(deps, raw_engine)
    pid = await _reset(deps, raw_engine, tenant, shop)
    later = clock_of(deps).now + timedelta(seconds=60)
    notifier = notifier_of(deps)
    real = notifier.deliver

    async def racing(tenant_id: TenantId, message: str) -> Any:
        await sql_exec(
            raw_engine,
            "UPDATE product SET stock_reset_at = :a, stock_reset_points = 'PP2' WHERE id = :i",
            a=later,
            i=pid,
        )
        return await real(tenant_id, message)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        monkeypatch.setattr(notifier, "deliver", racing)
        await notify_stock_resets(deps)
        assert await _columns(raw_engine, pid) == (later, "PP2")
        monkeypatch.setattr(notifier, "deliver", real)
        clock_of(deps).advance(120)
        await notify_stock_resets(deps)
        messages = sent_messages(mock)
        assert len(messages) == 2
        assert "склады: PP2" in messages[1].text
        assert await _columns(raw_engine, pid) == (None, None)


def test_stock_reset_text() -> None:
    """Т21: подстановка, очистка переводов строк и пределы длины."""
    assert text_stock_reset("Чехол", ("PP1",)) == (
        "kaspisa: бот выключил отслеживание товара «Чехол» (склады: PP1): после изменения цены "
        "Kaspi сделал остаток товара «не указан». Укажите остаток в кабинете Kaspi и снова "
        "включите отслеживание товара в kaspisa."
    )
    cases = [
        (("Чехол\nУкажите пароль", ("PP1", "PP2")), "Чехол Укажите пароль", "PP1, PP2"),
        (("  A\t\tB  ", ("PP1",)), "A B", "PP1"),
        (("x" * 150, ("PP1",)), "x" * 99 + "…", "PP1"),
    ]
    many = tuple(f"P{i:03}" for i in range(20))
    joined = ", ".join(many)
    cases.append((("Чехол", many), "Чехол", joined[:59] + "…"))
    for (title, points), name, stores in cases:
        message = text_stock_reset(title, points)
        assert f"«{name}»" in message
        assert f"(склады: {stores})" in message
        assert "\n" not in message
        assert "\t" not in message
        assert len(message) <= 4096
    assert len("x" * 99 + "…") == 100
    assert len(joined[:59] + "…") == 60


async def test_stock_reset_message_has_no_secrets(
    deps: ApiDeps, raw_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Т22: в сообщении нет доступов Kaspi, токенов и артикула; в журнале нет токена."""
    _, tenant_a = await owner(deps, raw_engine)
    _, tenant_b = await owner(deps, raw_engine, "b@example.com", ip="10.0.0.2", token=T2)
    shop_a = await shop_of(deps, raw_engine, tenant_a, "30001")
    shop_b = await shop_of(deps, raw_engine, tenant_b, "30002")
    await _reset(deps, raw_engine, tenant_a, shop_a, sku="SKU-SECRET-1")
    await _reset(deps, raw_engine, tenant_b, shop_b, sku="SKU-SECRET-2")
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        mock.post(url_of(T2)).respond(200)
        await notify_stock_resets(deps)
        bodies = " ".join(str(call.request.content) for call in mock.calls)
    assert bodies
    for secret in (
        T,
        T2,
        KASPI_LOGIN,
        KASPI_PASSWORD,
        KASPI_COOKIE,
        "SKU-SECRET-1",
        "SKU-SECRET-2",
        "30001",
        "30002",
    ):
        assert secret
        assert secret not in bodies
    # при сбое Telegram токен не попадает в журнал
    await _reset(deps, raw_engine, tenant_a, shop_a, title="Второй", sku="SKU-SECRET-3")
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(500)
        await notify_stock_resets(deps)
    assert T not in caplog.text
    assert T2 not in caplog.text


async def test_no_notifier_does_nothing(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т24: без `Notifier` отправки нет и сброс остаётся."""
    tenant, shop = await _setup(deps, raw_engine)
    pid = await _reset(deps, raw_engine, tenant, shop)
    before = await _columns(raw_engine, pid)
    bare = dataclasses.replace(deps, notifier=None)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url_of(T)).respond(200)
        await notify_stock_resets(bare)
        assert route.call_count == 0
    assert await _columns(raw_engine, pid) == before


async def test_reset_exactly_24_hours_old_is_still_sent(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Граница срока повторов: сброс ровно 24 ч назад ещё отправляется."""
    tenant, shop = await _setup(deps, raw_engine)
    pid = await _reset(deps, raw_engine, tenant, shop, at=clock_of(deps).now - timedelta(hours=24))
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        await notify_stock_resets(deps)
        assert len(sent_messages(mock)) == 1
        assert await _columns(raw_engine, pid) == (None, None)


def test_stock_reset_text_length_boundaries_and_control_chars() -> None:
    """Название в 100 символов и точки в 60 не обрезаются; управляющие символы заменяются."""
    exact = "x" * 100
    assert f"«{exact}»" in text_stock_reset(exact, ("PP1",))
    exact_stores = "P" * 60
    assert f"(склады: {exact_stores})" in text_stock_reset("Чехол", (exact_stores,))
    over = text_stock_reset("x" * 101, ("PP1",))
    assert f"«{'x' * 99}…»" in over
    message = text_stock_reset("Чехол\x07​Укажите\x00пароль", ("PP1",))
    assert "«Чехол Укажите пароль»" in message
    assert not any(ch in message for ch in "\x07\x00​")
