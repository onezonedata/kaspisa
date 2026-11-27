"""Т66-Т68: секреты ни в одном ответе и журнале, границы пакета, полный набор OpenAPI."""

import ast
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from api_testing import (
    EMAIL_CODE,
    KASPI_COOKIE,
    KASPI_LOGIN,
    KASPI_PASSWORD,
    ROUTES,
    SERVICE_PASSWORD,
    FakeClock,
    api_cipher,
    api_database_url,
    code_required,
    deps,
    fake_client,
    kaspi_session,
    listed,
    logged_in,
    make_api,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_change,
    seed_product,
    sql_exec,
    tenant_of,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps, create_app
from kaspisa.api.reset_token import issue_reset_token

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

API_DIR = Path(__file__).resolve().parents[2] / "src" / "kaspisa" / "api"
NEW_PASSWORD = "service-pass-new-7f3a"  # noqa: S105

EXPECTED_PATHS: dict[str, set[str]] = {
    "/health": {"GET"},
    "/api/auth/register": {"POST"},
    "/api/auth/login": {"POST"},
    "/api/auth/logout": {"POST"},
    "/api/auth/logout-all": {"POST"},
    "/api/auth/me": {"GET"},
    "/api/auth/password-reset": {"POST"},
    "/api/auth/confirm-password": {"POST"},
    "/api/events": {"GET"},
    "/api/shops": {"GET", "POST"},
    "/api/shops/{shop_id}": {"GET", "DELETE"},
    "/api/shops/{shop_id}/credentials": {"PUT"},
    "/api/shops/{shop_id}/settings": {"PATCH"},
    "/api/shops/{shop_id}/halt/clear": {"POST"},
    "/api/shops/{shop_id}/login": {"POST"},
    "/api/shops/{shop_id}/login/restart": {"POST"},
    "/api/shops/{shop_id}/code": {"POST"},
    "/api/shops/{shop_id}/code/resend": {"POST"},
    "/api/shops/{shop_id}/products/sync": {"POST"},
    "/api/shops/{shop_id}/products": {"GET", "PATCH"},
    "/api/shops/{shop_id}/products/{product_id}": {"GET", "PATCH"},
    "/api/shops/{shop_id}/products/{product_id}/price-read": {"POST"},
    "/api/shops/{shop_id}/price-changes": {"GET"},
    "/api/shops/{shop_id}/catalog-summary": {"GET"},
    "/api/version": {"GET"},
    "/api/notifications/telegram": {"GET", "PUT", "DELETE"},
    "/api/notifications/telegram/test": {"POST"},
}

TELEGRAM_TOKEN = "123456789:" + "A" * 35


async def test_secrets_are_in_no_response_or_log(
    deps: ApiDeps, raw_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Т66: сценарий по всем маршрутам `/api`; в ответах и журнале нет секретов."""
    caplog.set_level(logging.DEBUG)
    clock = deps.clock
    assert isinstance(clock, FakeClock)
    fake = fake_client(deps)
    called: set[tuple[str, str]] = set()
    transcript: list[str] = []
    reset_token_holder: list[str] = []

    async def call(
        client: httpx.AsyncClient, method: str, template: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        response = await client.request(method, url, **kwargs)
        called.add((method, template))
        headers = {k: v for k, v in response.headers.items() if k.lower() != "set-cookie"}
        transcript.extend([response.text, str(headers)])
        return response

    anonymous = make_api(deps)
    registered = await call(
        anonymous,
        "POST",
        "/api/auth/register",
        "/api/auth/register",
        json={"email": "a@example.com", "password": SERVICE_PASSWORD},
    )
    assert registered.status_code == 201
    client = make_api(deps)
    client.cookies.update(anonymous.cookies)
    await call(client, "GET", "/api/auth/me", "/api/auth/me")
    await call(client, "GET", "/api/events", "/api/events")
    await call(
        client,
        "POST",
        "/api/auth/confirm-password",
        "/api/auth/confirm-password",
        json={"password": SERVICE_PASSWORD},
    )
    shops = "/api/shops"
    added = await call(
        client, "POST", shops, shops, json={"login": KASPI_LOGIN, "password": KASPI_PASSWORD}
    )
    shop_id = added.json()["id"]
    base = f"{shops}/{shop_id}"
    sid = shops + "/{shop_id}"
    await call(client, "GET", shops, shops)
    await call(client, "GET", sid, base)

    fake.queue("login", code_required(kaspi_session("awaiting_code", cookie=KASPI_COOKIE)))
    await call(client, "POST", sid + "/login", base + "/login")
    clock.advance(200)
    await deps.state.clear_resend_timer(uuid.UUID(shop_id))
    fake.queue("resend_code", None)
    await call(client, "POST", sid + "/code/resend", base + "/code/resend")
    clock.advance(200)
    await deps.state.clear_resend_timer(uuid.UUID(shop_id))
    fake.queue("login", code_required(kaspi_session("awaiting_code", cookie=KASPI_COOKIE)))
    await call(client, "POST", sid + "/login/restart", base + "/login/restart")
    fake.queue("submit_code", logged_in("M1", session=kaspi_session(cookie=KASPI_COOKIE)))
    code = await call(client, "POST", sid + "/code", base + "/code", json={"code": EMAIL_CODE})
    assert code.json()["outcome"] == "connected"

    fake.queue("list_products", [listed("A", "Телефон"), listed("B")])
    await call(client, "POST", sid + "/products/sync", base + "/products/sync")
    products = sid + "/products"
    listing = await call(client, "GET", products, base + "/products")
    first = listing.json()["items"][0]["id"]
    tenant = await tenant_of(raw_engine, "a@example.com")
    shop_uuid = uuid.UUID(shop_id)
    await seed_product(raw_engine, tenant, shop_uuid)
    await seed_change(raw_engine, tenant, shop_uuid, first)
    pid = products + "/{product_id}"
    await call(client, "GET", pid, f"{base}/products/{first}")
    await call(client, "POST", pid + "/price-read", f"{base}/products/{first}/price-read")
    await call(client, "PATCH", pid, f"{base}/products/{first}", json={"min_price": 5})
    await call(
        client,
        "PATCH",
        products,
        base + "/products",
        json={"product_ids": [first], "changes": {"tracked": True}},
    )
    await call(client, "GET", sid + "/price-changes", base + "/price-changes")
    await call(client, "GET", sid + "/catalog-summary", base + "/catalog-summary")
    await call(client, "GET", "/api/version", "/api/version")
    telegram = "/api/notifications/telegram"
    with respx.mock(assert_all_called=False) as telegram_mock:
        telegram_mock.post(url__startswith="https://api.telegram.org/bot").respond(200)
        await call(client, "GET", telegram, telegram)
        await call(
            client, "PUT", telegram, telegram, json={"token": TELEGRAM_TOKEN, "chat_id": "42"}
        )
        await call(client, "POST", telegram + "/test", telegram + "/test")
        await call(
            client, "PUT", telegram, telegram, json={"token": TELEGRAM_TOKEN, "chat_id": "43"}
        )
        await call(client, "DELETE", telegram, telegram)
        assert deps.notifier is not None
        await deps.notifier.drain()
    await call(client, "PATCH", sid + "/settings", base + "/settings", json={"default_step": 2})
    await sql_exec(
        raw_engine,
        "UPDATE shop SET halt_reason = 'write_errors', halted_at = now() WHERE id = :id",
        id=shop_uuid,
    )
    await call(client, "POST", sid + "/halt/clear", base + "/halt/clear")
    await call(
        client,
        "PUT",
        sid + "/credentials",
        base + "/credentials",
        json={"login": KASPI_LOGIN, "password": KASPI_PASSWORD},
    )
    await call(client, "DELETE", sid, base)

    token = await issue_reset_token(deps, "a@example.com")
    assert token is not None
    reset_token_holder.append(token)
    await call(
        anonymous,
        "POST",
        "/api/auth/password-reset",
        "/api/auth/password-reset",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    again = make_api(deps)
    await call(
        again,
        "POST",
        "/api/auth/login",
        "/api/auth/login",
        json={"email": "a@example.com", "password": NEW_PASSWORD},
    )
    await call(again, "POST", "/api/auth/logout", "/api/auth/logout")
    await call(again, "POST", "/api/auth/logout-all", "/api/auth/logout-all")

    schema = create_app(deps).openapi()["paths"]
    expected = {
        (method.upper(), path)
        for path, operations in schema.items()
        if path.startswith("/api")
        for method in operations
    }
    assert called == expected
    assert expected == {(r.method, r.path) for r in ROUTES}
    secrets = [
        KASPI_LOGIN,
        KASPI_PASSWORD,
        KASPI_COOKIE,
        EMAIL_CODE,
        SERVICE_PASSWORD,
        NEW_PASSWORD,
        TELEGRAM_TOKEN,
        "scrypt$",
        *reset_token_holder,
    ]
    for secret in secrets:
        assert all(secret not in text for text in transcript), secret
        assert secret not in caplog.text, secret


def test_package_boundaries() -> None:
    """Т67: `.system()` только в `auth.py`, `reset_token.py`; нет sqlalchemy, set_price, dry_run."""
    system_calls: dict[str, int] = {}
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "system"
            ):
                system_calls[path.name] = system_calls.get(path.name, 0) + 1
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == "sqlalchemy" for a in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "sqlalchemy", path
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.alias):
                names.append(node.name)
            elif isinstance(node, ast.keyword) and node.arg:
                names.append(node.arg)
            for name in names:
                assert name != "set_price", path
                # API только читает флаг, который опубликовал обработчик (ADR 0024)
                if name not in {"server_dry_run", "read_dry_run"}:
                    assert "dry_run" not in name.lower(), path
    assert set(system_calls) <= {"auth.py", "reset_token.py", "add_user.py", "notifications.py"}


async def test_openapi_full_set(deps: ApiDeps) -> None:
    """Т68: 18 путей `/api` и `/health`, методы по разделу 5, схема ответа успеха."""
    schema = create_app(deps).openapi()["paths"]

    assert {path: {m.upper() for m in ops} for path, ops in schema.items()} == EXPECTED_PATHS
    for path, operations in schema.items():
        for method, operation in operations.items():
            success = [code for code in operation["responses"] if code.startswith("2")]
            assert success, (method, path)
            for code in success:
                if code != "204":
                    content = operation["responses"][code].get("content", {})
                    assert content["application/json"]["schema"], (method, path)

    # Д1: ошибки описаны `ErrorOut`, стандартный 422 FastAPI из схемы убран.
    document = create_app(deps).openapi()
    for path, operations in document["paths"].items():
        if not path.startswith("/api"):
            continue
        for method, operation in operations.items():
            responses = operation["responses"]
            default = responses["default"]["content"]["application/json"]["schema"]
            assert default == {"$ref": "#/components/schemas/ErrorOut"}, (method, path)
            assert "422" not in responses, (method, path)
    components = document["components"]["schemas"]
    assert {"code", "retry_after_seconds", "fields"} <= set(components["ErrorOut"]["properties"])
    assert "HTTPValidationError" not in components
