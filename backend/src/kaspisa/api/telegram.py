"""Отправка уведомлений в Telegram (ADR 0025): клиент, `Notifier`, тексты, защита токена.

Токен бота стоит в пути запроса, поэтому он нигде не пишется: исключения несут только код,
ответ Telegram не читается, адрес в журнале `httpx` заменяется фильтром.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx

from kaspisa.db import Database, TelegramTarget, TenantId
from kaspisa.security import DecryptionError

logger = logging.getLogger("kaspisa.api")

# Адрес запроса попадает в журналы http-библиотек: уровни не ниже WARNING (как у клиента Kaspi).
for _name in ("httpx", "httpcore", "hpack"):
    logging.getLogger(_name).setLevel(logging.WARNING)

TELEGRAM_API = "https://api.telegram.org"
HTTP_TIMEOUT = httpx.Timeout(10, connect=5)
MAX_PENDING = 100  # предел фоновых задач из запросов на процесс
DRAIN_SECONDS = 5  # ожидание задач при остановке

Reason = Literal["unauthorized", "rejected", "unavailable", "timeout"]
Delivery = Literal["sent", "no_settings", "final", "retry"]

_TOKEN_IN_URL = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")


class TokenRedactingFilter(logging.Filter):
    """Заменяет `/bot<токен>` на `/bot***` в записях журнала `httpx`."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_IN_URL.sub("/bot***", message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def _install_filter() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, TokenRedactingFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(TokenRedactingFilter())


_install_filter()


