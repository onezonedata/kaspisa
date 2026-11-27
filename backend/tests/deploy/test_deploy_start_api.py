"""Т16–Т18: `backend/docker/start-api.sh` выбирает флаги uvicorn по режиму установки."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from deploy_testing import START_API, write_stub
from fastapi import FastAPI, Request
from uvicorn.main import main as uvicorn_cli

from kaspisa.api.auth import client_ip

SUBNET = "172.29.247.0/29"


def _probe() -> FastAPI:
    app = FastAPI()

    @app.get("/ip")
    async def ip(request: Request) -> str:
        return client_ip(request)

    return app


def _run(tmp_path: Path, env: dict[str, str]) -> tuple[int, str, list[str] | None]:
    """Скрипт с заглушкой uvicorn: код, stderr и аргументы заглушки (None — не вызвана)."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    args_file = tmp_path / "args.txt"
    write_stub(
        stubs,
        "uvicorn",
        f'for a in "$@"; do printf \'%s\\n\' "$a" >> "{args_file}"; done\n',
    )
    done = subprocess.run(  # noqa: S603
        ["/bin/sh", str(START_API)],
        env={"PATH": f"{stubs}:/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    args = args_file.read_text(encoding="utf-8").splitlines() if args_file.exists() else None
    return done.returncode, done.stderr, args


def _params(args: list[str]) -> dict[str, Any]:
    # Приложение (позиционный аргумент) в Config не передаётся: оно в params["app"].
    return dict(uvicorn_cli.make_context("uvicorn", args).params)


async def _forwarded_ip(params: dict[str, Any], peer: str, xff: str) -> str:
    config = uvicorn.Config(
        app=_probe(),
        proxy_headers=params["proxy_headers"],
        forwarded_allow_ips=params["forwarded_allow_ips"],
        log_config=None,
    )
    config.load()
    transport = httpx.ASGITransport(app=config.loaded_app, client=(peer, 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ip", headers={"X-Forwarded-For": xff})
    result: str = response.json()
    return result


@pytest.mark.parametrize("mode", ["local", "tunnel"])
async def test_local_and_tunnel_ignore_forwarded_for(tmp_path: Path, mode: str) -> None:
    code, _, args = _run(tmp_path, {"KASPISA_MODE": mode, "KASPISA_EDGE_SUBNET": SUBNET})
    assert code == 0
    assert args is not None
    assert args[0] == "kaspisa.main:app"
    params = _params(args)
    assert params["host"] == "0.0.0.0"  # noqa: S104
    assert params["port"] == 8000
    assert params["proxy_headers"] is False
    assert await _forwarded_ip(params, "127.0.0.1", "10.0.0.5") == "127.0.0.1"


async def test_public_trusts_only_edge_subnet(tmp_path: Path) -> None:
    code, _, args = _run(tmp_path, {"KASPISA_MODE": "public", "KASPISA_EDGE_SUBNET": SUBNET})
    assert code == 0
    assert args is not None
    params = _params(args)
    assert params["proxy_headers"] is True
    assert params["forwarded_allow_ips"] == SUBNET
    assert await _forwarded_ip(params, "172.29.247.2", "1.2.3.4") == "1.2.3.4"
    assert await _forwarded_ip(params, "172.18.0.5", "1.2.3.4") == "172.18.0.5"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"KASPISA_MODE": ""},
        {"KASPISA_MODE": "prod", "KASPISA_EDGE_SUBNET": SUBNET},
        {"KASPISA_MODE": "public"},
        {"KASPISA_MODE": "public", "KASPISA_EDGE_SUBNET": ""},
    ],
)
def test_bad_mode_does_not_start_uvicorn(tmp_path: Path, env: dict[str, str]) -> None:
    code, stderr, args = _run(tmp_path, env)
    assert code == 2
    assert args is None
    assert stderr.strip()
