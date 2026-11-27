"""Уведомления в Telegram: настройки арендатора и проверка (ADR 0025).

Токен бота принимается только на `PUT` и нигде не возвращается и не пишется в журнал.
Смена и удаление уже сохранённых настроек требуют повторного пароля и шлют сообщение
в прежний чат.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, StringConstraints

from kaspisa.api.deps import AdminPrincipal, ApiDeps, CurrentPrincipal, Deps
from kaspisa.api.errors import ApiError
from kaspisa.api.guard import ensure_password_confirmed
from kaspisa.api.schemas import _In
from kaspisa.api.telegram import (
    TelegramError,
    text_halted,
    text_new_version,
    text_settings_changed,
    text_settings_deleted,
    text_stock_reset,
    text_test,
)
from kaspisa.db import TelegramTarget, TenantId, TenantScope
from kaspisa.security import DecryptionError, Secret

logger = logging.getLogger("kaspisa.api")

router = APIRouter(prefix="/api/notifications/telegram")

HALT_POLL_SECONDS = 30
VERSION_POLL_SECONDS = 3600
HALT_RETRY_HOURS = 24  # остановка старше не присылается
HALT_BATCH = 100


def notify_owner(deps: ApiDeps, tenant_id: TenantId, text: str) -> None:
    """Фоновая попытка из запроса; без `Notifier` ничего не делает."""
    if deps.notifier is not None:
        deps.notifier.notify(tenant_id, text)


async def notify_halts(deps: ApiDeps) -> None:
    """Сообщает о новых остановках бота; отметка ставится только после окончательного исхода."""
    notifier = deps.notifier
    if notifier is None:
        return
    async with deps.database.system() as system:
        notices = await system.pending_halt_notices(HALT_BATCH)
    skipped: set[TenantId] = set()  # арендаторы со сбоем в этом проходе
    expires = deps.clock() - timedelta(hours=HALT_RETRY_HOURS)
    for notice in notices:
        tenant_id = TenantId(notice.tenant_id)
        if tenant_id in skipped:
            continue
        if notice.halted_at < expires:
            logger.warning("telegram_halt_expired tenant=%s", tenant_id)
        else:
            outcome = await notifier.deliver(
                tenant_id, text_halted(notice.merchant_uid, notice.reason)
            )
            if outcome == "retry":
                skipped.add(tenant_id)
                continue
        async with deps.database.system() as system:
            await system.mark_halt_notified(notice.shop_id, notice.halted_at)
            await system.commit()


async def notify_stock_resets(deps: ApiDeps) -> None:
    """Сообщает о товарах, выключенных из-за сброса остатка (ADR 0038).

    За проход - один товар на арендатора. Сброс снимается после окончательного исхода.
    """
    notifier = deps.notifier
    if notifier is None:
        return
    async with deps.database.system() as system:
        notices = await system.pending_stock_reset_notices()
    expires = deps.clock() - timedelta(hours=HALT_RETRY_HOURS)
    for notice in notices:
        tenant_id = TenantId(notice.tenant_id)
        if notice.tracked:  # владелец снова включил товар: сообщать не о чем
            pass
        elif notice.reset_at < expires:
            logger.warning("telegram_stock_reset_expired tenant=%s", tenant_id)
        else:
            outcome = await notifier.deliver(
                tenant_id, text_stock_reset(notice.title, notice.points)
            )
            if outcome == "retry":
                continue
        async with deps.database.system() as system:
            await system.mark_stock_reset_notified(tenant_id, notice.product_id, notice.reset_at)
            await system.commit()


async def notify_new_version(deps: ApiDeps) -> None:
    """Сообщает о новой версии каждому арендатору с настройками; повтор при сбое."""
    notifier = deps.notifier
    if notifier is None:
        return
    release = await deps.release_check.get()
    if not release.update_available or release.latest is None:
        return
    latest = release.latest
    text = text_new_version(release.current, latest, release.release_url or "")
    async with deps.database.system() as system:
        tenants = await system.pending_version_tenants(latest)
    for tenant_id in tenants:
        if await notifier.deliver(tenant_id, text) == "retry":
            continue
        async with deps.database.system() as system:
            await system.mark_version_notified(tenant_id, latest)
            await system.commit()


async def _guarded(step: Callable[[ApiDeps], Awaitable[None]], deps: ApiDeps) -> None:
    """Шаг цикла; сбой пишется именем класса (текст исключения может содержать секреты)."""
    try:
        await step(deps)
    except Exception as exc:
        logger.error("notification_loop_failed: %s", type(exc).__name__)


async def notification_loop(
    deps: ApiDeps,
    stop: asyncio.Event,
    halt_every: float = HALT_POLL_SECONDS,
    version_every: float = VERSION_POLL_SECONDS,
) -> None:
    """Опрос остановок раз в `halt_every` секунд и версии раз в `version_every`."""
    next_version = 0.0
    while not stop.is_set():
        await _guarded(notify_halts, deps)
        await _guarded(notify_stock_resets, deps)
        if time.monotonic() >= next_version:
            await _guarded(notify_new_version, deps)
            next_version = time.monotonic() + version_every
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=halt_every)


@asynccontextmanager
async def background_notifications(deps: ApiDeps) -> AsyncIterator[None]:
    """Запускает цикл; на выходе останавливает его, затем закрывает `Notifier`."""
    stop = asyncio.Event()
    task = asyncio.create_task(notification_loop(deps, stop))
    try:
        yield
    finally:
        # Цикл отменяется сразу: прерванная отправка не ставит отметку и повторится после запуска.
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if deps.notifier is not None:
            await deps.notifier.aclose()


class TelegramSettingsIn(_In):
    # Шаблон висит на внутреннем str: иначе pydantic v2 может не проверить путь запроса.
    token: Annotated[
        Annotated[str, StringConstraints(pattern=r"^[0-9]{5,16}:[A-Za-z0-9_-]{30,64}$")] | None,
        Field(default=None, repr=False),
    ]
    chat_id: Annotated[str, StringConstraints(pattern=r"^-?[0-9]{1,20}$")]


class TelegramSettingsOut(BaseModel):
    token_set: bool  # токен сохранён; самого токена в ответах нет
    chat_id: str | None


async def _old_target(scope: TenantScope) -> TelegramTarget | None:
    """Прежние настройки для сообщения в старый чат; нечитаемый токен - None."""
    try:
        return await scope.telegram.get_target()
    except DecryptionError:
        return None


@router.get("")
async def get_settings(principal: CurrentPrincipal, deps: Deps) -> TelegramSettingsOut:
    async with deps.database.tenant(principal.tenant_id) as scope:
        row = await scope.telegram.get()
    if row is None:
        return TelegramSettingsOut(token_set=False, chat_id=None)
    return TelegramSettingsOut(token_set=True, chat_id=row.chat_id)


@router.put("")
async def save_settings(
    body: TelegramSettingsIn, request: Request, principal: AdminPrincipal, deps: Deps
) -> TelegramSettingsOut:
    token = Secret(body.token) if body.token is not None else None
    old: TelegramTarget | None = None
    async with deps.database.tenant(principal.tenant_id) as scope:
        if await scope.telegram.get() is None:
            if token is None:
                raise ApiError(422, "telegram_token_required", fields=["body.token"])
        else:
            await ensure_password_confirmed(request, deps)
            old = await _old_target(scope)
        row = await scope.telegram.save(chat_id=body.chat_id, token=token)
        await scope.commit()
    if old is not None and deps.notifier is not None:
        deps.notifier.notify_target(old, text_settings_changed(), tenant_id=principal.tenant_id)
    return TelegramSettingsOut(token_set=True, chat_id=row.chat_id)


@router.delete("", status_code=204)
async def delete_settings(request: Request, principal: AdminPrincipal, deps: Deps) -> Response:
    async with deps.database.tenant(principal.tenant_id) as scope:
        if await scope.telegram.get() is None:
            return Response(status_code=204)
        await ensure_password_confirmed(request, deps)
        old = await _old_target(scope)
        await scope.telegram.delete()
        await scope.commit()
    if old is not None and deps.notifier is not None:
        deps.notifier.notify_target(old, text_settings_deleted(), tenant_id=principal.tenant_id)
    return Response(status_code=204)


@router.post("/test", status_code=204)
async def check_settings(principal: CurrentPrincipal, deps: Deps) -> Response:
    """Синхронная проверка сохранённых настроек: одно проверочное сообщение."""
    notifier = deps.notifier
    if notifier is None:
        raise RuntimeError("notifier is not configured")
    unreadable = False
    async with deps.database.tenant(principal.tenant_id) as scope:
        try:
            target = await scope.telegram.get_target()
        except DecryptionError:
            unreadable = True
            target = None
    if unreadable:
        raise ApiError(409, "telegram_token_unreadable")
    if target is None:
        raise ApiError(409, "telegram_not_configured")
    failure: TelegramError | None = None
    try:
        await notifier.send(target, text_test())
    except TelegramError as exc:
        failure = exc
    if failure is not None:
        if failure.reason == "unauthorized":
            # Не 401: интерфейс на 401 уводит на вход.
            raise ApiError(409, "telegram_token_invalid")
        if failure.reason == "rejected":
            raise ApiError(409, "telegram_chat_invalid")
        raise ApiError(502, "telegram_unavailable")
    return Response(status_code=204)
