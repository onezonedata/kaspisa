"""Т18-Т29: магазины - добавление, список, изоляция, доступы, настройки, остановка, удаление."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from api_testing import (
    KASPI_LOGIN,
    KASPI_PASSWORD,
    ROUTES,
    FakeClock,
    api_cipher,
    api_database_url,
    deps,
    make_api,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_change,
    seed_price_write,
    seed_product,
    seed_shop,
    signed_in,
    sql_exec,
    sql_scalar,
    tenant_of,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps, create_app
from kaspisa.db import TenantId

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

SHOP_ID_ROUTES = [r for r in ROUTES if "{shop_id}" in r.path]


def _clock(deps: ApiDeps) -> FakeClock:
    assert isinstance(deps.clock, FakeClock)
    return deps.clock


async def _shop_row(engine: AsyncEngine, shop_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT * FROM shop WHERE id = :id"), {"id": shop_id})
        return dict(row.mappings().one())


async def test_add_shop(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)

    response = await client.post(
        "/api/shops", json={"login": KASPI_LOGIN, "password": KASPI_PASSWORD}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["connection_state"] == "not_connected"
    assert body["merchant_uid"] is None
    assert body["restart_required"] is False
    assert body["resend_available_in_seconds"] is None
    assert KASPI_LOGIN not in response.text
    assert KASPI_PASSWORD not in response.text
    tenant_id = await tenant_of(raw_engine, "a@example.com")
    async with deps.database.tenant(TenantId(tenant_id)) as scope:
        credentials = await scope.shops.get_credentials(uuid.UUID(body["id"]))
    assert credentials.login.reveal() == KASPI_LOGIN
    assert credentials.password.reveal() == KASPI_PASSWORD


async def test_new_shop_gets_default_city_astana(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т61: новый магазин сверяет по Астане, выбор продавца заменяет город (ADR 0032)."""
    client = await signed_in(deps)

    created = await client.post(
        "/api/shops", json={"login": KASPI_LOGIN, "password": KASPI_PASSWORD}
    )

    assert created.status_code == 201
    assert created.json()["city_id"] == "710000000"
    shop_id = uuid.UUID(created.json()["id"])
    assert (await _shop_row(raw_engine, shop_id))["city_id"] == "710000000"
    patched = await client.patch(f"/api/shops/{shop_id}/settings", json={"city_id": "750000000"})
    fetched = await client.get(f"/api/shops/{shop_id}")
    assert patched.status_code == 200
    assert patched.json()["city_id"] == "750000000"
    assert fetched.json()["city_id"] == "750000000"
    assert (await _shop_row(raw_engine, shop_id))["city_id"] == "750000000"


