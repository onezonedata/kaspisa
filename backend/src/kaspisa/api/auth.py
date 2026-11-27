"""Маршруты /api/auth: регистрация, вход, выход, текущий пользователь, сброс пароля."""

from __future__ import annotations

import hashlib
import secrets
import uuid

from fastapi import APIRouter, Request, Response

from kaspisa.api.deps import (
    LOGIN_FAIL_EMAIL_LIMIT,
    LOGIN_FAIL_LIMIT,
    LOGIN_FAIL_WINDOW_SECONDS,
    PASSWORD_CONFIRM_FAIL_LIMIT,
    PASSWORD_CONFIRM_FAIL_WINDOW_SECONDS,
    REGISTER_IP_LIMIT,
    REGISTER_IP_WINDOW_SECONDS,
    RESET_IP_LIMIT,
    RESET_IP_WINDOW_SECONDS,
    SESSION_COOKIE,
    ApiDeps,
    CurrentPrincipal,
    Deps,
    set_session_cookie,
)
from kaspisa.api.errors import ApiError
from kaspisa.api.notifications import notify_owner
from kaspisa.api.passwords import (
    DUMMY_HASH,
    hash_password_async,
    verify_password_async,
)
from kaspisa.api.schemas import LoginIn, PasswordConfirmIn, PasswordResetIn, RegisterIn, UserOut
from kaspisa.api.telegram import text_new_device
from kaspisa.db import EmailTakenError, OwnerEventKind, TenantId

router = APIRouter(prefix="/api/auth")

DEVICE_COOKIE = "kaspisa_device"
DEVICE_TTL_SECONDS = 34_560_000  # 400 дней - предел браузеров
DEVICES_MAX = 20  # известных устройств на пользователя
DEVICE_MAX = 128  # длиннее - cookie считается отсутствующей
USER_AGENT_MAX = 120


def client_ip(request: Request) -> str:
    """Адрес клиента (за прокси - после `proxy-headers` uvicorn, этап 9)."""
    return request.client.host if request.client else "unknown"


def _set_cookie(response: Response, deps: ApiDeps, token: str) -> None:
    set_session_cookie(response, deps.cookie_secure, token)


@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request, response: Response, deps: Deps) -> UserOut:
    left = await deps.state.hit(
        f"register:{client_ip(request)}",
        limit=REGISTER_IP_LIMIT,
        window=REGISTER_IP_WINDOW_SECONDS,
    )
    if left is not None:
        raise ApiError(429, "too_many_attempts", retry_after_seconds=left)
    # Режим «только владелец» (ADR 0027): закрыто до хеширования, ответ не зависит от e-mail.
    async with deps.database.system() as system:
        if await system.has_users():
            raise ApiError(403, "registration_closed")
    password_hash = await hash_password_async(body.password)
    try:
        async with deps.database.system() as system:
            user = await system.register_first(email=body.email, password_hash=password_hash)
    except EmailTakenError:
        # При пустой app_user занять e-mail нельзя: значит, кто-то уже зарегистрировался.
        raise ApiError(403, "registration_closed") from None
    if user is None:
        raise ApiError(403, "registration_closed")
    tenant_id = TenantId(user.tenant_id)
    await _log_login(deps, tenant_id, user.id, client_ip(request))
    token = await deps.state.create_session(user.id, tenant_id)
    _set_cookie(response, deps, token)
    # Устройство регистрации становится известным; уведомления нет (настроек ещё нет).
    await _issue_device(request, response, deps, user.id, known_only=False)
    return UserOut(id=user.id, email=user.email)


def _clean_browser(user_agent: str | None) -> str:
    """`User-Agent` для сообщения: без управляющих символов, до 120 символов."""
    cleaned = "".join(ch for ch in (user_agent or "") if ch.isprintable())[:USER_AGENT_MAX]
    return cleaned or "неизвестен"


