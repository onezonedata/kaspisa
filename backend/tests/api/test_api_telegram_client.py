"""ADR 0025, Т9-Т16, Т52: клиент Telegram, `Notifier`, тексты, токен вне журналов."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from api_testing import api_cipher, api_database_url, deps, redis_client, redis_prefix

from kaspisa.api import ApiDeps, telegram
from kaspisa.api.telegram import (
    HTTP_TIMEOUT,
    Notifier,
    TelegramError,
    text_halted,
    text_live,
    text_min_lowered,
    text_new_device,
    text_new_version,
    text_settings_changed,
    text_settings_deleted,
    text_test,
)
from kaspisa.db import TelegramTarget, TenantId, create_database
from kaspisa.security import KeyRing, Secret, SecretCipher

T = "123456789:" + "A" * 35
API = "https://api.telegram.org"
URL = f"{API}/bot{T}/sendMessage"
BACKEND = Path(__file__).resolve().parents[2]

__all__ = ["api_cipher", "api_database_url", "deps", "redis_client", "redis_prefix"]


async def tenant_with_telegram(
    deps: ApiDeps, *, token: str = T, chat_id: str = "42", cipher: SecretCipher | None = None
) -> TenantId:
    """Арендатор с сохранёнными настройками Telegram (токен шифруется репозиторием)."""
    async with deps.database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    tenant_id = TenantId(tenant.id)
    async with deps.database.tenant(tenant_id) as scope:
        await scope.telegram.save(chat_id=chat_id, token=Secret(token))
        await scope.commit()
    return tenant_id


@pytest.fixture
async def notifier(deps: ApiDeps) -> AsyncIterator[Notifier]:
    instance = Notifier(deps.database)
    try:
        yield instance
    finally:
        await instance.aclose()


# (ответ или исключение, исход send, исход deliver)
CASES: list[tuple[Any, str | None, str]] = [
    (200, None, "sent"),
    (401, "unauthorized", "final"),
    (400, "rejected", "final"),
    (403, "rejected", "final"),
    (404, "rejected", "final"),
    (302, "unavailable", "retry"),
    (429, "unavailable", "retry"),
    (500, "unavailable", "retry"),
    (502, "unavailable", "retry"),
    (httpx.ConnectError("boom " + T), "unavailable", "retry"),
    (httpx.ReadTimeout("slow " + T), "timeout", "retry"),
    (httpx.ConnectTimeout("slow " + T), "timeout", "retry"),
]
CASE_IDS = [str(c[0]) if isinstance(c[0], int) else type(c[0]).__name__ for c in CASES]


def _mock(mock: respx.MockRouter, outcome: Any) -> respx.Route:
    route = mock.post(URL)
    if isinstance(outcome, Exception):
        route.mock(side_effect=outcome)
    elif outcome == 302:
        route.respond(302, headers={"Location": f"{API}/elsewhere/{T}"})
    else:
        route.respond(outcome, json={"ok": outcome == 200, "description": "secret-" + T})
    return route


async def test_send_request_shape(notifier: Notifier) -> None:
    """Т9: один POST на URL с токеном, тело строго по проекту."""
    with respx.mock() as mock:
        route = mock.post(URL).respond(200, json={"ok": True})
        await notifier.send(TelegramTarget(Secret(T), "42"), "привет")
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "chat_id": "42",
        "text": "привет",
        "link_preview_options": {"is_disabled": True},
    }


@pytest.mark.parametrize(("outcome", "send_reason", "delivery"), CASES, ids=CASE_IDS)
async def test_send_and_deliver_outcomes(
    deps: ApiDeps,
    notifier: Notifier,
    outcome: Any,
    send_reason: str | None,
    delivery: str,
) -> None:
    """Т10, Т11: исходы send и deliver; исключение без токена и без цепочки."""
    with respx.mock() as mock:
        route = _mock(mock, outcome)
        if send_reason is None:
            await notifier.send(TelegramTarget(Secret(T), "42"), "x")
        else:
            with pytest.raises(TelegramError) as info:
                await notifier.send(TelegramTarget(Secret(T), "42"), "x")
            err = info.value
            assert err.reason == send_reason
            assert T not in str(err)
            assert T not in repr(err)
            assert err.__cause__ is None
            assert err.__context__ is None
        tenant_id = await tenant_with_telegram(deps)
        assert await notifier.deliver(tenant_id, "x") == delivery
    assert route.call_count == 2


@pytest.mark.parametrize(("outcome", "send_reason", "delivery"), CASES, ids=CASE_IDS)
async def test_deliver_log_has_no_token(
    deps: ApiDeps,
    notifier: Notifier,
    caplog: pytest.LogCaptureFixture,
    outcome: Any,
    send_reason: str | None,
    delivery: str,
) -> None:
    """Т12: в журнале нет токена и трассировок; сбой записан кодом."""
    tenant_id = await tenant_with_telegram(deps)
    caplog.set_level(logging.DEBUG)
    with respx.mock() as mock:
        _mock(mock, outcome)
        assert await notifier.deliver(tenant_id, "x") == delivery
    assert T not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    failed = [r.getMessage() for r in caplog.records if "telegram_send_failed" in r.getMessage()]
    if send_reason is None:
        assert failed == []
    else:
        assert failed == [f"telegram_send_failed: {send_reason} tenant={tenant_id}"]


def test_import_main_quiets_http_loggers_and_installs_filter() -> None:
    """Т12: после `import kaspisa.main` уровни не ниже WARNING, на `httpx` стоит фильтр."""
    code = (
        "import logging, kaspisa.main;"
        "print([logging.getLogger(n).getEffectiveLevel() >= logging.WARNING"
        " for n in ('httpx', 'httpcore', 'hpack')]);"
        "print([type(f).__name__ for f in logging.getLogger('httpx').filters])"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    first, second = result.stdout.strip().splitlines()
    assert first == "[True, True, True]"
    assert "TokenRedactingFilter" in second


def test_timeout_limits() -> None:
    """Т13: предел отправки задан."""
    assert HTTP_TIMEOUT.connect == 5
    assert HTTP_TIMEOUT.read == 10


async def test_notify_does_not_wait_and_drain_cancels(
    deps: ApiDeps,
    notifier: Notifier,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Т14: `notify` возвращается сразу; `drain` отменяет висящие; предел задач."""
    tenant_id = await tenant_with_telegram(deps)
    never = asyncio.Event()
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await never.wait()
        return httpx.Response(200)

    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(URL).mock(side_effect=hang)
        notifier.notify(tenant_id, "x")
        await asyncio.wait_for(started.wait(), timeout=5)  # запрос ушёл, ответа нет
        assert len(notifier._tasks) == 1
        await notifier.drain(timeout=0.1)
        assert not notifier._tasks

        monkeypatch.setattr(telegram, "MAX_PENDING", 2)
        notifier.notify(tenant_id, "1")
        notifier.notify(tenant_id, "2")
        notifier.notify(tenant_id, "3")
        assert len(notifier._tasks) == 2
        assert f"telegram_queue_full tenant={tenant_id}" in caplog.text
        await notifier.drain(timeout=0.1)
        assert not notifier._tasks