async def test_city_choice_is_never_overwritten(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т62: сервер не меняет выбранный город ни при повторном добавлении, ни при очистке."""
    client = await signed_in(deps)
    shop_id = await seed_shop(
        deps,
        raw_engine,
        await tenant_of(raw_engine, "a@example.com"),
        state="connected",
        merchant_uid="M1",
        city_id="591010000",
    )

    repeated = await client.post(
        "/api/shops", json={"login": KASPI_LOGIN, "password": KASPI_PASSWORD}
    )
    after_repeat = await client.get(f"/api/shops/{shop_id}")
    cleared = await client.patch(f"/api/shops/{shop_id}/settings", json={"city_id": None})
    after_clear = await client.get(f"/api/shops/{shop_id}")

    assert repeated.status_code == 409
    assert repeated.json()["code"] == "shop_limit_reached"
    assert after_repeat.json()["city_id"] == "591010000"
    assert cleared.json()["city_id"] is None
    assert after_clear.json()["city_id"] is None
    assert (await _shop_row(raw_engine, shop_id))["city_id"] is None


async def test_list_and_get_shops(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client_a = await signed_in(deps, "a@example.com")
    client_b = await signed_in(deps, "b@example.com")
    tenant_a = await tenant_of(raw_engine, "a@example.com")
    tenant_b = await tenant_of(raw_engine, "b@example.com")
    first = await seed_shop(deps, raw_engine, tenant_a)
    second = await seed_shop(deps, raw_engine, tenant_a)
    foreign = await seed_shop(deps, raw_engine, tenant_b)

    listed = await client_a.get("/api/shops")
    one = await client_a.get(f"/api/shops/{second}")
    foreign_listed = await client_b.get("/api/shops")

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [str(first), str(second)]
    assert str(foreign) not in listed.text
    assert one.status_code == 200
    assert one.json()["id"] == str(second)
    assert [item["id"] for item in foreign_listed.json()] == [str(foreign)]


async def test_every_shop_route_hides_foreign_shops(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    app = create_app(deps)
    expected = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "{shop_id}" in path
        for method in operations
    }
    assert {(r.method, r.path) for r in SHOP_ID_ROUTES} == expected
    await signed_in(deps, "a@example.com")
    client_b = await signed_in(deps, "b@example.com")
    tenant_a = await tenant_of(raw_engine, "a@example.com")
    shop_a = await seed_shop(
        deps,
        raw_engine,
        tenant_a,
        state="connected",
        merchant_uid="M1",
        version=2,
        halt_reason="write_errors",
        city_id="750000000",
    )
    product = await seed_product(raw_engine, tenant_a, shop_a)
    await seed_change(raw_engine, tenant_a, shop_a, product)
    before = await _shop_row(raw_engine, shop_a)
    counts = "SELECT (SELECT count(*) FROM product WHERE shop_id = :s), "
    counts += "(SELECT count(*) FROM price_change WHERE shop_id = :s)"

    for spec in SHOP_ID_ROUTES:
        seen: list[dict[str, Any]] = []
        for shop_id in (shop_a, uuid.uuid4()):
            response = await client_b.request(spec.method, spec.url(shop_id), json=spec.body)
            assert response.status_code == 404, spec
            seen.append(response.json())
        not_found = {"code": "not_found", "retry_after_seconds": None, "fields": None}
        assert seen[0] == seen[1] == not_found

    assert await _shop_row(raw_engine, shop_a) == before
    async with raw_engine.connect() as conn:
        row = (await conn.execute(text(counts), {"s": shop_a})).one()
    assert tuple(row) == (1, 1)
    credentials_owner = deps.database.tenant(TenantId(tenant_a))
    async with credentials_owner as scope:
        assert (await scope.shops.get_credentials(shop_a)).login.reveal() == KASPI_LOGIN


async def test_change_credentials(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant_id = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(
        deps, raw_engine, tenant_id, state="connected", merchant_uid="M1", version=3
    )

    response = await client.put(
        f"/api/shops/{shop_id}/credentials",
        json={"login": "new-login@shop.kz", "password": "NEW-KASPI-PASS"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connection_state"] == "not_connected"
    assert body["connection_error"] is None
    assert body["merchant_uid"] == "M1"
    assert "NEW-KASPI-PASS" not in response.text
    row = await _shop_row(raw_engine, shop_id)
    assert row["connection_version"] == 4
    async with deps.database.tenant(TenantId(tenant_id)) as scope:
        credentials = await scope.shops.get_credentials(shop_id)
    assert credentials.login.reveal() == "new-login@shop.kz"
    assert credentials.password.reveal() == "NEW-KASPI-PASS"


async def test_update_settings_partially(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    shop_id = await seed_shop(deps, raw_engine, await tenant_of(raw_engine, "a@example.com"))

    first = await client.patch(
        f"/api/shops/{shop_id}/settings",
        json={
            "strategy": "hold_second",
            "city_id": "750000000",
            "excluded_merchant_ids": ["m1", "m1", "m2"],
            "default_step": 5,
            "auto_raise_enabled": False,
            "bot_mode": "live",
        },
    )
    second = await client.patch(f"/api/shops/{shop_id}/settings", json={"default_step": 7})

    assert first.status_code == 200
    expected = {
        "strategy": "hold_second",
        "city_id": "750000000",
        "excluded_merchant_ids": ["m1", "m2"],
        "default_step": 5,
        "auto_raise_enabled": False,
        "bot_mode": "live",
    }
    assert {key: first.json()[key] for key in expected} == expected
    assert second.status_code == 200
    assert {key: second.json()[key] for key in expected} == {**expected, "default_step": 7}


async def test_settings_validation(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    shop_id = await seed_shop(
        deps, raw_engine, await tenant_of(raw_engine, "a@example.com"), city_id=None
    )
    url = f"/api/shops/{shop_id}/settings"
    before = await _shop_row(raw_engine, shop_id)

    bad_bodies: list[dict[str, Any]] = [
        {"default_step": 0},
        {"strategy": "foo"},
        {"below_min": "drop_to_min"},  # настройки больше нет (ADR 0020)
        {"strategy": "be_first", "below_min": "do_nothing"},
        {},
        {"bot_mode": None},
        {"bot_mode": "on"},
        {"bot_enabled": True},  # поля больше нет (ADR 0024)
        {"excluded_merchant_ids": [str(n) for n in range(201)]},
    ]
    for body in bad_bodies:
        response = await client.patch(url, json=body)
        assert response.status_code == 422, body
    no_city = await client.patch(url, json={"bot_mode": "dry_run"})
    cleared = await client.patch(url, json={"city_id": None, "bot_mode": "off"})

    assert no_city.status_code == 422
    assert no_city.json()["code"] == "validation_error"
    assert no_city.json()["fields"] == ["body.city_id"]
    assert await _shop_row(raw_engine, shop_id) == before
    assert cleared.status_code == 200
    assert cleared.json()["city_id"] is None


async def test_reasons_are_shown_as_codes(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    halted_at = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    shop_id = await seed_shop(
        deps,
        raw_engine,
        await tenant_of(raw_engine, "a@example.com"),
        state="reauth_required",
        error="session_expired",
        halt_reason="write_errors",
        halted_at=halted_at,
    )

    body = (await client.get(f"/api/shops/{shop_id}")).json()

    assert body["connection_state"] == "reauth_required"
    assert body["connection_error"] == "session_expired"
    assert body["halt_reason"] == "write_errors"
    assert datetime.fromisoformat(body["halted_at"]) == halted_at


async def test_awaiting_code_shows_resend_timer_and_restart_mark(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    now = _clock(deps).now
    shop_id = await seed_shop(
        deps,
        raw_engine,
        await tenant_of(raw_engine, "a@example.com"),
        state="awaiting_code",
        version=1,
        changed_at=now - timedelta(seconds=60),
    )

    before = (await client.get(f"/api/shops/{shop_id}")).json()
    await deps.state.mark_restart(shop_id, 1)
    marked = (await client.get(f"/api/shops/{shop_id}")).json()
    await deps.state.mark_restart(shop_id, 0)
    stale = (await client.get(f"/api/shops/{shop_id}")).json()

    assert before["resend_available_in_seconds"] == 120
    assert before["restart_required"] is False
    assert marked["restart_required"] is True
    assert stale["restart_required"] is False


async def test_clear_halt(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    shop_id = await seed_shop(
        deps,
        raw_engine,
        await tenant_of(raw_engine, "a@example.com"),
        state="connected",
        merchant_uid="M1",
        halt_reason="store_anomalies",
        login_streak=2,
        write_streak=3,
    )

    first = await client.post(f"/api/shops/{shop_id}/halt/clear")
    second = await client.post(f"/api/shops/{shop_id}/halt/clear")

    for response in (first, second):
        assert response.status_code == 200
        assert response.json()["halt_reason"] is None
        assert response.json()["halted_at"] is None
    row = await _shop_row(raw_engine, shop_id)
    assert (row["login_error_streak"], row["write_error_streak"]) == (0, 0)


async def test_delete_shop_removes_its_data(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant_id = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant_id)
    for _ in range(2):
        product = await seed_product(raw_engine, tenant_id, shop_id)
    for _ in range(3):
        await seed_change(raw_engine, tenant_id, shop_id, product)

    response = await client.delete(f"/api/shops/{shop_id}")

    assert response.status_code == 204
    assert (await client.get(f"/api/shops/{shop_id}")).status_code == 404
    for table in ("product", "price_change"):
        count = await sql_scalar(
            raw_engine, f"SELECT count(*) FROM {table} WHERE shop_id = :s", s=shop_id
        )
        assert count == 0


async def test_delete_blocked_by_open_price_write(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    tenant_id = await tenant_of(raw_engine, "a@example.com")
    now = _clock(deps).now
    cases: list[tuple[str, int, int | None]] = [
        ("receipt", 100, 500),
        ("receipt", 599, 1),
        ("unknown", 300, 300),
        ("receipt", 600, None),
        ("confirmed", 10, None),
    ]
    for status, age, retry in cases:
        shop_id = await seed_shop(deps, raw_engine, tenant_id)
        product = await seed_product(raw_engine, tenant_id, shop_id)
        await seed_price_write(
            raw_engine,
            tenant_id,
            shop_id,
            product,
            status=status,
            sent_at=now - timedelta(seconds=age),
        )

        response = await client.delete(f"/api/shops/{shop_id}")

        if retry is None:
            assert response.status_code == 204, (status, age)
        else:
            assert response.status_code == 409, (status, age)
            assert response.json()["code"] == "price_write_open"
            assert response.json()["retry_after_seconds"] == retry
            assert (await client.get(f"/api/shops/{shop_id}")).status_code == 200


async def test_settings_do_not_overwrite_worker_columns(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    claimed = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    later = claimed + timedelta(minutes=5)
    shop_id = await seed_shop(
        deps,
        raw_engine,
        await tenant_of(raw_engine, "a@example.com"),
        state="connected",
        merchant_uid="M1",
        claimed_by="w1",
        claimed_until=claimed,
        halt_reason="login_errors",
        login_streak=3,
    )

    async with raw_engine.connect() as conn:
        await conn.execute(
            text("UPDATE shop SET claimed_until = :u WHERE id = :id"), {"u": later, "id": shop_id}
        )
        request = asyncio.create_task(
            client.patch(f"/api/shops/{shop_id}/settings", json={"default_step": 2})
        )
        await asyncio.sleep(0.3)
        assert not request.done()  # запрос ждёт блокировку строки
        await conn.commit()
    response = await asyncio.wait_for(request, 5)

    assert response.status_code == 200
    row = await _shop_row(raw_engine, shop_id)
    assert row["default_step"] == 2
    assert row["claimed_until"] == later
    assert row["claimed_by"] == "w1"
    assert row["halt_reason"] == "login_errors"
    assert row["login_error_streak"] == 3


async def test_settings_wait_for_shop_row_lock(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т83: S5 берёт блокировку строки и видит свежий `city_id`."""
    client = await signed_in(deps)
    tenant_id = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant_id, city_id="750000000", bot_mode="off")

    async with deps.database.tenant(TenantId(tenant_id)) as scope:
        shop = await scope.shops.lock_for_write(shop_id)
        shop.city_id = None
        request = asyncio.create_task(
            client.patch(f"/api/shops/{shop_id}/settings", json={"bot_mode": "live"})
        )
        await asyncio.sleep(0.2)
        assert not request.done()  # запрос ждёт блокировку строки
        await scope.commit()
    response = await asyncio.wait_for(request, 5)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    assert response.json()["fields"] == ["body.city_id"]
    row = await _shop_row(raw_engine, shop_id)
    assert (row["bot_mode"], row["city_id"]) == ("off", None)