async def _issue_device(
    request: Request, response: Response, deps: ApiDeps, user_id: uuid.UUID, *, known_only: bool
) -> bool:
    """Ставит cookie устройства; возвращает True, если устройство уже было известно.

    Значение длиннее 128 символов считается отсутствующим. `known_only` - вход (проверяем
    известность); при регистрации устройство всегда новое и тихое.
    """
    device = request.cookies.get(DEVICE_COOKIE)
    if device is not None and len(device) > DEVICE_MAX:
        device = None
    now = deps.clock().timestamp()
    known = (
        known_only
        and device is not None
        and await deps.state.device_known(user_id, device, now=now, ttl=DEVICE_TTL_SECONDS)
    )
    if not known or device is None:
        device = secrets.token_urlsafe(32)  # неизвестную cookie не сохраняем: выдаём новую
    await deps.state.remember_device(
        user_id, device, now=now, ttl=DEVICE_TTL_SECONDS, limit=DEVICES_MAX
    )
    response.set_cookie(
        DEVICE_COOKIE,
        device,
        max_age=DEVICE_TTL_SECONDS,
        path="/api/auth",
        secure=deps.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return known


async def _log_login(deps: ApiDeps, tenant_id: TenantId, user_id: uuid.UUID, ip: str) -> None:
    """Событие `login` журнала владельца: адрес и время, без секретов (ADR 0024)."""
    async with deps.database.tenant(tenant_id) as scope:
        await scope.owner_events.add(kind=OwnerEventKind.LOGIN, user_id=user_id, ip=ip)
        await scope.commit()


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, deps: Deps) -> UserOut:
    ip = client_ip(request)
    email_digest = hashlib.sha256(body.email.encode()).hexdigest()
    # Счётчики растут до проверки пароля: параллельные запросы не обходят предел.
    pair_left = await deps.state.hit(
        f"login_fail:{email_digest}:{ip}",
        limit=LOGIN_FAIL_LIMIT,
        window=LOGIN_FAIL_WINDOW_SECONDS,
    )
    email_left = await deps.state.hit(
        f"login_fail:{email_digest}",
        limit=LOGIN_FAIL_EMAIL_LIMIT,
        window=LOGIN_FAIL_WINDOW_SECONDS,
    )
    if pair_left is not None or email_left is not None:
        longest = max(pair_left or 0, email_left or 0)
        raise ApiError(429, "too_many_attempts", retry_after_seconds=longest)
    async with deps.database.system() as system:
        user = await system.find_user_by_email(body.email)
    stored = user.password_hash if user is not None else DUMMY_HASH
    matched = await verify_password_async(body.password, stored)
    if user is None or not matched:
        raise ApiError(401, "invalid_credentials")
    await deps.state.clear_login_failures(body.email, ip)
    token = await deps.state.create_session(user.id, TenantId(user.tenant_id))
    # Сброс пароля мог пройти между проверкой и созданием сессии: сессия не должна пережить его.
    async with deps.database.system() as system:
        fresh = await system.find_user_by_email(body.email)
    if fresh is None or fresh.password_hash != stored:
        await deps.state.delete_session(token)
        raise ApiError(401, "invalid_credentials")
    await _log_login(deps, TenantId(user.tenant_id), user.id, ip)
    _set_cookie(response, deps, token)
    known = await _issue_device(request, response, deps, user.id, known_only=True)
    if not known:
        browser = _clean_browser(request.headers.get("user-agent"))
        notify_owner(deps, TenantId(user.tenant_id), text_new_device(ip, browser))
    return UserOut(id=user.id, email=user.email)


def _logged_out(deps: ApiDeps) -> Response:
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=deps.cookie_secure, httponly=True, samesite="lax"
    )
    return response


@router.post("/logout", status_code=204)
async def logout(request: Request, deps: Deps) -> Response:
    """Выход: завершает только текущую сессию (ADR 0012)."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await deps.state.delete_session(token)
    return _logged_out(deps)


@router.post("/logout-all", status_code=204)
async def logout_all(request: Request, deps: Deps) -> Response:
    """Выход на всех устройствах: завершает все сессии учётной записи (ADR 0012)."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await deps.state.revoke_sessions_of_token(token)
    return _logged_out(deps)


@router.get("/me")
async def me(principal: CurrentPrincipal, deps: Deps) -> UserOut:
    async with deps.database.tenant(principal.tenant_id) as scope:
        user = await scope.users.get(principal.user_id)
    if user is None:
        raise ApiError(401, "unauthenticated")
    return UserOut(id=user.id, email=user.email)


@router.post("/confirm-password", status_code=204)
async def confirm_password(
    body: PasswordConfirmIn, request: Request, principal: CurrentPrincipal, deps: Deps
) -> Response:
    """Повторный ввод пароля перед опасной правкой границ (ADR 0024).

    Счётчик растёт до проверки и считается по пользователю: украденную cookie нельзя размножить
    на сессии. Верный пароль открывает окно на 10 минут для токена этой сессии.
    """
    left = await deps.state.hit(
        f"pw_confirm_fail:{principal.user_id}",
        limit=PASSWORD_CONFIRM_FAIL_LIMIT,
        window=PASSWORD_CONFIRM_FAIL_WINDOW_SECONDS,
    )
    if left is not None:
        raise ApiError(429, "too_many_attempts", retry_after_seconds=left)
    async with deps.database.tenant(principal.tenant_id) as scope:
        user = await scope.users.get(principal.user_id)
    if user is None:
        raise ApiError(401, "unauthenticated")
    if not await verify_password_async(body.password, user.password_hash):
        async with deps.database.tenant(principal.tenant_id) as scope:
            await scope.owner_events.add(
                kind=OwnerEventKind.PASSWORD_CONFIRM_FAILED, user_id=user.id, ip=client_ip(request)
            )
            await scope.commit()
        # 403, а не 401: интерфейс на `unauthenticated` уводит на вход
        raise ApiError(403, "password_invalid")
    await deps.state.clear_password_failures(user.id)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await deps.state.mark_password_confirmed(token)
    return Response(status_code=204)


@router.post("/password-reset", status_code=204)
async def password_reset(body: PasswordResetIn, request: Request, deps: Deps) -> Response:
    left = await deps.state.hit(
        f"reset_try:{client_ip(request)}",
        limit=RESET_IP_LIMIT,
        window=RESET_IP_WINDOW_SECONDS,
    )
    if left is not None:
        raise ApiError(429, "too_many_attempts", retry_after_seconds=left)
    # Хеш до `GETDEL`: отказ очереди хеширования не сжигает одноразовый токен.
    new_hash = await hash_password_async(body.new_password)
    principal = await deps.state.take_reset_token(body.token)
    if principal is None:
        raise ApiError(400, "invalid_token")
    async with deps.database.tenant(principal.tenant_id) as scope:
        user = await scope.users.get(principal.user_id)
        if user is None:
            raise ApiError(400, "invalid_token")
        user.password_hash = new_hash
        await scope.commit()
    await deps.state.revoke_user_sessions(user.id)
    return Response(status_code=204)
