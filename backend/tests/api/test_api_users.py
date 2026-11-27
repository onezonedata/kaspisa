"""Т7-Т21, Т23, Т41, Т42 этапа 19: администратор управляет пользователями (ADR 0039)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import httpx
import pytest
import respx
from api_notify_testing import T, notifier_of, owner, sent_messages, url_of
from api_testing import (
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    confirm_password,
    deps,
    make_api,
    raw_engine,
    redis_client,
    redis_prefix,
    signed_in,
    signed_in_member,
    sql_exec,
    sql_scalar,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps, users
from kaspisa.api.deps import SESSION_COOKIE
from kaspisa.api.reset_token import issue_reset_token
from kaspisa.api.telegram import text_user_added, text_user_removed
from kaspisa.db.repositories import UserRepository

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

ADMIN = "admin@example.com"
STAFF = "staff@example.com"
NEW = "new@example.com"
USER_FIELDS = {"id", "email", "is_admin", "created_at"}
ADDED_FIELDS = {"user", "reset_token", "expires_in_seconds"}


async def _counts(raw: AsyncEngine) -> tuple[int, int]:
    return (
        int(await sql_scalar(raw, "SELECT count(*) FROM app_user")),
        int(await sql_scalar(raw, "SELECT count(*) FROM owner_event")),
    )


async def _user_count(raw: AsyncEngine, email: str) -> int:
    return int(await sql_scalar(raw, "SELECT count(*) FROM app_user WHERE email = :e", e=email))


async def _user_id(raw: AsyncEngine, email: str) -> uuid.UUID:
    found = await sql_scalar(raw, "SELECT id FROM app_user WHERE email = :e", e=email)
    assert isinstance(found, uuid.UUID)
    return found


async def _events(raw: AsyncEngine, kind: str) -> list[tuple[Any, ...]]:
    async with raw.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT user_email, new_value, old_value, ip, shop_id FROM owner_event "
                "WHERE kind = :k ORDER BY id"
            ),
            {"k": kind},
        )
        return [tuple(row) for row in rows]


# --- Т7. Роль в ответах ---


async def test_responses_carry_is_admin(deps: ApiDeps) -> None:
    client = make_api(deps)
    registered = await client.post(
        "/api/auth/register", json={"email": ADMIN, "password": SERVICE_PASSWORD}
    )
    assert registered.status_code == 201
    assert registered.json()["is_admin"] is True
    assert (await client.get("/api/auth/me")).json()["is_admin"] is True
    staff, _ = await signed_in_member(deps, ADMIN, STAFF)
    assert (await staff.get("/api/auth/me")).json()["is_admin"] is False
    login = await make_api(deps).post(
        "/api/auth/login", json={"email": STAFF, "password": SERVICE_PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["is_admin"] is False
    admin_login = await make_api(deps).post(
        "/api/auth/login", json={"email": ADMIN, "password": SERVICE_PASSWORD}
    )
    assert admin_login.json()["is_admin"] is True


# --- Т8. Права обычного пользователя ---


async def test_member_gets_admin_required_before_password_and_body(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    admin, tenant = await owner(deps, raw_engine, ADMIN)
    staff, staff_id = await signed_in_member(deps, ADMIN, STAFF)
    admin_id = await _user_id(raw_engine, ADMIN)
    before = await _counts(raw_engine)

    async def attempts() -> None:
        calls = [
            staff.get("/api/users"),
            staff.post("/api/users", json={"email": NEW}),
            staff.post("/api/users", json={"email": "not-an-email"}),
            staff.delete(f"/api/users/{admin_id}"),
            staff.delete(f"/api/users/{uuid.uuid4()}"),
            staff.delete(f"/api/users/{staff_id}"),
        ]
        for call in calls:
            response = await call
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "admin_required"

    with respx.mock(assert_all_called=False) as mock:
        await attempts()  # без окна пароля: не `password_required`
        await confirm_password(staff)
        await attempts()  # с открытым окном
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []
    assert await _counts(raw_engine) == before
    assert (await staff.get("/api/auth/me")).status_code == 200
    assert admin is not None
    assert tenant is not None


# --- Т9. Без сессии ---


async def test_anonymous_gets_401(deps: ApiDeps) -> None:
    client = make_api(deps)
    for response in (
        await client.get("/api/users"),
        await client.post("/api/users", json={"email": NEW}),
        await client.delete(f"/api/users/{uuid.uuid4()}"),
    ):
        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"


# --- Т10. Список ---


async def test_list_users_is_ordered_and_tenant_scoped(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    admin = await signed_in(deps, ADMIN)
    await signed_in_member(deps, ADMIN, STAFF)
    await signed_in_member(deps, ADMIN, "staff2@example.com")
    await signed_in(deps, "b@example.com")

    response = await admin.get("/api/users")

    assert response.status_code == 200
    items = response.json()
    assert [(u["email"], u["is_admin"]) for u in items] == [
        (ADMIN, True),
        (STAFF, False),
        ("staff2@example.com", False),
    ]
    assert all(set(u) == USER_FIELDS for u in items)
    assert "b@example.com" not in response.text


# --- Т11. Окно пароля ---


async def test_add_requires_password_window(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    admin = await signed_in(deps, ADMIN)
    before = await _counts(raw_engine)

    response = await admin.post("/api/users", json={"email": NEW})

    assert response.status_code == 403
    assert response.json()["code"] == "password_required"
    assert await _counts(raw_engine) == before
    assert await _events(raw_engine, "user_added") == []


# --- Т12. Успешное добавление ---


async def test_add_user_flow(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    admin, _ = await owner(deps, raw_engine, ADMIN, ip="10.1.1.1", confirmed=True)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        response = await admin.post("/api/users", json={"email": " New@Example.com "})
        await notifier_of(deps).drain()
        messages = sent_messages(mock)

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == ADDED_FIELDS
    assert set(body["user"]) == USER_FIELDS
    assert body["user"]["email"] == NEW
    assert body["user"]["is_admin"] is False
    assert body["expires_in_seconds"] == 3600
    token = body["reset_token"]
    assert await _events(raw_engine, "user_added") == [(ADMIN, NEW, None, "10.1.1.1", None)]
    assert [m.text for m in messages] == [text_user_added(NEW)]
    assert token not in messages[0].text

    anonymous = make_api(deps)
    reset = await anonymous.post(
        "/api/auth/password-reset", json={"token": token, "new_password": SERVICE_PASSWORD}
    )
    assert reset.status_code == 204
    login = await anonymous.post(
        "/api/auth/login", json={"email": NEW, "password": SERVICE_PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["is_admin"] is False
    again = await anonymous.post(
        "/api/auth/password-reset", json={"token": token, "new_password": SERVICE_PASSWORD}
    )
    assert again.status_code == 400
    assert again.json()["code"] == "invalid_token"


# --- Т13. Занятый адрес и неверный e-mail ---


async def test_add_taken_email_and_invalid_email(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    admin, _ = await owner(deps, raw_engine, ADMIN, confirmed=True)
    await signed_in_member(deps, ADMIN, STAFF)
    await signed_in(deps, "b@example.com")
    before = await _counts(raw_engine)

    with respx.mock(assert_all_called=False) as mock:
        for email in (STAFF, "B@Example.com"):
            response = await admin.post("/api/users", json={"email": email})
            assert response.status_code == 409
            assert response.json()["code"] == "email_taken"
        invalid = await admin.post("/api/users", json={"email": "nope"})
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []

    assert invalid.status_code == 422
    assert await _counts(raw_engine) == before


# --- Т14. Сбой Redis при выдаче токена ---


async def test_add_rolls_back_when_token_cannot_be_stored(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await signed_in(deps, ADMIN)
    await confirm_password(admin)
    before = await _counts(raw_engine)

    async def broken(*_: object) -> str:
        raise RuntimeError("redis down")

    monkeypatch.setattr(deps.state, "put_reset_token", broken)
    response = await admin.post("/api/users", json={"email": NEW})

    assert response.status_code == 500
    assert await _counts(raw_engine) == before


# --- Т15. Приглашённый не входит до задания пароля ---


async def test_invited_user_cannot_log_in_before_setting_password(deps: ApiDeps) -> None:
    admin = await signed_in(deps, ADMIN)
    await confirm_password(admin)
    assert (await admin.post("/api/users", json={"email": NEW})).status_code == 201
    anonymous = make_api(deps)

    for password in (SERVICE_PASSWORD, "!", "x" * 12):
        credentials = {"email": NEW, "password": password}
        response = await anonymous.post("/api/auth/login", json=credentials)
        assert response.status_code == 401


# --- Т16. Удаление ---


async def test_delete_user_revokes_everything(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_prefix: str
) -> None:
    admin, _ = await owner(deps, raw_engine, ADMIN, ip="10.2.2.2", confirmed=True)
    first, staff_id = await signed_in_member(deps, ADMIN, STAFF)
    second = make_api(deps, ip="10.3.3.3")
    credentials = {"email": STAFF, "password": SERVICE_PASSWORD}
    login = await second.post("/api/auth/login", json=credentials)
    assert login.status_code == 200
    devices_key = f"{redis_prefix}:api:devices:{staff_id}"
    assert await deps.redis.exists(devices_key)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        response = await admin.delete(f"/api/users/{staff_id}")
        await notifier_of(deps).drain()
        messages = sent_messages(mock)

    assert response.status_code == 204
    assert await _user_count(raw_engine, STAFF) == 0
    assert await _events(raw_engine, "user_removed") == [(ADMIN, None, STAFF, "10.2.2.2", None)]
    assert [m.text for m in messages if "удалён" in m.text] == [text_user_removed(STAFF)]
    for client in (first, second):
        assert (await client.get("/api/auth/me")).status_code == 401
        assert (await client.get("/api/shops")).status_code == 401
    old_login = await make_api(deps).post(
        "/api/auth/login", json={"email": STAFF, "password": SERVICE_PASSWORD}
    )
    assert old_login.status_code == 401
    assert not await deps.redis.exists(devices_key)
    assert (await admin.get("/api/auth/me")).status_code == 200


# --- Т17-Т19. Отказы при удалении ---


async def test_delete_requires_password_window(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    admin = await signed_in(deps, ADMIN)
    staff, staff_id = await signed_in_member(deps, ADMIN, STAFF)
    before = await _counts(raw_engine)

    response = await admin.delete(f"/api/users/{staff_id}")

    assert response.status_code == 403
    assert response.json()["code"] == "password_required"
    assert await _counts(raw_engine) == before
    assert (await staff.get("/api/auth/me")).status_code == 200


async def test_admin_cannot_delete_self(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    admin = await signed_in(deps, ADMIN)
    await confirm_password(admin)
    admin_id = await _user_id(raw_engine, ADMIN)
    before = await _counts(raw_engine)

    response = await admin.delete(f"/api/users/{admin_id}")

    assert response.status_code == 409
    assert response.json()["code"] == "cannot_delete_self"
    assert await _counts(raw_engine) == before
    assert (await admin.get("/api/auth/me")).status_code == 200


async def test_delete_foreign_unknown_and_repeated(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    admin = await signed_in(deps, ADMIN)
    other = await signed_in(deps, "b@example.com")
    await confirm_password(admin)
    other_id = (await other.get("/api/auth/me")).json()["id"]
    _, staff_id = await signed_in_member(deps, ADMIN, STAFF)

    foreign = await admin.delete(f"/api/users/{other_id}")
    unknown = await admin.delete(f"/api/users/{uuid.uuid4()}")
    first = await admin.delete(f"/api/users/{staff_id}")
    repeated = await admin.delete(f"/api/users/{staff_id}")

    for response in (foreign, unknown, repeated):
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
    assert first.status_code == 204
    assert (await other.get("/api/auth/me")).status_code == 200
    assert await _user_count(raw_engine, "b@example.com") == 1
    assert (await admin.delete("/api/users/not-a-uuid")).status_code == 422


# --- Т20, Т21. Мёртвая сессия ---


async def test_session_of_user_deleted_in_database_is_dead(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_prefix: str
) -> None:
    await signed_in(deps, ADMIN)
    staff, staff_id = await signed_in_member(deps, ADMIN, STAFF)
    token = staff.cookies[SESSION_COOKIE]
    session_key = f"{redis_prefix}:api:sess:{hashlib.sha256(token.encode()).hexdigest()}"
    await sql_exec(raw_engine, "DELETE FROM app_user WHERE id = :i", i=staff_id)
    # срок меньше порога продления: у живой сессии ответ нёс бы новый Set-Cookie
    await deps.redis.expire(session_key, 1000)

    response = await staff.get("/api/shops")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert not await deps.redis.exists(session_key)


async def test_revoke_failure_still_leaves_deleted_user_locked_out(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, _ = await owner(deps, raw_engine, ADMIN, confirmed=True)
    staff, staff_id = await signed_in_member(deps, ADMIN, STAFF)

    async def broken(*_: object) -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr(deps.state, "revoke_user_sessions", broken)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        response = await admin.delete(f"/api/users/{staff_id}")
        await notifier_of(deps).drain()
        messages = sent_messages(mock)

    assert response.status_code == 500
    assert (await staff.get("/api/shops")).status_code == 401
    assert [m.text for m in messages] == [text_user_removed(STAFF)]
    assert await _user_count(raw_engine, STAFF) == 0
    assert len(await _events(raw_engine, "user_removed")) == 1


# --- Т23. Токен сброса удалённого ---


async def test_reset_token_issued_before_deletion_is_useless(deps: ApiDeps) -> None:
    admin = await signed_in(deps, ADMIN)
    _, staff_id = await signed_in_member(deps, ADMIN, STAFF)
    token = await issue_reset_token(deps, STAFF)
    assert token is not None
    await confirm_password(admin)
    assert (await admin.delete(f"/api/users/{staff_id}")).status_code == 204

    response = await make_api(deps).post(
        "/api/auth/password-reset", json={"token": token, "new_password": SERVICE_PASSWORD}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_token"


# --- Т41. Предел добавления ---


async def test_add_is_rate_limited(deps: ApiDeps, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = await signed_in(deps, ADMIN)
    await confirm_password(admin)
    calls = 0

    async def fast_hash(_: str) -> str:
        nonlocal calls
        calls += 1
        return "scrypt$stub"

    monkeypatch.setattr(users, "hash_password_async", fast_hash)
    for number in range(20):
        response = await admin.post("/api/users", json={"email": f"u{number}@example.com"})
        assert response.status_code == 201, response.text
    assert calls == 20

    blocked = await admin.post("/api/users", json={"email": "u99@example.com"})

    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_attempts"
    assert 1 <= blocked.json()["retry_after_seconds"] <= 3600
    assert calls == 20
    listing = (await admin.get("/api/users")).json()
    assert "u99@example.com" not in [u["email"] for u in listing]
    assert len(listing) == 21


# --- Т42. Гонка сброса пароля и удаления ---


async def test_password_reset_racing_with_delete_is_invalid_token(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await signed_in(deps, ADMIN)
    _, staff_id = await signed_in_member(deps, ADMIN, STAFF)
    token = await issue_reset_token(deps, STAFF)
    assert token is not None
    original = UserRepository.get

    async def get_then_delete(self: UserRepository, user_id: uuid.UUID) -> Any:
        user = await original(self, user_id)
        await sql_exec(raw_engine, "DELETE FROM app_user WHERE id = :i", i=staff_id)
        return user

    monkeypatch.setattr(UserRepository, "get", get_then_delete)
    response = await make_api(deps).post(
        "/api/auth/password-reset", json={"token": token, "new_password": SERVICE_PASSWORD}
    )

    assert isinstance(response, httpx.Response)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_token"
    assert await _user_count(raw_engine, STAFF) == 0
