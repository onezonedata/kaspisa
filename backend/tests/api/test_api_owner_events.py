"""ADR 0024, Т24: события входа и чтение журнала действий владельца."""

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
    sql_exec,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

FIELDS = {
    "id",
    "kind",
    "ip",
    "shop_id",
    "sku",
    "old_value",
    "new_value",
    "created_at",
    "user_email",
}


async def test_register_and_login_write_login_events(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps, ip="10.1.2.3")
    other = make_api(deps, ip="10.9.9.9")
    wrong = await other.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "not-the-password-1"}
    )
    assert wrong.status_code == 401  # неудачный вход события не пишет
    ok = await other.post(
        "/api/auth/login", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )
    assert ok.status_code == 200

    events = (await client.get("/api/events")).json()
    assert [(e["kind"], e["ip"]) for e in events] == [("login", "10.9.9.9"), ("login", "10.1.2.3")]
    for event in events:
        assert set(event) == FIELDS
        assert event["created_at"]
        assert (event["shop_id"], event["sku"], event["old_value"], event["new_value"]) == (
            None,
            None,
            None,
            None,
        )


async def test_events_are_tenant_scoped_newest_first_and_capped(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client_a = await signed_in(deps, "a@example.com")
    client_b = await signed_in(deps, "b@example.com")
    tenant_a = await tenant_of(raw_engine, "a@example.com")
    await sql_exec(
        raw_engine,
        "INSERT INTO owner_event (tenant_id, user_id, user_email, kind, ip, created_at) "
        "SELECT :t, (SELECT id FROM app_user WHERE tenant_id = :t), "
        "(SELECT email FROM app_user WHERE tenant_id = :t), 'login', 'ip-' || n, "
        "now() + n * interval '1 second' FROM generate_series(1, 205) AS n",
        t=tenant_a,
    )
    events_a = (await client_a.get("/api/events")).json()
    events_b = (await client_b.get("/api/events")).json()
    assert len(events_a) == 200
    assert events_a[0]["ip"] == "ip-205"
    assert [e["id"] for e in events_a] == sorted((e["id"] for e in events_a), reverse=True)
    # Второй пользователь создан через слой данных (ADR 0027): события входа у него нет,
    # чужих событий тоже.
    assert events_b == []
    anonymous = make_api(deps)
    assert (await anonymous.get("/api/events")).status_code == 401


async def test_user_events_carry_author_email_and_outlive_the_user(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т27: `user_added` и `user_removed` в выдаче; авторство события живёт дольше пользователя."""
    admin = await signed_in(deps, "admin@example.com")
    await confirm_password(admin)
    added = await admin.post("/api/users", json={"email": "new@example.com"})
    assert added.status_code == 201
    token = added.json()["reset_token"]
    staff = make_api(deps, ip="10.4.4.4")
    reset = await staff.post(
        "/api/auth/password-reset", json={"token": token, "new_password": SERVICE_PASSWORD}
    )
    assert reset.status_code == 204
    login = await staff.post(
        "/api/auth/login", json={"email": "new@example.com", "password": SERVICE_PASSWORD}
    )
    assert login.status_code == 200
    staff_id = login.json()["id"]
    assert (await admin.delete(f"/api/users/{staff_id}")).status_code == 204

    events = (await admin.get("/api/events")).json()

    by_kind = {(e["kind"], e["user_email"]): e for e in events}
    assert set(by_kind) == {
        ("login", "admin@example.com"),
        ("user_added", "admin@example.com"),
        ("user_removed", "admin@example.com"),
        ("login", "new@example.com"),
    }
    assert by_kind[("user_added", "admin@example.com")]["new_value"] == "new@example.com"
    assert by_kind[("user_removed", "admin@example.com")]["old_value"] == "new@example.com"
    assert all(set(e) == FIELDS for e in events)
