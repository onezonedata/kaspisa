"""Т1-Т5, Т7: проверка новой версии (`GET /api/version`, `ReleaseCheck`)."""

import dataclasses
import logging
from importlib.metadata import version

import httpx
import pytest
import respx
from api_testing import (
    api_cipher,
    api_database_url,
    deps,
    make_api,
    redis_client,
    redis_prefix,
    signed_in,
)
from pydantic import ValidationError

from kaspisa.api import ApiDeps
from kaspisa.api.deps import ApiSettings
from kaspisa.api.version import ReleaseCheck

__all__ = ["api_cipher", "api_database_url", "deps", "redis_client", "redis_prefix"]

RELEASE_URL = "https://api.github.com/repos/owner/kaspisa/releases/latest"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_empty_repo_makes_no_request(deps: ApiDeps) -> None:
    """Т1: пустое имя репозитория - проверка выключена, сети нет."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith="https://api.github.com")
        client = await signed_in(deps)
        response = await client.get("/api/version")
        assert route.call_count == 0
    assert response.status_code == 200
    assert response.json() == {
        "current": version("kaspisa"),
        "latest": None,
        "update_available": False,
        "release_url": None,
    }


async def test_version_requires_session(deps: ApiDeps) -> None:
    response = await make_api(deps).get("/api/version")
    assert response.status_code == 401


async def test_endpoint_reports_newer_release(deps: ApiDeps) -> None:
    custom = dataclasses.replace(deps, release_check=ReleaseCheck("owner/kaspisa", "0.1.0"))
    with respx.mock() as mock:
        mock.get(RELEASE_URL).respond(json={"tag_name": "v0.2.0"})
        client = await signed_in(custom)
        response = await client.get("/api/version")
    assert response.json() == {
        "current": "0.1.0",
        "latest": "0.2.0",
        "update_available": True,
        "release_url": "https://github.com/owner/kaspisa/releases/tag/v0.2.0",
    }


async def test_newer_release_is_reported() -> None:
    """Т2: новая версия, адрес запроса и User-Agent."""
    with respx.mock() as mock:
        route = mock.get(RELEASE_URL).respond(json={"tag_name": "v0.2.0"})
        result = await ReleaseCheck("owner/kaspisa", "0.1.0").get()
    assert result.latest == "0.2.0"
    assert result.update_available is True
    assert result.release_url == "https://github.com/owner/kaspisa/releases/tag/v0.2.0"
    assert route.call_count == 1
    assert route.calls.last.request.headers["user-agent"]


async def test_result_is_cached_for_24_hours() -> None:
    """Т3: два вызова в пределах суток - один запрос, на границе суток - второй."""
    clock = Clock()
    check = ReleaseCheck("owner/kaspisa", "0.1.0", clock=clock)
    with respx.mock() as mock:
        route = mock.get(RELEASE_URL).respond(json={"tag_name": "v0.2.0"})
        await check.get()
        clock.now = 86399
        await check.get()
        assert route.call_count == 1
        clock.now = 86400
        await check.get()
        assert route.call_count == 2


@pytest.mark.parametrize(
    ("tag", "current", "latest", "available"),
    [
        ("v0.1.0", "0.1.0", "0.1.0", False),
        ("v0.0.9", "0.1.0", "0.0.9", False),
        ("v0.10.0", "0.9.0", "0.10.0", True),
    ],
)
async def test_versions_compare_as_numbers(
    tag: str, current: str, latest: str, available: bool
) -> None:
    """Т4: сравнение чисел, а не строк."""
    with respx.mock() as mock:
        mock.get(RELEASE_URL).respond(json={"tag_name": tag})
        result = await ReleaseCheck("owner/kaspisa", current).get()
    assert result.latest == latest
    assert result.update_available is available


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(500, text="boom-SECRET-BODY"),
        httpx.ConnectTimeout("timeout"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"tag_name": "nightly"}),
        httpx.Response(200, json={"tag_name": "v1.2"}),
        httpx.Response(200, json={"name": "no tag"}),
        httpx.Response(200, json=["list"]),
    ],
)
async def test_failures_give_no_update_and_are_cached(
    outcome: httpx.Response | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """Т5: сбой - пустой ответ, WARNING без тела, повтор в течение суток без запроса."""
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    check = ReleaseCheck("owner/kaspisa", "0.1.0", clock=clock)
    with respx.mock() as mock:
        route = mock.get(RELEASE_URL)
        if isinstance(outcome, Exception):
            route.side_effect = outcome
        else:
            route.mock(return_value=outcome)
        first = await check.get()
        clock.now = 100
        second = await check.get()
        assert route.call_count == 1
    for result in (first, second):
        assert result.latest is None
        assert result.update_available is False
        assert result.release_url is None
        assert result.current == "0.1.0"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert all(r.name == "kaspisa.api" for r in warnings)
    assert "SECRET-BODY" not in caplog.text


async def test_redirect_is_not_followed() -> None:
    """Перенаправление на чужой адрес не выполняется: сбой, один запрос."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RELEASE_URL).respond(302, headers={"Location": "https://evil.example/x"})
        target = mock.get("https://evil.example/x").respond(json={"tag_name": "v9.9.9"})
        result = await ReleaseCheck("owner/kaspisa", "0.1.0").get()
    assert result.latest is None
    assert result.update_available is False
    assert route.call_count == 1
    assert target.call_count == 0


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_error_status_with_valid_tag_is_ignored(status: int) -> None:
    """Тег из тела ответа с ошибкой не принимается."""
    with respx.mock() as mock:
        mock.get(RELEASE_URL).respond(status, json={"tag_name": "v0.2.0"})
        result = await ReleaseCheck("owner/kaspisa", "0.1.0").get()
    assert result.latest is None
    assert result.update_available is False


@pytest.mark.parametrize("repo", ["https://evil.example/x", "a/b/c", "a/../b", "a b/c"])
def test_update_repo_rejects_bad_values(repo: str) -> None:
    """Т7: имя репозитория проверяется шаблоном."""
    with pytest.raises(ValidationError):
        ApiSettings(update_repo=repo)


@pytest.mark.parametrize("repo", ["", "owner/kaspisa"])
def test_update_repo_accepts_good_values(repo: str) -> None:
    assert ApiSettings(update_repo=repo).update_repo == repo