async def test_no_settings_and_unreadable_token(
    deps: ApiDeps,
    notifier: Notifier,
    api_database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Т15: без настроек запросов нет; нечитаемый токен - `final` и строка журнала."""
    caplog.set_level(logging.DEBUG)
    async with deps.database.system() as system:
        bare = await system.create_tenant(name="bare")
        await system.commit()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(URL).respond(200)
        assert await notifier.deliver(TenantId(bare.id), "x") == "no_settings"
        assert route.call_count == 0
        assert "telegram" not in caplog.text

        tenant_id = await tenant_with_telegram(deps)
        foreign_db = create_database(
            api_database_url, cipher=SecretCipher(KeyRing(5, {5: b"k" * 32}))
        )
        other = Notifier(foreign_db)
        try:
            assert await other.deliver(tenant_id, "x") == "final"
        finally:
            await other.aclose()
            await foreign_db.dispose()
        assert route.call_count == 0
    assert f"telegram_token_unreadable tenant={tenant_id}" in caplog.text
    assert T not in caplog.text


def test_texts() -> None:
    """Т16: тексты ровно по таблице проекта."""
    assert text_test() == "kaspisa: проверка уведомлений. Настройка работает."
    assert text_new_device("10.0.0.5", "Mozilla/5.0") == (
        "kaspisa: вход в учётную запись с нового устройства, адрес 10.0.0.5, "
        "браузер Mozilla/5.0. Если это были не вы, нажмите «Выйти на всех устройствах» "
        "и смените пароль."
    )
    assert text_min_lowered("30001", "SKU-1", 15000, 11000) == (
        "kaspisa: магазин 30001, товар SKU-1: минимальная цена снижена с 15000 до 11000 тенге. "
        "Если это были не вы, нажмите «Выйти на всех устройствах» и проверьте границы цены."
    )
    assert text_min_lowered("30001", "SKU-1", None, 11000) == (
        "kaspisa: магазин 30001, товар SKU-1: минимальная цена задана: 11000 тенге. "
        "Если это были не вы, нажмите «Выйти на всех устройствах» и проверьте границы цены."
    )
    live = (
        "kaspisa: магазин {} переведён в режим «работает»: бот будет отправлять цены в Kaspi. "
        "Если это были не вы, смените режим и нажмите «Выйти на всех устройствах»."
    )
    assert text_live("30001") == live.format("30001")
    assert text_live(None) == live.format("без номера")
    halted = (
        "kaspisa: бот магазина 30001 остановлен: {}. "
        "Цены не меняются, пока вы не снимете остановку на странице магазина."
    )
    assert text_halted("30001", "login_errors") == halted.format("ошибки входа в Kaspi")
    assert text_halted("30001", "write_errors") == halted.format("ошибки записи цены")
    assert text_halted("30001", "store_anomalies") == halted.format("расхождения в данных магазина")
    assert text_halted("30001", "other") == halted.format("причина other")
    assert text_new_version("1.1.0", "1.2.0", "https://github.com/o/r/releases/tag/v1.2.0") == (
        "kaspisa: доступна новая версия 1.2.0, у вас 1.1.0. "
        "https://github.com/o/r/releases/tag/v1.2.0"
    )
    assert text_settings_changed() == (
        "kaspisa: настройки уведомлений изменены, сообщения теперь могут идти в другой чат. "
        "Если это были не вы, нажмите «Выйти на всех устройствах» и смените пароль."
    )
    assert text_settings_deleted() == (
        "kaspisa: уведомления в Telegram отключены. "
        "Если это были не вы, нажмите «Выйти на всех устройствах» и смените пароль."
    )


@pytest.mark.parametrize("status", [200, 500])
async def test_httpx_log_is_redacted_even_at_debug(
    notifier: Notifier, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    """Т52: фильтр прячет токен, даже если логгер `httpx` понизили до DEBUG."""
    httpx_logger = logging.getLogger("httpx")
    previous = httpx_logger.level
    httpx_logger.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    try:
        with respx.mock() as mock:
            mock.post(URL).respond(status)
            with contextlib.suppress(TelegramError):
                await notifier.send(TelegramTarget(Secret(T), "42"), "x")
    finally:
        httpx_logger.setLevel(previous)
    assert T not in caplog.text
    requests = [r.getMessage() for r in caplog.records if "HTTP Request" in r.getMessage()]
    assert requests
    assert all("/bot***/sendMessage" in message for message in requests)
