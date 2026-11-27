"""Т10, Т13-Т17, Т71: CSRF, ошибки без эха, Redis, OpenAPI, запуск, cookie."""

import json
import logging
import os
import subprocess
import sys
from typing import Any

import httpx
import pytest
from api_testing import (
    KASPI_PASSWORD,
    ROUTES,
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    deps,
    make_api,
    redis_client,
    redis_prefix,
    signed_in,
)
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from kaspisa.api import ApiDeps, create_app
from kaspisa.api.deps import ApiSettings
from kaspisa.api.openapi import main as openapi_main

__all__ = ["api_cipher", "api_database_url", "deps", "redis_client", "redis_prefix"]

SAFE = {"GET", "HEAD", "OPTIONS"}


def _api_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Изменяющие маршруты /api из схемы (обход `app.routes` скрыт за вложенными роутерами)."""
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api")
        for method in operations
        if method.upper() not in SAFE
    }


async def test_csrf_header_required_on_every_mutating_route(deps: ApiDeps) -> None:
    app = create_app(deps)
    mutating = {(r.method, r.path) for r in ROUTES if r.method not in SAFE}
    assert mutating == _api_routes(app)
    anonymous = make_api(deps, app=app, csrf=False)
    session = await signed_in(deps)
    session.headers.pop("X-Requested-With")
    for spec in (r for r in ROUTES if r.method not in SAFE):
        for client in (anonymous, session):
            response = await client.request(spec.method, spec.path, json=spec.body)
            assert response.status_code == 403, spec
            assert response.json()["code"] == "csrf_required"
    me = await anonymous.get("/api/auth/me")
    assert me.status_code == 401  # GET без заголовка доходит до аутентификации


async def test_validation_errors_do_not_echo_input(deps: ApiDeps) -> None:
    app = create_app(deps)
    app.add_api_route("/api/probe", _probe, methods=["POST"])
    probe = make_api(deps, app=app)

    too_long = await probe.post(
        "/api/probe", json={"login": "seller-7f3a@shop.kz", "password": KASPI_PASSWORD + "a" * 300}
    )
    broken = await probe.post(
        "/api/probe",
        content='{"password":"' + KASPI_PASSWORD + '"',
        headers={"Content-Type": "application/json"},
    )

    for response in (too_long, broken):
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
        assert KASPI_PASSWORD not in response.text
        assert "seller-7f3a" not in response.text


class _Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str
    password: str = Field(max_length=256)


async def _probe(data: _Probe) -> dict[str, str]:
    return {"login": data.login}


async def test_unhandled_error_is_500_without_text(
    deps: ApiDeps, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(deps)

    async def boom() -> None:
        raise RuntimeError("SECRET-500")

    app.add_api_route("/api/boom", boom, methods=["GET"])
    caplog.set_level(logging.DEBUG)

    response = await make_api(deps, app=app).get("/api/boom")

    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "retry_after_seconds": None,
        "fields": None,
    }
    assert "SECRET-500" not in response.text
    for record in caplog.records:
        assert "SECRET-500" not in record.getMessage()
        assert record.exc_info is None
        assert "SECRET-500" not in (record.exc_text or "")
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


async def test_redis_down_is_503(deps: ApiDeps) -> None:
    dead = Redis(port=1, socket_connect_timeout=1, socket_timeout=1)
    from dataclasses import replace

    from kaspisa.api.redis_state import RedisState

    broken = replace(deps, redis=dead, state=RedisState(dead, prefix="x:api"))
    client = make_api(broken)
    client.cookies.set("kaspisa_session", "any")

    me = await client.get("/api/auth/me")
    login = await client.post(
        "/api/auth/login", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )
    await dead.aclose()

    assert me.status_code == 503
    assert me.json()["code"] == "service_unavailable"
    assert login.status_code == 503


async def test_openapi_basics(deps: ApiDeps, capsys: pytest.CaptureFixture[str]) -> None:
    app = create_app(deps)
    response = await make_api(deps, app=app).get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()

    ids = [op["operationId"] for path in schema["paths"].values() for op in path.values()]
    assert len(ids) == len(set(ids))
    assert {"register", "login", "logout", "logout_all", "me", "password_reset"} <= set(ids)
    openapi_main()
    assert json.loads(capsys.readouterr().out) == schema
    forbidden = {"password", "password_hash", "login", "session", "token"}
    for path in schema["paths"].values():
        for op in path.values():
            for reply in op["responses"].values():
                for content in reply.get("content", {}).values():
                    assert not (_properties(schema, content["schema"]) & forbidden)


def _properties(schema: dict[str, Any], node: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _properties(schema, schema["components"]["schemas"][name])
    found |= set(node.get("properties", {}))
    children = [*node.get("properties", {}).values()]
    children += [*node.get("anyOf", []), *node.get("allOf", [])]
    if "items" in node:
        children.append(node["items"])
    for child in children:
        found |= _properties(schema, child)
    return found


async def test_health_from_create_app() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_import_does_not_connect() -> None:
    env = {**os.environ, "KASPISA_DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/x"}
    env["KASPISA_REDIS_URL"] = "redis://127.0.0.1:1/0"
    code = (
        "import kaspisa.api.app as m;"
        "paths=set(m.app.openapi()['paths']);"
        "assert '/api/auth/login' in paths and '/health' in paths"
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr


async def test_cookie_without_secure_for_development(deps: ApiDeps) -> None:
    from dataclasses import replace

    client = make_api(replace(deps, cookie_secure=False))

    response = await client.post(
        "/api/auth/register", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )

    cookie = response.headers["set-cookie"]
    assert "Secure" not in cookie
    for attribute in ("HttpOnly", "Max-Age=2592000", "Path=/"):
        assert attribute in cookie
    assert "samesite=lax" in cookie.lower()
    assert ApiSettings().cookie_secure is True
