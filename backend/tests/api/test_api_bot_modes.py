"""ADR 0024, Т14, Т15, Т17-Т23: режим бота в API, подтверждение пароля, защита границ, журнал."""

import hashlib
import uuid
from typing import Any

import httpx
from api_testing import (
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    deps,
    make_api,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_product,
    seed_shop,
    signed_in,
    sql_exec,
    sql_scalar,
    tenant_of,
)
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api.deps import SESSION_COOKIE

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

CITY = "750000000"


async def _events(engine: AsyncEngine) -> list[dict[str, Any]]:
    """События правок и паролей; вход при регистрации (`login`) не в счёт."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT kind, old_value, new_value, ip, shop_id, sku FROM owner_event "
                "WHERE kind <> 'login' ORDER BY id"
            )
        )
        return [dict(row) for row in rows.mappings()]


async def _setup(
    deps: ApiDeps, engine: AsyncEngine, *, mode: str = "off"
) -> tuple[httpx.AsyncClient, uuid.UUID, uuid.UUID]:
    client = await signed_in(deps)
    tenant = await tenant_of(engine, "a@example.com")
    shop = await seed_shop(deps, engine, tenant, city_id=CITY, bot_mode=mode)
    return client, tenant, shop


async def _product(
    engine: AsyncEngine,
    tenant: uuid.UUID,
    shop: uuid.UUID,
    *,
    low: int | None = 1000,
    high: int | None = 5000,
    anchor: int | None = 1000,
    current: int | None = 1000,
    **extra: Any,
) -> int:
    if current is not None:
        extra["current_price"] = current
    product = await seed_product(engine, tenant, shop, min_price=low, max_price=high, **extra)
    await sql_exec(
        engine, "UPDATE product SET min_price_anchor = :a WHERE id = :i", a=anchor, i=product
    )
    return product


async def _row(engine: AsyncEngine, product: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT min_price, max_price, min_price_anchor, tracked, step, decision_reason "
                "FROM product WHERE id = :i"
            ),
            {"i": product},
        )
        return dict(result.mappings().one())


async def _confirm(client: httpx.AsyncClient, password: str = SERVICE_PASSWORD) -> httpx.Response:
    return await client.post("/api/auth/confirm-password", json={"password": password})


def _patch(shop: uuid.UUID, product: int) -> str:
    return f"/api/shops/{shop}/products/{product}"


# --- Т14 ---


async def test_shop_reports_mode_and_worker_dry_run_flag(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_client: Redis, redis_prefix: str
) -> None:
    client, _, shop = await _setup(deps, raw_engine, mode="dry_run")
    key = f"{redis_prefix}:worker:dry_run"
    seen: list[Any] = []
    for stored in (b"1", b"0", None):
        if stored is None:
            await redis_client.delete(key)
        else:
            await redis_client.set(key, stored)
        body = (await client.get(f"/api/shops/{shop}")).json()
        assert body["bot_mode"] == "dry_run"
        assert "bot_enabled" not in body
        seen.append(body["server_dry_run"])
    assert seen == [True, False, None]
    listed = (await client.get("/api/shops")).json()
    assert [item["server_dry_run"] for item in listed] == [None]


# --- Т15 ---


async def test_bot_mode_change_is_validated_and_logged(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant, shop = await _setup(deps, raw_engine)
    url = f"/api/shops/{shop}/settings"
    changed = await client.patch(url, json={"bot_mode": "live"})
    again = await client.patch(url, json={"bot_mode": "live"})
    assert (changed.status_code, again.status_code) == (200, 200)
    assert changed.json()["bot_mode"] == "live"
    events = await _events(raw_engine)
    assert len(events) == 1  # повтор того же значения событие не пишет
    assert events[0] == {
        "kind": "bot_mode_changed",
        "old_value": "off",
        "new_value": "live",
        "ip": "127.0.0.1",
        "shop_id": shop,
        "sku": None,
    }

    no_city = await seed_shop(deps, raw_engine, tenant, city_id=None)
    refused = await client.patch(f"/api/shops/{no_city}/settings", json={"bot_mode": "dry_run"})
    assert refused.status_code == 422
    assert refused.json()["fields"] == ["body.city_id"]
    for body in ({"bot_enabled": True}, {"bot_mode": "on"}):
        assert (await client.patch(url, json=body)).status_code == 422
    assert len(await _events(raw_engine)) == 1
    mode = await sql_scalar(raw_engine, "SELECT bot_mode FROM shop WHERE id = :i", i=no_city)
    assert mode == "off"


# --- Т17 ---


async def test_chain_of_small_lowerings_stops_at_password(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant, shop = await _setup(deps, raw_engine)
    product = await _product(
        raw_engine, tenant, shop, low=10_000, high=50_000, anchor=10_000, current=10_000
    )
    first = await client.patch(_patch(shop, product), json={"min_price": 8100})
    assert first.status_code == 200
    assert (await _row(raw_engine, product))["min_price_anchor"] == 10_000
    # обработчик наблюдает цену, равную новому минимуму
    await sql_exec(raw_engine, "UPDATE product SET current_price = 8100 WHERE id = :i", i=product)
    second = await client.patch(_patch(shop, product), json={"min_price": 6561})
    assert second.status_code == 403
    assert second.json()["code"] == "password_required"
    assert (await _row(raw_engine, product))["min_price"] == 8100
    assert (await _confirm(client)).status_code == 204
    third = await client.patch(_patch(shop, product), json={"min_price": 6561})
    assert third.status_code == 200
    row = await _row(raw_engine, product)
    assert (row["min_price"], row["min_price_anchor"]) == (6561, 6561)


# --- Т18 ---


async def test_dangerous_edit_without_confirmation_changes_nothing(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant, shop = await _setup(deps, raw_engine)
    product = await _product(
        raw_engine,
        tenant,
        shop,
        decision_reason="undercut_first",
        decision_target_price=1100,
        decision_write="dry_run",
    )
    before = await _row(raw_engine, product)
    response = await client.patch(_patch(shop, product), json={"min_price": 500, "step": 7})
    assert response.status_code == 403
    assert response.json()["code"] == "password_required"
    assert await _row(raw_engine, product) == before
    assert await _events(raw_engine) == []


# --- Т19 ---


async def test_confirmed_password_opens_a_ten_minute_window(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_client: Redis, redis_prefix: str
) -> None:
    client, tenant, shop = await _setup(deps, raw_engine)
    product = await _product(raw_engine, tenant, shop)
    assert (await _confirm(client)).status_code == 204
    first = await client.patch(_patch(shop, product), json={"min_price": 500})
    # второй раз пароль не вводится
    second = await client.patch(_patch(shop, product), json={"min_price": 200})
    assert (first.status_code, second.status_code) == (200, 200)
    events = await _events(raw_engine)
    assert [(e["kind"], e["old_value"], e["new_value"]) for e in events] == [
        ("bounds_changed", "1000–5000", "500–5000"),
        ("bounds_changed", "500–5000", "200–5000"),
    ]
    assert events[0]["sku"] is not None and events[0]["shop_id"] == shop
    token = client.cookies.get(SESSION_COOKIE)
    assert token is not None
    key = f"{redis_prefix}:api:pw_ok:{hashlib.sha256(token.encode()).hexdigest()}"
    assert 590 < await redis_client.ttl(key) <= 600  # ровно 10 минут, не меньше


# --- Т20 ---


async def test_other_session_of_same_user_has_no_window(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant, shop = await _setup(deps, raw_engine)
    product = await _product(raw_engine, tenant, shop)
    assert (await _confirm(client)).status_code == 204
    other = make_api(deps)
    login = await other.post(
        "/api/auth/login", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )
    assert login.status_code == 200
    response = await other.patch(_patch(shop, product), json={"min_price": 500})
    assert (response.status_code, response.json()["code"]) == (403, "password_required")


# --- Т21 ---


async def test_wrong_password_is_logged_and_limited(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client = await signed_in(deps)
    wrong = await _confirm(client, "not-the-password-1")
    assert (wrong.status_code, wrong.json()["code"]) == (403, "password_invalid")
    assert (await client.get("/api/auth/me")).status_code == 200  # сессия цела
    events = await _events(raw_engine)
    assert [(e["kind"], e["ip"], e["shop_id"], e["old_value"]) for e in events] == [
        ("password_confirm_failed", "127.0.0.1", None, None)
    ]
    for _ in range(4):
        assert (await _confirm(client, "not-the-password-1")).status_code == 403
    blocked = await _confirm(client)  # шестая попытка, пароль верный
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_attempts"
    assert blocked.json()["retry_after_seconds"] > 0
    assert len(await _events(raw_engine)) == 5  # шестого события нет


async def test_four_wrong_then_right_resets_counter(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_client: Redis, redis_prefix: str
) -> None:
    client = await signed_in(deps)
    user_id = (await client.get("/api/auth/me")).json()["id"]
    for _ in range(4):
        assert (await _confirm(client, "not-the-password-1")).status_code == 403
    counter = f"{redis_prefix}:api:pw_confirm_fail:{user_id}"
    assert await redis_client.exists(counter) == 1
    assert 890 < await redis_client.ttl(counter) <= 900  # окно попыток - 15 минут
    assert (await _confirm(client)).status_code == 204
    assert await redis_client.exists(counter) == 0


# --- Т22 ---


async def test_bulk_enable_in_live_needs_password(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    client, tenant, shop = await _setup(deps, raw_engine, mode="live")
    ids = [await _product(raw_engine, tenant, shop, tracked=False) for _ in range(2)]
    url = f"/api/shops/{shop}/products"

    for changes in ({"tracked": True}, {"auto_decrease": True}):
        refused = await client.patch(url, json={"product_ids": ids, "changes": changes})
        assert refused.status_code == 403, changes
        assert refused.json()["code"] == "password_required"
    for product in ids:
        row = await _row(raw_engine, product)
        assert (row["tracked"], row["step"]) == (False, 1)
    safe: list[dict[str, Any]] = [{"tracked": False, "step": 5}, {"auto_raise": True}]
    for safe_changes in safe:
        allowed = await client.patch(url, json={"product_ids": ids, "changes": safe_changes})
        assert allowed.status_code == 200, safe_changes
    assert (await _confirm(client)).status_code == 204
    opened = await client.patch(url, json={"product_ids": ids, "changes": {"tracked": True}})
    assert opened.status_code == 200
    assert [(await _row(raw_engine, p))["tracked"] for p in ids] == [True, True]


async def test_bulk_enable_outside_live_needs_no_password(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant, _ = await _setup(deps, raw_engine)
    for mode in ("off", "dry_run"):
        shop = await seed_shop(deps, raw_engine, tenant, city_id=CITY, bot_mode=mode)
        product = await _product(raw_engine, tenant, shop, tracked=False)
        response = await client.patch(
            f"/api/shops/{shop}/products",
            json={"product_ids": [product], "changes": {"tracked": True, "auto_decrease": True}},
        )
        assert response.status_code == 200, mode


# --- Т23 ---


async def test_safe_edits_need_no_password_and_log_only_real_changes(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client, tenant, shop = await _setup(deps, raw_engine)
    product = await _product(raw_engine, tenant, shop)
    url = _patch(shop, product)

    assert (await client.patch(url, json={"min_price": 900})).status_code == 200
    assert (await _row(raw_engine, product))["min_price_anchor"] == 1000  # якорь не опускается
    assert len(await _events(raw_engine)) == 1
    assert (await client.patch(url, json={"min_price": 900, "max_price": 5000})).status_code == 200
    assert (await client.patch(url, json={"step": 3})).status_code == 200
    assert len(await _events(raw_engine)) == 1  # те же значения и только шаг: событий нет
    assert (await client.patch(url, json={"max_price": 6000})).status_code == 200
    events = await _events(raw_engine)
    assert [(e["old_value"], e["new_value"]) for e in events] == [
        ("1000–5000", "900–5000"),
        ("900–5000", "900–6000"),
    ]
    erased = await client.patch(url, json={"min_price": None})
    assert erased.status_code == 200
    assert (await _row(raw_engine, product))["min_price_anchor"] == 1000
    assert (await _events(raw_engine))[-1]["new_value"] == "–6000"


async def test_erase_then_set_lower_is_still_dangerous(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Якорь переживает стирание минимума: «стереть и задать ниже» пароль всё равно требует."""
    client, tenant, shop = await _setup(deps, raw_engine)
    product = await _product(raw_engine, tenant, shop, current=None)
    assert (await client.patch(_patch(shop, product), json={"min_price": None})).status_code == 200
    again = await client.patch(_patch(shop, product), json={"min_price": 700})
    assert (again.status_code, again.json()["code"]) == (403, "password_required")
