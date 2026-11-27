"""Вход магазина в Kaspi (C1-C4): единственное место вызова `login`, `submit_code`, `resend_code`.

Порядок (ADR 0009, раздел 6.3 проекта): чтение без открытой транзакции -> `ensure_operation_allowed`
-> блокировка входа и таймер письма -> запрос к Kaspi -> сохранение сессии и нового состояния одним
`commit()` с `expected_version`. Сеть не идёт внутри открытой транзакции базы.
Секреты (логин, пароль, код, cookie, `session.dump()`) в ответы и журнал не попадают.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast

from fastapi import APIRouter
from redis.exceptions import RedisError

from kaspisa.api import deps as api_deps
from kaspisa.api.deps import RESEND_INTERVAL_SECONDS, ApiDeps, CurrentPrincipal, Deps, Principal
from kaspisa.api.errors import ApiError
from kaspisa.api.schemas import CodeIn, ConnectResult
from kaspisa.api.shops import build_shop_out
from kaspisa.db import (
    ShopAlreadyConnectedError,
    StaleStateError,
    TenantScope,
)
from kaspisa.kaspi import (
    CodeRequired,
    ConnectionOperation,
    ConnectionState,
    KaspiCredentials,
    KaspiError,
    KaspiSession,
    KaspiTransportError,
    LoggedIn,
    LoginRejectedError,
    RateLimitedError,
    SessionBlobError,
    SessionPhase,
    SessionStateError,
    ensure_operation_allowed,
    next_connection_state,
)
from kaspisa.security import DecryptionError, Secret, UnknownKeyError
from kaspisa.worker.constants import LOGIN_LOCK_TTL_SECONDS
from kaspisa.worker.redis_limits import LimiterBusyError, LimiterUnavailableError

logger = logging.getLogger("kaspisa.api")

router = APIRouter(prefix="/api/shops")

Outcome = Literal[
    "connected",
    "code_required",
    "code_sent",
    "code_invalid",
    "rejected",
    "temporary_error",
    "restart_required",
]
Write = Literal["none", "connected", "awaiting_code", "reauth"]


@dataclass
class _Read:
    """Данные шага 1: состояние магазина и то, что нужно запросу (без открытой транзакции)."""

    state: ConnectionState
    version: int
    known_uid: str | None
    credentials: KaspiCredentials | None
    session: KaspiSession | None
    ready_at: datetime  # когда по базе можно слать письмо снова (connection_changed_at + 180 с)


@dataclass
class _Plan:
    """Что ответить и что записать по исходу запроса."""

    outcome: Outcome
    retry_after: int | None = None
    write: Write = "none"
    error: str | None = None
    merchant_uid: str | None = None
    state_blob: Secret | None = None
    mark_restart: bool = False


def _rejected(error: str) -> _Plan:
    return _Plan("rejected", write="reauth", error=error)


def _plan(
    operation: ConnectionOperation,
    state: ConnectionState,
    known_uid: str | None,
    outcome: object,
) -> _Plan:
    """Исход запроса -> ответ и запись (таблица 6.3.1 проекта)."""
    if isinstance(outcome, LoggedIn):
        next_connection_state(state, operation, outcome)
        uids = [merchant.uid for merchant in outcome.merchants]
        if len(uids) > 1:
            return _rejected("multiple_merchants")
        if not uids:
            return _rejected("no_merchants")
        if known_uid not in (None, uids[0]):
            return _rejected("merchant_mismatch")
        return _Plan(
            "connected",
            write="connected",
            merchant_uid=uids[0],
            state_blob=outcome.session.dump(),
        )
    if isinstance(outcome, CodeRequired):
        next_connection_state(state, operation, outcome)
        return _Plan("code_required", write="awaiting_code", state_blob=outcome.session.dump())
    if outcome is None:  # resend_code выполнен
        return _Plan("code_sent")
    if isinstance(outcome, LoginRejectedError):
        change = next_connection_state(state, operation, outcome)
        if change is not None and change.error is not None:
            return _rejected(change.error)
        if operation == "submit_code":
            if outcome.code == "MFA_CODE_INVALID":
                return _Plan("code_invalid")
            return _Plan("restart_required", mark_restart=True)
        return _Plan("temporary_error", retry_after=outcome.retry_after_seconds)
    if operation == "submit_code":
        # Код мог быть принят уже: повторно вводить его нельзя, нужен вход заново (Н9).
        return _Plan("restart_required", mark_restart=True)
    if isinstance(outcome, RateLimitedError):
        return _Plan("temporary_error", retry_after=outcome.retry_after)
    if isinstance(outcome, LimiterBusyError):
        return _Plan("temporary_error", retry_after=math.ceil(outcome.wait_seconds))
    return _Plan("temporary_error")


async def _read(
    deps: ApiDeps, principal: Principal, shop_id: uuid.UUID, operation: ConnectionOperation
) -> _Read:
    """Шаг 1: магазин, допустимость операции, метка, доступы и прежняя сессия."""
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.get(shop_id)
        if shop is None:
            raise ApiError(404, "not_found")
        state = cast(ConnectionState, shop.connection_state)
        try:
            ensure_operation_allowed(state, operation)
        except SessionStateError:
            raise ApiError(409, "operation_not_allowed") from None
        version = shop.connection_version
        if operation == "submit_code" and await deps.state.restart_marked(shop_id, version):
            raise ApiError(409, "restart_required")
        credentials: KaspiCredentials | None = None
        session: KaspiSession | None = None
        try:
            if operation in ("login", "login_restart"):
                stored = await scope.shops.get_credentials(shop_id)
                credentials = KaspiCredentials(login=stored.login, password=stored.password)
        except UnknownKeyError:
            logger.error("unknown_key shop_id=%s", shop_id)
            raise ApiError(503, "service_unavailable") from None
        except DecryptionError:
            raise ApiError(409, "credentials_unreadable") from None
        # Из not_connected прежняя сессия во вход не передаётся (ADR 0005).
        if not (operation == "login" and state == "not_connected"):
            try:
                blob = await scope.shops.get_session(shop_id)
                session = KaspiSession.load(blob) if blob is not None else None
            except UnknownKeyError:
                logger.error("unknown_key shop_id=%s", shop_id)
                raise ApiError(503, "service_unavailable") from None
            except (DecryptionError, SessionBlobError):
                session = None
        return _Read(
            state=state,
            version=version,
            known_uid=shop.merchant_uid,
            credentials=credentials,
            session=session,
            ready_at=shop.connection_changed_at + timedelta(seconds=RESEND_INTERVAL_SECONDS),
        )


async def _result(
    deps: ApiDeps, principal: Principal, shop_id: uuid.UUID, outcome: Outcome, retry: int | None
) -> ConnectResult:
    """Ответ: магазин читается заново после записи (удалён - 404)."""
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.get(shop_id)
    if shop is None:
        raise ApiError(404, "not_found")
    return ConnectResult(
        outcome=outcome, retry_after_seconds=retry, shop=await build_shop_out(deps, shop)
    )


async def _apply(
    scope: TenantScope,
    shop_id: uuid.UUID,
    version: int,
    plan: _Plan,
    save: KaspiSession | None,
) -> None:
    shops = scope.shops
    if save is not None:
        await shops.save_session(shop_id, save.dump(), expected_version=version)
    if plan.write == "connected" and plan.state_blob is not None and plan.merchant_uid:
        await shops.mark_connected(
            shop_id,
            merchant_uid=plan.merchant_uid,
            session_state=plan.state_blob,
            expected_version=version,
        )
    elif plan.write == "awaiting_code" and plan.state_blob is not None:
        await shops.mark_awaiting_code(shop_id, plan.state_blob, expected_version=version)
    elif plan.write == "reauth" and plan.error is not None:
        await shops.mark_reauth_required(shop_id, error=plan.error, expected_version=version)
    await scope.commit()


async def _write(
    deps: ApiDeps,
    principal: Principal,
    shop_id: uuid.UUID,
    version: int,
    plan: _Plan,
    save: KaspiSession | None,
) -> Outcome:
    """Шаг 4: сессия и состояние одним `commit()`; `NotFoundError` - 404 общим обработчиком.

    Возвращает итоговый исход ответа (при конфликте магазина Kaspi - `rejected`).
    """
    if plan.write == "none" and save is None:
        return plan.outcome
    try:
        try:
            async with deps.database.tenant(principal.tenant_id) as scope:
                await _apply(scope, shop_id, version, plan, save)
        except ShopAlreadyConnectedError:
            # Магазин Kaspi подключён в другой записи: сессия своего кабинета сохраняется.
            conflict = _rejected("shop_already_connected")
            async with deps.database.tenant(principal.tenant_id) as scope:
                await _apply(scope, shop_id, version, conflict, save)
            return conflict.outcome
    except StaleStateError:
        raise ApiError(409, "state_changed") from None
    return plan.outcome


async def run_connect(
    deps: ApiDeps,
    principal: Principal,
    shop_id: uuid.UUID,
    operation: ConnectionOperation,
    code: str | None = None,
) -> ConnectResult:
    read = await _read(deps, principal, shop_id, operation)
    call: Callable[[], Awaitable[object]]
    if operation in ("login", "login_restart") and read.credentials is not None:
        credentials, previous = read.credentials, read.session

        async def login_call() -> object:
            return await deps.client.login(credentials, previous)

        call = login_call
    elif read.session is None:
        # Сохранённой сессии нет или она не читается: код вводить некуда, нужен вход заново.
        await deps.state.mark_restart(shop_id, read.version)
        return await _result(deps, principal, shop_id, "restart_required", None)
    else:
        loaded = read.session

        async def session_call() -> object:
            if operation == "submit_code" and code is not None:
                return await deps.client.submit_code(loaded, code)
            await deps.client.resend_code(loaded)
            return None

        call = session_call

    async with deps.login_lock.hold(shop_id, LOGIN_LOCK_TTL_SECONDS * 1000) as acquired:
        if not acquired:
            raise ApiError(409, "login_in_progress")
        await _recheck_version(deps, principal, shop_id, read.version)
        timer_started = False
        if operation in ("resend_code", "login_restart"):
            left_db = math.ceil((read.ready_at - deps.clock()).total_seconds())
            if left_db > 0:
                raise ApiError(429, "resend_too_early", retry_after_seconds=left_db)
            left = await deps.state.start_resend_timer(shop_id, only_if_free=True)
            if left is not None:
                raise ApiError(429, "resend_too_early", retry_after_seconds=left)
            timer_started = True

        outcome: object
        try:
            async with asyncio.timeout(api_deps.LOGIN_CALL_TIMEOUT_SECONDS):
                outcome = await call()
        except (KaspiError, LimiterBusyError, LimiterUnavailableError) as exc:
            outcome = exc
        except TimeoutError:
            outcome = KaspiTransportError(operation, "timeout")

        if isinstance(outcome, LimiterBusyError | LimiterUnavailableError):
            # Ограничитель отказал до отправки запроса: письмо не ушло, таймер снимается.
            if timer_started:
                await deps.state.clear_resend_timer(shop_id)
            if isinstance(outcome, LimiterUnavailableError):
                logger.error("limiter_unavailable shop_id=%s", shop_id)
                if operation != "submit_code":
                    raise ApiError(503, "service_unavailable")

        plan = _plan(operation, read.state, read.known_uid, outcome)
        request_session = (
            read.session
            if operation in ("submit_code", "resend_code")
            else outcome.session
            if isinstance(outcome, LoggedIn | CodeRequired)
            else None
        )
        save = (
            request_session
            if request_session is not None
            and request_session.changed
            and request_session.phase is not SessionPhase.NEW
            else None
        )
        outcome_code = await _write(deps, principal, shop_id, read.version, plan, save)
        # Redis после записи в базу: сбой не должен терять уже записанные сессию и состояние.
        if isinstance(outcome, CodeRequired):
            timer = deps.state.start_resend_timer(shop_id, only_if_free=False)
            await _after_write(shop_id, "start_resend_timer", timer)
        if plan.mark_restart:
            mark = deps.state.mark_restart(shop_id, read.version)
            await _after_write(shop_id, "mark_restart", mark)
        return await _result(deps, principal, shop_id, outcome_code, plan.retry_after)


async def _recheck_version(
    deps: ApiDeps, principal: Principal, shop_id: uuid.UUID, version: int
) -> None:
    """Под блокировкой входа версия должна быть той, что прочитана до неё."""
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.get(shop_id)
    if shop is None:
        raise ApiError(404, "not_found")
    if shop.connection_version != version:
        raise ApiError(409, "state_changed")


async def _after_write(shop_id: uuid.UUID, op: str, action: Awaitable[object]) -> None:
    """Операция Redis после записи в базу: сбой пишется в журнал, ответ строится как обычно."""
    try:
        await action
    except (RedisError, OSError):
        logger.warning("redis_after_write op=%s shop_id=%s", op, shop_id)


@router.post("/{shop_id}/login")
async def connect_login(
    shop_id: uuid.UUID, principal: CurrentPrincipal, deps: Deps
) -> ConnectResult:
    return await run_connect(deps, principal, shop_id, "login")


@router.post("/{shop_id}/login/restart")
async def connect_restart(
    shop_id: uuid.UUID, principal: CurrentPrincipal, deps: Deps
) -> ConnectResult:
    return await run_connect(deps, principal, shop_id, "login_restart")


@router.post("/{shop_id}/code")
async def connect_code(
    shop_id: uuid.UUID, body: CodeIn, principal: CurrentPrincipal, deps: Deps
) -> ConnectResult:
    return await run_connect(deps, principal, shop_id, "submit_code", body.code)


@router.post("/{shop_id}/code/resend")
async def connect_resend(
    shop_id: uuid.UUID, principal: CurrentPrincipal, deps: Deps
) -> ConnectResult:
    return await run_connect(deps, principal, shop_id, "resend_code")
