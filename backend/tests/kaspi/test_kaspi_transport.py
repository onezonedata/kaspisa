"""Общий путь запроса: повторы, ошибки, проверка адреса (Т12-Т16, Т18)."""

# ruff: noqa: F401, F811  (фикстуры импортируются из kaspi_fixtures; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from kaspi_fixtures import (
    client,
    fake_time,
    limiter,
    mock_kaspi,
)
from kaspi_testing import (
    EDIT_PRICE_URL,
    MERCHANT_UID,
    SKU,
    FakeTime,
    RecordingLimiter,
    authenticated_session,
    load_fixture,
)

from kaspisa.kaspi import (
    KaspiClient,
    KaspiTransportError,
    RateLimitedError,
    ResponseFormatError,
    UnexpectedStatusError,
)
from kaspisa.kaspi.transport import Transport

OFFERS_URL = "https://kaspi.kz/yml/offer-view/offers/900000005"


async def test_network_failure_retried_with_pauses(
    client: KaspiClient,
    mock_kaspi: respx.MockRouter,
    limiter: RecordingLimiter,
    fake_time: FakeTime,
) -> None:
    """Т12: два сбоя, затем успех; паузы 1 и 3 с; каждая попытка через ограничитель."""
    route = mock_kaspi.post(OFFERS_URL).mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
            httpx.Response(200, json=load_fixture("offers.json")),
        ]
    )
    offers = (await client.get_offers("900000005", "710000000", "90100003")).offers
    assert len(offers) == 7
    assert route.call_count == 3
    assert fake_time.sleeps == [1.0, 3.0]
    assert limiter.keys == ["kaspi.kz"] * 3


async def test_three_failures_raise_transport_error_without_cause(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т13: три сбоя - `KaspiTransportError` без `__cause__` и `__context__`."""
    route = mock_kaspi.post(OFFERS_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(KaspiTransportError) as info:
        await client.get_offers("900000005", "710000000", "90100003")
    assert route.call_count == 3
    assert info.value.kind == "ConnectError"
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


async def test_503_retried_then_success(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т14: 503, затем 200 - успех."""
    mock_kaspi.post(OFFERS_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=load_fixture("offers.json")),
        ]
    )
    assert len((await client.get_offers("900000005", "710000000", "90100003")).offers) == 7


async def test_three_503_give_unexpected_status(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т14: три раза 503 - `UnexpectedStatusError(503)`."""
    route = mock_kaspi.post(OFFERS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(UnexpectedStatusError) as info:
        await client.get_offers("900000005", "710000000", "90100003")
    assert info.value.status_code == 503
    assert route.call_count == 3


async def test_429_not_retried(client: KaspiClient, mock_kaspi: respx.MockRouter) -> None:
    """Т15: 429 с `Retry-After` - без повторов; без заголовка `retry_after is None`."""
    route = mock_kaspi.post(OFFERS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "30"}),
            httpx.Response(429),
        ]
    )
    with pytest.raises(RateLimitedError) as info:
        await client.get_offers("900000005", "710000000", "90100003")
    assert info.value.retry_after == 30
    assert route.call_count == 1
    with pytest.raises(RateLimitedError) as info2:
        await client.get_offers("900000005", "710000000", "90100003")
    assert info2.value.retry_after is None


async def test_non_json_body_is_format_error(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т16: тело не JSON при 200."""
    mock_kaspi.post(OFFERS_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(ResponseFormatError) as info:
        await client.get_offers("900000005", "710000000", "90100003")
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/yml",
        "http://kaspi.kz/yml",
        "https://kaspi.kz.evil.example/yml",
        "https://other.kaspi.kz/yml",
    ],
)
async def test_foreign_host_or_http_rejected_before_send(
    mock_kaspi: respx.MockRouter, limiter: RecordingLimiter, fake_time: FakeTime, url: str
) -> None:
    """Т18: чужой хост и `http` отклоняются до отправки (маршрутов нет - запрос упал бы иначе)."""
    transport = Transport(limiter, fake_time.sleep)
    try:
        with pytest.raises(ValueError, match="адрес"):
            await transport.send("POST", url, operation="x", retry=False)
    finally:
        await transport.aclose()
    assert limiter.keys == []


