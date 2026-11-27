"""Т65, Т66: `make dev-backend` не доверяет присланному `X-Forwarded-For` (ADR 0014)."""

import shlex
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from uvicorn.main import main as uvicorn_cli

from kaspisa.api.auth import client_ip

_MAKEFILE = Path(__file__).resolve().parents[3] / "Makefile"


def _probe() -> FastAPI:
    app = FastAPI()

    @app.get("/ip")
    async def ip(request: Request) -> str:
        return client_ip(request)

    return app


def _dev_backend_params() -> dict[str, Any]:
    """Параметры CLI uvicorn из рецепта цели `dev-backend`."""
    lines = _MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = lines.index("dev-backend:")
    recipe = next(line for line in lines[start + 1 :] if line.startswith("\t"))
    tokens = shlex.split(recipe)
    args = tokens[tokens.index("uvicorn") + 1 :]
    # Приложение (первый позиционный аргумент) в Config не передаётся.
    return dict(uvicorn_cli.make_context("uvicorn", args).params)


async def _forwarded_ip(proxy_headers: bool, forwarded_allow_ips: str | None) -> str:
    config = uvicorn.Config(
        app=_probe(),
        proxy_headers=proxy_headers,
        forwarded_allow_ips=forwarded_allow_ips,
        log_config=None,
    )
    config.load()
    transport = httpx.ASGITransport(app=config.loaded_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ip", headers={"X-Forwarded-For": "10.0.0.5"})
    result: str = response.json()
    return result


async def test_dev_backend_ignores_forwarded_for() -> None:
    params = _dev_backend_params()
    assert params["proxy_headers"] is False
    got = await _forwarded_ip(params["proxy_headers"], params["forwarded_allow_ips"])
    assert got == "127.0.0.1"


async def test_proxy_headers_enabled_trusts_forwarded_for() -> None:
    """Контроль: при включённых proxy_headers адрес подменяется, значит Т65 может упасть."""
    assert await _forwarded_ip(True, "127.0.0.1") == "10.0.0.5"