class TelegramError(Exception):
    """Сбой отправки; текст - только код. Цепочки исключений нет (в них URL с токеном)."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


async def send_message(client: httpx.AsyncClient, target: TelegramTarget, text: str) -> None:
    """Одна отправка. Исключение поднимается вне блока `except`: без `__cause__`."""
    reason: Reason
    url = f"{TELEGRAM_API}/bot{target.token.reveal()}/sendMessage"
    payload = {
        "chat_id": target.chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    try:
        response = await client.post(url, json=payload)
    except httpx.TimeoutException:
        reason = "timeout"
    except (httpx.HTTPError, OSError):
        reason = "unavailable"
    else:
        status = response.status_code
        if status == 200:
            return
        if status == 401:
            reason = "unauthorized"
        elif status in (400, 403, 404):
            reason = "rejected"
        else:
            reason = "unavailable"
    raise TelegramError(reason)


class Notifier:
    """Отправка уведомлений арендатору; владеет одним клиентом `httpx` и фоновыми задачами."""

    def __init__(
        self,
        database: Database,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout = HTTP_TIMEOUT,
    ) -> None:
        self._database = database
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=transport
        )
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, target: TelegramTarget, text: str) -> None:
        """Одна отправка; поднимает `TelegramError`."""
        await send_message(self._client, target, text)

    async def deliver(self, tenant_id: TenantId, text: str) -> Delivery:
        """Читает настройки арендатора и отправляет; исключений не поднимает."""
        try:
            async with self._database.tenant(tenant_id) as scope:
                target = await scope.telegram.get_target()
        except DecryptionError:
            logger.warning("telegram_token_unreadable tenant=%s", tenant_id)
            return "final"
        except Exception as exc:
            logger.warning("telegram_send_failed: %s tenant=%s", type(exc).__name__, tenant_id)
            return "retry"
        if target is None:
            return "no_settings"
        error: TelegramError | None = None
        try:
            await self.send(target, text)
        except TelegramError as exc:
            error = exc
        except Exception as exc:
            logger.warning("telegram_send_failed: %s tenant=%s", type(exc).__name__, tenant_id)
            return "retry"
        if error is None:
            return "sent"
        logger.warning("telegram_send_failed: %s tenant=%s", error.reason, tenant_id)
        return "final" if error.reason in ("unauthorized", "rejected") else "retry"

    def _spawn(self, work: Callable[[], Awaitable[object]], *, tenant: str) -> None:
        """`tenant` - готовый суффикс журнала (` tenant=<id>` или пусто)."""
        if len(self._tasks) >= MAX_PENDING:
            logger.warning("telegram_queue_full%s", tenant)
            return

        async def run() -> None:
            try:
                await work()
            except Exception as exc:
                logger.warning("telegram_send_failed: %s%s", type(exc).__name__, tenant)

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def notify(self, tenant_id: TenantId, text: str) -> None:
        """Фоновая попытка: возвращается сразу, исход пишется только в журнал."""
        self._spawn(lambda: self.deliver(tenant_id, text), tenant=f" tenant={tenant_id}")

    def notify_target(
        self, target: TelegramTarget, text: str, *, tenant_id: TenantId | None = None
    ) -> None:
        """Фоновая отправка на заданные настройки (прежний чат при смене настроек).

        `tenant_id` нужен только для журнала.
        """
        suffix = f" tenant={tenant_id}" if tenant_id is not None else ""

        async def work() -> None:
            error: TelegramError | None = None
            try:
                await self.send(target, text)
            except TelegramError as exc:
                error = exc
            if error is not None:
                logger.warning("telegram_send_failed: %s%s", error.reason, suffix)

        self._spawn(work, tenant=suffix)

    async def drain(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """Ждёт фоновые задачи (по умолчанию `DRAIN_SECONDS`); оставшиеся отменяет."""
        tasks = list(self._tasks)
        if not tasks:
            return
        limit = DRAIN_SECONDS if timeout is None else timeout
        _, pending = await asyncio.wait(tasks, timeout=limit)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain()
        await self._client.aclose()


# --- тексты: простой текст, секретов нет по построению ---

_NOT_YOU_SESSIONS = "Если это были не вы, нажмите «Выйти на всех устройствах»"
_HALT_REASONS = {
    "login_errors": "ошибки входа в Kaspi",
    "write_errors": "ошибки записи цены",
    "store_anomalies": "расхождения в данных магазина",
}


def _shop(merchant_uid: str | None) -> str:
    return merchant_uid or "без номера"


def text_test() -> str:
    return "kaspisa: проверка уведомлений. Настройка работает."


def text_new_device(ip: str, browser: str) -> str:
    return (
        f"kaspisa: вход в учётную запись с нового устройства, адрес {ip}, браузер {browser}. "
        f"{_NOT_YOU_SESSIONS} и смените пароль."
    )


def text_min_lowered(merchant_uid: str | None, sku: str, old: int | None, new: int) -> str:
    head = f"kaspisa: магазин {_shop(merchant_uid)}, товар {sku}: "
    if old is None:
        change = f"минимальная цена задана: {new} тенге."
    else:
        change = f"минимальная цена снижена с {old} до {new} тенге."
    return f"{head}{change} {_NOT_YOU_SESSIONS} и проверьте границы цены."


def text_live(merchant_uid: str | None) -> str:
    return (
        f"kaspisa: магазин {_shop(merchant_uid)} переведён в режим «работает»: "
        "бот будет отправлять цены в Kaspi. "
        "Если это были не вы, смените режим и нажмите «Выйти на всех устройствах»."
    )


def text_halted(merchant_uid: str | None, reason: str) -> str:
    cause = _HALT_REASONS.get(reason, f"причина {reason}")
    return (
        f"kaspisa: бот магазина {_shop(merchant_uid)} остановлен: {cause}. "
        "Цены не меняются, пока вы не снимете остановку на странице магазина."
    )


def _clean(value: str) -> str:
    """Заменяет каждую последовательность пробельных и непечатаемых символов одним пробелом."""
    parts: list[str] = []
    gap = False
    for ch in value:
        if ch.isspace() or not ch.isprintable():
            gap = True
            continue
        if gap and parts:
            parts.append(" ")
        gap = False
        parts.append(ch)
    return "".join(parts)


def _cut(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def text_stock_reset(title: str, points: tuple[str, ...]) -> str:
    """Сообщение о товаре, выключенном из-за сброса остатка (ADR 0038); только название и точки."""
    name = _cut(_clean(title), 100)
    stores = _cut(_clean(", ".join(points)), 60)
    return (
        f"kaspisa: бот выключил отслеживание товара «{name}» (склады: {stores}): "
        "после изменения цены Kaspi сделал остаток товара «не указан». "
        "Укажите остаток в кабинете Kaspi и снова включите отслеживание товара в kaspisa."
    )


def text_new_version(current: str, latest: str, url: str) -> str:
    return f"kaspisa: доступна новая версия {latest}, у вас {current}. {url}"


def text_settings_changed() -> str:
    return (
        "kaspisa: настройки уведомлений изменены, сообщения теперь могут идти в другой чат. "
        f"{_NOT_YOU_SESSIONS} и смените пароль."
    )


def text_settings_deleted() -> str:
    return f"kaspisa: уведомления в Telegram отключены. {_NOT_YOU_SESSIONS} и смените пароль."