async def test_change_credentials_blocked_by_open_price_write(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    tenant_id = await tenant_of(raw_engine, "a@example.com")
    now = _clock(deps).now
    shop_id = await seed_shop(
        deps, raw_engine, tenant_id, state="connected", merchant_uid="M1", version=3
    )
    product = await seed_product(raw_engine, tenant_id, shop_id)
    await seed_price_write(
        raw_engine,
        tenant_id,
        shop_id,
        product,
        status="sending",
        sent_at=now - timedelta(seconds=30),
    )
    body = {"login": "new-login@shop.kz", "password": "NEW-KASPI-PASS"}
    url = f"/api/shops/{shop_id}/credentials"

    blocked = await client.put(url, json=body)

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "price_write_open"
    assert blocked.json()["retry_after_seconds"] == 570
    row = await _shop_row(raw_engine, shop_id)
    assert (row["connection_state"], row["connection_version"]) == ("connected", 3)
    async with deps.database.tenant(TenantId(tenant_id)) as scope:
        assert (await scope.shops.get_credentials(shop_id)).login.reveal() == KASPI_LOGIN
    await sql_exec(
        raw_engine,
        "UPDATE price_write SET sent_at = :at WHERE shop_id = :s",
        at=now - timedelta(seconds=600),
        s=shop_id,
    )
    allowed = await client.put(url, json=body)
    assert allowed.status_code == 200


async def test_shop_requires_session(deps: ApiDeps) -> None:
    anonymous = make_api(deps)

    for spec in SHOP_ID_ROUTES + [r for r in ROUTES if r.path == "/api/shops"]:
        response = await anonymous.request(spec.method, spec.url(uuid.uuid4()), json=spec.body)
        assert response.status_code == 401, spec