async def test_cookie_isolation_between_sessions(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т17: `Set-Cookie` магазина А не уходит ни магазину Б, ни в `get_offers`."""
    first_reply = httpx.Response(
        200,
        json=load_fixture("edit-price.json"),
        headers={"Set-Cookie": "shop-a-cookie=marker-shop-a; Path=/; Secure"},
    )
    plain_reply = httpx.Response(200, json=load_fixture("edit-price.json"))
    cabinet = mock_kaspi.get(url__startswith=EDIT_PRICE_URL).mock(
        side_effect=[first_reply, plain_reply, plain_reply]
    )
    offers = mock_kaspi.post(OFFERS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("offers.json"))
    )
    session_a = authenticated_session()
    session_b = authenticated_session()
    await client.get_product(session_a, MERCHANT_UID, SKU)
    assert session_a.changed is True
    assert session_b.changed is False
    await client.get_product(session_b, MERCHANT_UID, SKU)
    await client.get_offers("900000005", "710000000", "90100003")
    assert "marker-shop-a" not in cabinet.calls.last.request.headers.get("cookie", "")
    assert "cookie" not in offers.calls.last.request.headers
    # Сама сессия А своё cookie получила и отправит.
    await client.get_product(session_a, MERCHANT_UID, SKU)
    assert "shop-a-cookie=marker-shop-a" in cabinet.calls.last.request.headers["cookie"]


def _bad_gzip() -> httpx.Response:
    return httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not-gzip")
    )


def _assert_clean(exc: KaspiTransportError) -> None:
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert not hasattr(exc, "request")


async def test_decoding_error_retried_then_raised(
    client: KaspiClient, mock_kaspi: respx.MockRouter, fake_time: FakeTime
) -> None:
    """Т69 (а): тело не декодируется на всех попытках - `KaspiTransportError`, три вызова."""
    route = mock_kaspi.post(OFFERS_URL).mock(side_effect=lambda request: _bad_gzip())
    with pytest.raises(KaspiTransportError) as info:
        await client.get_offers("900000005", "710000000", "90100003")
    assert info.value.kind == "DecodingError"
    assert route.call_count == 3
    assert fake_time.sleeps == [1.0, 3.0]
    _assert_clean(info.value)


async def test_decoding_error_once_then_success(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т69 (б): один сбой декодирования, затем успех."""
    route = mock_kaspi.post(OFFERS_URL).mock(
        side_effect=[_bad_gzip(), httpx.Response(200, json=load_fixture("offers.json"))]
    )
    assert len((await client.get_offers("900000005", "710000000", "90100003")).offers) == 7
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("failure", "kind"),
    [(httpx.InvalidURL("x"), "InvalidURL"), (RuntimeError("boom-marker"), "RuntimeError")],
)
async def test_other_exceptions_not_retried(
    client: KaspiClient, mock_kaspi: respx.MockRouter, failure: Exception, kind: str
) -> None:
    """Т69 (в, г): прочие `Exception` - сразу `KaspiTransportError`, без повтора и текста сбоя."""
    route = mock_kaspi.post(OFFERS_URL).mock(side_effect=failure)
    with pytest.raises(KaspiTransportError) as info:
        await client.get_offers("900000005", "710000000", "90100003")
    assert info.value.kind == kind
    assert route.call_count == 1
    assert "boom-marker" not in str(info.value)
    assert "boom-marker" not in repr(info.value)
    _assert_clean(info.value)


async def test_non_http_failure_logged_as_warning_with_class_name_only(
    client: KaspiClient, mock_kaspi: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """Сбой не из `httpx.HTTPError` (вероятно, свой код) - WARNING с классом, без текста."""
    mock_kaspi.post(OFFERS_URL).mock(side_effect=RuntimeError("boom-marker"))
    with caplog.at_level("DEBUG", logger="kaspisa.kaspi"), pytest.raises(KaspiTransportError):
        await client.get_offers("900000005", "710000000", "90100003")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert "boom-marker" not in caplog.text


async def test_cancelled_error_not_caught(
    client: KaspiClient, mock_kaspi: respx.MockRouter
) -> None:
    """Т69 (д): отмена задачи проходит наружу."""

    def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    mock_kaspi.post(OFFERS_URL).mock(side_effect=cancel)
    with pytest.raises(asyncio.CancelledError):
        await client.get_offers("900000005", "710000000", "90100003")
