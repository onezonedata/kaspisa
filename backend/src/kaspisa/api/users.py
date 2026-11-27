"""Маршруты /api/users: список, добавление и удаление пользователей (ADR 0039).

Все маршруты - только для администратора. Порядок проверок: сессия, роль (403
`admin_required`), тело и путь (422), окно повторного пароля (403 `password_required`).
Токен задания пароля отдаётся один раз и нигде не пишется: ни в журнал, ни в логи, ни в Telegram.
"""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Request, Response

from kaspisa.api.auth import client_ip
from kaspisa.api.deps import (
    RESET_TOKEN_TTL_SECONDS,
    USERS_ADD_LIMIT,
    USERS_ADD_WINDOW_SECONDS,
    AdminPrincipal,
    Deps,
)
from kaspisa.api.errors import ApiError
from kaspisa.api.guard import ensure_password_confirmed
from kaspisa.api.notifications import notify_owner
from kaspisa.api.passwords import hash_password_async
from kaspisa.api.schemas import UserAddedOut, UserAddIn, UserAdminOut
from kaspisa.api.telegram import text_user_added, text_user_removed
from kaspisa.db import EmailTakenError, OwnerEventKind, User

router = APIRouter(prefix="/api/users")


def _out(user: User) -> UserAdminOut:
    return UserAdminOut(
        id=user.id, email=user.email, is_admin=user.is_admin, created_at=user.created_at
    )


@router.get("")
async def list_users(principal: AdminPrincipal, deps: Deps) -> list[UserAdminOut]:
    async with deps.database.tenant(principal.tenant_id) as scope:
        users = await scope.users.list()
    return [_out(user) for user in users]


@router.post("", status_code=201)
async def add_user(
    body: UserAddIn, request: Request, principal: AdminPrincipal, deps: Deps
) -> UserAddedOut:
    await ensure_password_confirmed(request, deps)
    # Счётчик растёт до хеширования: предел защищает очередь scrypt.
    left = await deps.state.hit(
        f"users_add:{principal.user_id}", limit=USERS_ADD_LIMIT, window=USERS_ADD_WINDOW_SECONDS
    )
    if left is not None:
        raise ApiError(429, "too_many_attempts", retry_after_seconds=left)
    # Пароль непригоден для входа; настоящий хеш, а не константа: время ответа `login` не
    # выдаёт приглашённых, ещё не задавших пароль.
    unusable = await hash_password_async(secrets.token_urlsafe(32))
    async with deps.database.tenant(principal.tenant_id) as scope:
        try:
            user = await scope.users.add(email=body.email, password_hash=unusable)
        except EmailTakenError:
            raise ApiError(409, "email_taken") from None
        await scope.owner_events.add(
            kind=OwnerEventKind.USER_ADDED,
            user_id=principal.user_id,
            ip=client_ip(request),
            new_value=user.email,
        )
        # Токен выдаётся до commit: при сбое Redis транзакция откатывается и пользователя нет.
        token = await deps.state.put_reset_token(user.id, principal.tenant_id)
        await scope.commit()
        added = _out(user)
    notify_owner(deps, principal.tenant_id, text_user_added(added.email))
    return UserAddedOut(user=added, reset_token=token, expires_in_seconds=RESET_TOKEN_TTL_SECONDS)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID, request: Request, principal: AdminPrincipal, deps: Deps
) -> Response:
    await ensure_password_confirmed(request, deps)
    if user_id == principal.user_id:
        raise ApiError(409, "cannot_delete_self")
    async with deps.database.tenant(principal.tenant_id) as scope:
        target = await scope.users.get(user_id)  # чужой арендатор - тоже None
        if target is None:
            raise ApiError(404, "not_found")
        email = target.email
        # Условие `NOT is_admin` внутри `DELETE`; `False` - двойное удаление.
        if not await scope.users.delete_member(user_id):
            raise ApiError(404, "not_found")
        await scope.owner_events.add(
            kind=OwnerEventKind.USER_REMOVED,
            user_id=principal.user_id,
            ip=client_ip(request),
            old_value=email,
        )
        await scope.commit()
    notify_owner(deps, principal.tenant_id, text_user_removed(email))
    # Отзыв после commit; главная защита - проверка пользователя в `current_principal`.
    await deps.state.revoke_user_sessions(user_id)
    return Response(status_code=204)
