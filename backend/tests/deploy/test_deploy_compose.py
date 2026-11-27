"""Т20, Т21: состав `docker-compose.prod.yml` — порты, секреты, сети, миграции (по `config`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from deploy_testing import (
    ENC_KEY,
    PG_APP,
    PG_OWNER,
    PG_SUPER,
    STUB_VERSION,
    compose_config,
    docker_compose_available,
    duration_seconds,
)

pytestmark = pytest.mark.skipif(
    not docker_compose_available(), reason="нет docker compose: проверка состава пропущена"
)

PROFILES = ["local", "tunnel", "public"]
NO_PORTS = ["db", "redis", "api", "worker", "migrate", "backup"]


def _services_with(config: dict[str, Any], secret: str) -> set[str]:
    """Сервисы, в окружении которых значение встречается (в том числе внутри URL)."""
    found: set[str] = set()
    for name, service in config["services"].items():
        env = service.get("environment") or {}
        if any(secret in str(value) for value in env.values()):
            found.add(name)
    return found


@pytest.mark.parametrize("profile", PROFILES)
def test_published_ports(tmp_path: Path, profile: str) -> None:
    services = compose_config(tmp_path, profile)["services"]
    for name in NO_PORTS:
        assert not services[name].get("ports"), name
    web_ports = services["web"]["ports"]
    assert len(web_ports) == 1
    assert web_ports[0]["host_ip"] == "127.0.0.1"
    assert web_ports[0]["target"] == 3000
    if profile == "public":
        caddy_ports = {p["target"] for p in services["caddy"]["ports"]}
        assert caddy_ports == {80, 443}
    else:
        assert "caddy" not in services
    published = {
        (name, p["target"])
        for name, service in services.items()
        for p in service.get("ports") or []
    }
    allowed = {("web", 3000)} | ({("caddy", 80), ("caddy", 443)} if profile == "public" else set())
    assert published == allowed


@pytest.mark.parametrize("profile", PROFILES)
def test_migrations_images_secrets_networks(tmp_path: Path, profile: str) -> None:
    config = compose_config(tmp_path, profile)
    services = config["services"]

    for name in ("api", "worker"):
        migrate_dep = services[name]["depends_on"]["migrate"]
        assert migrate_dep["condition"] == "service_completed_successfully"
        assert duration_seconds(str(services[name]["stop_grace_period"])) >= 300
    assert services["migrate"]["restart"] == "no"

    for name in ("api", "worker", "migrate"):
        assert services[name]["image"] == f"local/kaspisa-backend:{STUB_VERSION}"
    assert services["web"]["image"] == f"local/kaspisa-web:{STUB_VERSION}"
    assert all("latest" not in service["image"] for service in services.values())

    assert "log_error_verbosity=terse" in " ".join(services["db"]["command"])

    assert _services_with(config, PG_SUPER) == {"db"}
    assert _services_with(config, PG_OWNER) == {"db", "migrate", "backup"}
    assert _services_with(config, PG_APP) == {"db", "api", "worker"}
    assert _services_with(config, ENC_KEY) == {"api", "worker"}

    assert services["api"]["environment"]["KASPISA_MODE"] == profile
    assert "KASPISA_DRY_RUN" not in services["api"]["environment"]
    assert services["worker"]["environment"]["KASPISA_DRY_RUN"] == "true"

    edge = config["networks"]["edge"]
    assert edge["internal"] is True
    assert edge["ipam"]["config"][0]["subnet"] == "172.29.247.0/29"
    members = {name for name, s in services.items() if "edge" in s.get("networks", {})}
    assert members == ({"api", "caddy"} if profile == "public" else {"api"})
    assert "edge" not in services["web"]["networks"]


@pytest.mark.parametrize("profile", PROFILES)
def test_only_directories_are_bind_mounted_from_deploy(tmp_path: Path, profile: str) -> None:
    services = compose_config(tmp_path, profile)["services"]
    for name, service in services.items():
        for volume in service.get("volumes") or []:
            if volume["type"] != "bind":
                continue
            source = volume["source"]
            assert not source.endswith((".sh", ".sql", "Caddyfile")), (name, source)
    backup_sources = {v["source"] for v in services["backup"]["volumes"] if v["type"] == "bind"}
    assert any(s.endswith("/deploy") for s in backup_sources)
    if profile == "public":
        caddy_sources = {v["source"] for v in services["caddy"]["volumes"] if v["type"] == "bind"}
        assert any(s.endswith("/deploy/caddy") for s in caddy_sources)
