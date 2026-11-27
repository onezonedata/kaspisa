"""Caddyfile режима public (ADR 0023): регистрация снаружи закрыта, X-Forwarded-For перезаписан.

Настоящий Caddy и ответы по HTTPS проверяет `deploy/check/profile.sh public`. Здесь - разбор
файла: сопоставитель регистрации ловит `/register` в любом регистре, закрытие стоит раньше
проксирования, адрес клиента подменяется значением, которое видит Caddy.
"""

from __future__ import annotations

import re

from deploy_testing import REPO

CADDYFILE = (REPO / "deploy" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
CODE = "\n".join(
    line for line in CADDYFILE.splitlines() if not line.strip().startswith("#") and line.strip()
)


def test_register_matcher_is_case_insensitive_substring() -> None:
    match = re.search(r"@register\s+path_regexp\s+(\S+)", CODE)
    assert match, "нет сопоставителя @register"
    pattern = re.compile(match.group(1).replace("(?i)", "", 1), re.IGNORECASE)
    assert match.group(1).startswith("(?i)")
    for path in [
        "/register",
        "/REGISTER",
        "/api/auth/register",
        "/api/auth/Register",
        "/a/register/b",
    ]:
        assert pattern.search(path), path
    for path in ["/", "/login", "/api/auth/login", "/api/stores"]:
        assert not pattern.search(path), path


def test_register_is_closed_before_proxying() -> None:
    closed = CODE.index("respond @register 404")
    assert closed < CODE.index("reverse_proxy /api/*")
    assert closed < CODE.index("reverse_proxy web:3000")


def test_forwarded_for_is_overwritten_not_appended() -> None:
    assert "header_up X-Forwarded-For {remote_host}" in CODE
    assert not re.search(r"header_up\s+\+X-Forwarded-For", CODE)
    assert "trusted_proxies" not in CODE, "Caddy не должен доверять присланному X-Forwarded-For"


def test_api_goes_to_api_and_rest_to_web() -> None:
    assert re.search(r"reverse_proxy /api/\* api:8000", CODE)
    assert re.search(r"reverse_proxy web:3000", CODE)
