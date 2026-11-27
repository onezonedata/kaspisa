"""Т19-Т27, Т47-Т49: кнопка «Узнать цены» (ADR 0030): разовое чтение публичной карточки.

Тесты Т19-Т23 и Т26 идут на настоящем `KaspiClient` с общим ограничителем и `respx`;
Т24, Т47, Т48 подменяют `get_offers` заглушкой.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from api_kaspi_testing import load_fixture
from api_testing import (
    FakeConnectClient,
    api_cipher,
    api_database_url,
    deps,
    fake_client,
    make_api,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_price_write,
    seed_product,
    seed_shop,
    signed_in,
    sql_exec,
    sql_scalar,
    tenant_of,
)
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api import deps as api_deps
from kaspisa.kaspi import KaspiClient
from kaspisa.worker.redis_limits import LimiterUnavailableError

__all__ = ["api_cipher", "api_database_url", "deps", "raw_engine", "redis_client", "redis_prefix"]

MASTER = "900000005"
OFFERS_URL = f"https://kaspi.kz/yml/offer-view/offers/{MASTER}"
OWN = "90100001"
CITY = "750000000"
ASTANA = "710000000"
OBSERVATION_COLUMNS = (
    "current_price, position, position_over, competitors_count, first_place_price, "
    "last_checked_at, decision_reason, decision_target_price, decision_write, skip_reason, tracked"
)


async def _no_sleep(delay: float) -> None:
    return None


class Env:
    """Вошедший пользователь, магазин, товар и клиент HTTP (с настоящим клиентом Kaspi)."""

    def __init__(
        self,
        deps: ApiDeps,
        engine: AsyncEngine,
        tenant: uuid.UUID,
        shop_id: uuid.UUID,
        product_id: int,
        api: httpx.AsyncClient,
    ) -> None:
        self.deps = deps
        self.engine = engine
        self.tenant = tenant
        self.shop_id = shop_id
        self.product_id = product_id
        self.api = api

    def url(self, product_id: int | None = None) -> str:
        return f"/api/shops/{self.shop_id}/products/{product_id or self.product_id}/price-read"

    async def post(self, product_id: int | None = None) -> httpx.Response:
        return await self.api.post(self.url(product_id))

    async def observation(self, product_id: int | None = None) -> str:
        """Столбцы наблюдения, решения и пропуска одной строкой (для сравнения до и после)."""
        found = await sql_scalar(
            self.engine,
            f"SELECT row(p.*)::text FROM (SELECT {OBSERVATION_COLUMNS} FROM product "
            "WHERE id = :id) p",
            id=product_id or self.product_id,
        )
        return str(found)

    async def timer_exists(self, product_id: int | None = None) -> bool:
        key = f"{self.deps.worker_prefix}:api:price_read:{product_id or self.product_id}"
        return bool(await self.deps.redis.exists(key))


async def _env(
    deps: ApiDeps,
    engine: AsyncEngine,
    *,
    real: bool = True,
    merchant_uid: str | None = OWN,
    city_id: str | None = CITY,
    state: str = "connected",
    master_sku: str = MASTER,
) -> Env:
    signed = await signed_in(deps)
    tenant = await tenant_of(engine, "a@example.com")
    shop_id = await seed_shop(
        deps, engine, tenant, state=state, merchant_uid=merchant_uid, city_id=city_id
    )
    product_id = await seed_product(engine, tenant, shop_id, master_sku=master_sku)
    if real:
        deps = dataclasses.replace(deps, client=KaspiClient(limiter=deps.limiter, sleep=_no_sleep))
        api = make_api(deps)
        api.cookies.update(signed.cookies)
    else:
        api = signed
    return Env(deps, engine, tenant, shop_id, product_id, api)


def _page(total: int = 7) -> dict[str, Any]:
    page: dict[str, Any] = load_fixture("offers.json")
    page["total"] = total
    return page


def _mock() -> respx.MockRouter:
    return respx.mock(assert_all_called=False, assert_all_mocked=True)


def _numbers(body: dict[str, Any]) -> tuple[Any, ...]:
    return (
        body["current_price"],
        body["position"],
        body["position_over"],
        body["competitors_count"],
        body["first_place_price"],
    )


async def test_untracked_product_read_updates_observation(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_client: Redis
) -> None:
    """Т70."""
    env = await _env(deps, raw_engine)
    with _mock() as mock:
        route = mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post()
    assert response.status_code == 200, response.text
    body = response.json()
    assert _numbers(body) == (2998, 3, None, 6, 2643)
    assert body["tracked"] is False
    assert body["id"] == env.product_id
    assert body["pending_write"] is None
    assert "price_read" not in body
    assert datetime.fromisoformat(body["last_checked_at"]) == env.deps.clock()
    assert (body["decision_reason"], body["skip_reason"]) == (None, None)
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["page"] == 0

    for table in ("price_change", "price_write"):
        count = await sql_scalar(raw_engine, f"SELECT count(*) FROM {table}")
        assert count == 0
    stored = await env.api.get(f"/api/shops/{env.shop_id}/products/{env.product_id}")
    assert stored.json() == body
    listed = await env.api.get(f"/api/shops/{env.shop_id}/products")
    assert listed.json()["items"][0] == body
    assert await redis_client.exists(f"{deps.worker_prefix}:rl:kaspi.kz")


async def test_own_shop_absent_and_second_page(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т71."""
    env = await _env(deps, raw_engine)
    await sql_exec(raw_engine, "UPDATE shop SET merchant_uid = '90200000'")
    await sql_exec(
        raw_engine, "UPDATE product SET current_price = 3100 WHERE id = :i", i=env.product_id
    )
    with _mock() as mock:
        route = mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post()
    assert response.status_code == 200, response.text
    body = response.json()
    assert route.call_count == 1
    assert _numbers(body) == (3100, None, None, 7, 2643)  # текущая цена не стирается

    empty = await seed_product(raw_engine, env.tenant, env.shop_id, master_sku=MASTER)
    with _mock() as mock:
        mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post(empty)
    assert response.status_code == 200, response.text
    assert _numbers(response.json()) == (None, None, None, 7, 2643)  # NULL остаётся NULL

    other = await seed_product(
        raw_engine, env.tenant, env.shop_id, master_sku=MASTER, current_price=3100
    )
    with _mock() as mock:
        second = {
            "offers": [
                {
                    "merchantId": f"9010001{i}",
                    "merchantName": f"Магазин {i}",
                    "price": 5000 + 100 * (i - 1),
                }
                for i in range(1, 5)
            ],
            "total": 12,
        }
        route = mock.post(OFFERS_URL).mock(
            side_effect=[
                httpx.Response(200, json=_page(total=12)),
                httpx.Response(200, json=second),
            ]
        )
        response = await env.post(other)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [json.loads(c.request.content)["page"] for c in route.calls] == [0, 1]
    assert _numbers(body) == (3100, None, 11, 12, 2643)


async def test_read_without_own_offer_keeps_min_guard(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т88: чтение без своего магазина не снимает защиту от опасного снижения минимума."""
    env = await _env(deps, raw_engine)
    await sql_exec(raw_engine, "UPDATE shop SET merchant_uid = '90200000'")
    await sql_exec(
        raw_engine,
        "UPDATE product SET min_price = 2000, min_price_anchor = 2000, max_price = 3500, "
        "current_price = 3000 WHERE id = :i",
        i=env.product_id,
    )
    with _mock() as mock:
        mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post()
    assert response.status_code == 200, response.text
    assert response.json()["current_price"] == 3000
    patched = await env.api.patch(
        f"/api/shops/{env.shop_id}/products/{env.product_id}", json={"min_price": 1700}
    )
    assert patched.status_code == 403, patched.text
    assert patched.json()["code"] == "password_required"
    stored = await sql_scalar(
        raw_engine, "SELECT min_price FROM product WHERE id = :i", i=env.product_id
    )
    assert stored == 2000


async def test_repeat_within_n_seconds_is_rejected(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т21."""
    env = await _env(deps, raw_engine)
    other = await seed_product(raw_engine, env.tenant, env.shop_id, master_sku=MASTER)
    with _mock() as mock:
        route = mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        first = await env.post()
        second = await env.post()
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["code"] == "price_read_too_early"
        assert 1 <= second.json()["retry_after_seconds"] <= api_deps.PRICE_READ_REPEAT_SECONDS
        assert route.call_count == 1
        stored = await env.api.get(f"/api/shops/{env.shop_id}/products/{env.product_id}")
        assert stored.json() == first.json()
        third = await env.post(other)
    assert third.status_code == 200, third.text


async def test_shared_limiter_busy(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т22."""
    env = await _env(deps, raw_engine)
    before = await env.observation()
    await env.deps.limiter.penalize("kaspi.kz", 120)
    with _mock() as mock:
        route = mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        first = await env.post()
        assert first.status_code == 429
        assert first.json() == {"code": "kaspi_busy", "retry_after_seconds": 120, "fields": None}
        assert route.call_count == 0
        assert await env.observation() == before
        assert not await env.timer_exists()
        second = await env.post()
    assert second.status_code == 429
    assert second.json()["code"] == "kaspi_busy"


async def test_kaspi_errors(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т23."""
    env = await _env(deps, raw_engine)
    before = await env.observation()
    cases: list[tuple[httpx.Response, int, str, int | None]] = [
        (httpx.Response(429, headers={"Retry-After": "90"}), 429, "kaspi_busy", 90),
        (httpx.Response(429), 429, "kaspi_busy", 60),
        (httpx.Response(500), 502, "kaspi_unavailable", None),
        (httpx.Response(200, json={"unexpected": 1}), 502, "kaspi_unavailable", None),
    ]
    for reply, status, code, pause in cases:
        await env.deps.redis.delete(f"{env.deps.worker_prefix}:api:price_read:{env.product_id}")
        await env.deps.redis.delete(f"{env.deps.worker_prefix}:rl:kaspi.kz")
        with _mock() as mock:
            mock.post(OFFERS_URL).mock(return_value=reply)
            response = await env.post()
        assert response.status_code == status, response.text
        assert response.json()["code"] == code
        retry = response.json()["retry_after_seconds"]
        if pause is not None:
            assert retry == pause
            assert await env.deps.limiter.pending_delay("kaspi.kz") >= pause - 1
        else:
            assert 1 <= retry <= api_deps.PRICE_READ_REPEAT_SECONDS
        assert await env.observation() == before


async def test_limiter_unavailable(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т24."""
    env = await _env(deps, raw_engine, real=False)
    before = await env.observation()
    fake_client(env.deps).queue("get_offers", LimiterUnavailableError("ConnectionError"))
    response = await env.post()
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert await env.observation() == before
    assert not await env.timer_exists()


async def test_preconditions(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т25."""
    env = await _env(deps, raw_engine, city_id=None)
    second_shop = await seed_shop(
        deps, raw_engine, env.tenant, state="connected", merchant_uid="90100002", city_id=CITY
    )
    foreign = await seed_product(raw_engine, env.tenant, second_shop, master_sku=MASTER)
    response = await env.post(foreign)
    assert response.status_code == 404

    # Т54: без города сверки кнопка читает в Астане, город магазина не меняется.
    with _mock() as mock:
        route = mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post()
    assert response.status_code == 200, response.text
    assert _numbers(response.json()) == (2998, 3, None, 6, 2643)
    assert json.loads(route.calls[0].request.content)["cityId"] == ASTANA
    assert f"?c={ASTANA}" in route.calls[0].request.headers["Referer"]
    shop_city = await sql_scalar(
        raw_engine, "SELECT city_id FROM shop WHERE id = :id", id=env.shop_id
    )
    assert shop_city is None

    await sql_exec(
        raw_engine,
        "UPDATE shop SET merchant_uid = NULL, connection_state = 'not_connected', "
        "session_enc = NULL WHERE id = :id",
        id=env.shop_id,
    )
    response = await env.post(await seed_product(raw_engine, env.tenant, env.shop_id))
    assert (response.status_code, response.json()["code"]) == (409, "operation_not_allowed")

    await sql_exec(
        raw_engine,
        "UPDATE shop SET merchant_uid = :u, connection_state = 'reauth_required', "
        "session_enc = 'x' WHERE id = :id",
        u=OWN,
        id=env.shop_id,
    )
    with _mock() as mock:
        mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        other = await seed_product(raw_engine, env.tenant, env.shop_id, master_sku=MASTER)
        assert (await env.post(other)).status_code == 200


async def test_timeout_maps_to_kaspi_unavailable(
    deps: ApiDeps, raw_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т26."""
    monkeypatch.setattr(api_deps, "PRICE_READ_TIMEOUT_SECONDS", 0.01)
    env = await _env(deps, raw_engine)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=_page())

    with _mock() as mock:
        mock.post(OFFERS_URL).mock(side_effect=slow)
        response = await env.post()
    assert response.status_code == 502
    assert response.json()["code"] == "kaspi_unavailable"
    assert 1 <= response.json()["retry_after_seconds"] <= api_deps.PRICE_READ_REPEAT_SECONDS
    assert 0 <= await env.deps.limiter.pending_delay("kaspi.kz") <= 1
    assert await env.timer_exists()


async def test_one_read_per_shop(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т47."""
    env = await _env(deps, raw_engine)
    token = await env.deps.price_read_lock.acquire(env.shop_id, 40_000)
    assert token is not None
    with _mock() as mock:
        route = mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        busy = await env.post()
        assert busy.status_code == 409
        assert busy.json()["code"] == "price_read_in_progress"
        assert busy.json()["retry_after_seconds"] is None
        assert route.call_count == 0
        assert not await env.timer_exists()
        await env.deps.price_read_lock.release(env.shop_id, token)
        assert (await env.post()).status_code == 200
        again = await env.deps.price_read_lock.acquire(env.shop_id, 40_000)
        assert again is not None
        await env.deps.price_read_lock.release(env.shop_id, again)

        other = await seed_product(raw_engine, env.tenant, env.shop_id, master_sku=MASTER)
        route.mock(return_value=httpx.Response(500))
        assert (await env.post(other)).status_code == 502
        final = await env.deps.price_read_lock.acquire(env.shop_id, 40_000)
    assert final is not None


async def test_observation_not_saved_after_master_sku_or_city_change(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т76."""
    env = await _env(deps, raw_engine, real=False, master_sku="M1")
    client: FakeConnectClient = fake_client(env.deps)

    async def change_sku() -> None:
        await sql_exec(
            raw_engine, "UPDATE product SET master_sku = 'M2' WHERE id = :id", id=env.product_id
        )

    before = await env.observation()
    client.before_return = change_sku
    response = await env.post()
    assert (response.status_code, response.json()["code"]) == (409, "state_changed")
    assert await env.observation() == before

    await env.deps.redis.delete(f"{env.deps.worker_prefix}:api:price_read:{env.product_id}")

    async def change_city() -> None:
        await sql_exec(raw_engine, "UPDATE shop SET city_id = '710000000'")

    client.before_return = change_city
    response = await env.post()
    assert (response.status_code, response.json()["code"]) == (409, "state_changed")
    assert await env.observation() == before

    # (г) города сверки не было, во время чтения его задали.
    await sql_exec(raw_engine, "UPDATE shop SET city_id = NULL")
    await env.deps.redis.delete(f"{env.deps.worker_prefix}:api:price_read:{env.product_id}")

    async def set_city() -> None:
        await sql_exec(raw_engine, "UPDATE shop SET city_id = :c", c=CITY)

    client.before_return = set_city
    response = await env.post()
    assert (response.status_code, response.json()["code"]) == (409, "state_changed")
    assert await env.observation() == before

    await env.deps.redis.delete(f"{env.deps.worker_prefix}:api:price_read:{env.product_id}")

    async def delete_product() -> None:
        await sql_exec(raw_engine, "DELETE FROM product WHERE id = :id", id=env.product_id)

    client.before_return = delete_product
    response = await env.post()
    assert (response.status_code, response.json()["code"]) == (404, "not_found")


async def test_invalid_master_sku(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т77."""
    env = await _env(deps, raw_engine, master_sku="bad sku")
    before = await env.observation()
    with _mock() as mock:
        route = mock.post(url__startswith="https://kaspi.kz/").mock(
            return_value=httpx.Response(200, json=_page())
        )
        response = await env.post()
    assert (response.status_code, response.json()["code"]) == (500, "internal_error")
    assert route.call_count == 0
    assert not await env.timer_exists()
    assert await env.observation() == before


async def test_read_on_tracked_product_resets_decision_not_journal(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    """Т72 (часть API): решение бота сброшено, строк журнала столько же, сколько было."""
    env = await _env(deps, raw_engine)
    await sql_exec(
        raw_engine,
        "UPDATE product SET tracked = true, min_price = 2000, max_price = 3500, "
        "current_price = 2998, decision_reason = 'already_at_target', "
        "decision_target_price = 2998 WHERE id = :id",
        id=env.product_id,
    )
    await sql_exec(
        raw_engine,
        "INSERT INTO price_change (tenant_id, shop_id, product_id, old_price, new_price, "
        "reason, target_price, competitor_price, result) VALUES (:t, :s, :p, 2998, 2998, "
        "'already_at_target', 2998, 2643, 'no_change')",
        t=env.tenant,
        s=env.shop_id,
        p=env.product_id,
    )
    with _mock() as mock:
        mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post()
    assert response.status_code == 200, response.text
    body = response.json()
    assert _numbers(body) == (2998, 3, None, 6, 2643)
    assert body["tracked"] is True
    assert (body["decision_reason"], body["decision_target_price"], body["decision_write"]) == (
        None,
        None,
        None,
    )
    assert (body["min_price"], body["max_price"]) == (2000, 3500)
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM price_change") == 1
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM price_write") == 0


async def test_read_response_carries_pending_write(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Ответ — `ProductOut`, как `GET` товара: открытая запись цены видна в `pending_write`."""
    env = await _env(deps, raw_engine)
    await seed_price_write(
        raw_engine,
        env.tenant,
        env.shop_id,
        env.product_id,
        status="receipt",
        sent_at=env.deps.clock() - timedelta(seconds=10),
    )
    with _mock() as mock:
        mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post()
    assert response.status_code == 200, response.text
    pending = response.json()["pending_write"]
    assert pending is not None
    assert pending["status"] == "receipt"
    got = await env.api.get(f"/api/shops/{env.shop_id}/products/{env.product_id}")
    assert got.json()["pending_write"] == pending
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM price_write") == 1


async def test_read_keeps_refused_decision(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т73."""
    env = await _env(deps, raw_engine)
    await sql_exec(
        raw_engine,
        "UPDATE product SET tracked = true, min_price = 2000, max_price = 3500, "
        "decision_reason = 'undercut_first', decision_target_price = 2642, "
        "decision_write = 'refused', refusal_reason = 'no_stores', "
        "refusal_since = :t, refusal_fingerprint = 'fp' WHERE id = :id",
        t=env.deps.clock(),
        id=env.product_id,
    )
    with _mock() as mock:
        mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post()
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["decision_reason"], body["decision_target_price"], body["decision_write"]) == (
        "undercut_first",
        2642,
        "refused",
    )
    assert _numbers(body) == (2998, 3, None, 6, 2643)
    assert body["refusal_reason"] == "no_stores"


async def test_read_keeps_skip_and_refusal_columns(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    """Т74."""
    env = await _env(deps, raw_engine)
    await sql_exec(
        raw_engine,
        "UPDATE product SET tracked = true, skip_reason = 'listing_missing', skipped_at = :t, "
        "refusal_reason = 'no_stores', refusal_since = :t, retry_at = :r WHERE id = :id",
        t=env.deps.clock(),
        r=env.deps.clock(),
        id=env.product_id,
    )
    columns = (
        "skip_reason, skipped_at, refusal_reason, refusal_since, retry_at, refusal_fingerprint"
    )
    query = f"SELECT row(p.*)::text FROM (SELECT {columns} FROM product WHERE id = :id) p"
    before = await sql_scalar(raw_engine, query, id=env.product_id)
    with _mock() as mock:
        mock.post(OFFERS_URL).mock(return_value=httpx.Response(200, json=_page()))
        response = await env.post()
    assert response.status_code == 200, response.text
    assert await sql_scalar(raw_engine, query, id=env.product_id) == before
    assert response.json()["skip_reason"] == "listing_missing"


async def test_lock_is_separate_from_sync_lock_and_has_a_ttl(
    deps: ApiDeps, raw_engine: AsyncEngine, redis_prefix: str
) -> None:
    """Блокировка чтения не пересекается с блокировкой загрузки каталога и истекает сама."""
    from kaspisa.worker.redis_limits import SyncLock

    env = await _env(deps, raw_engine, real=False)
    sync_token = await SyncLock(env.deps.redis, prefix=redis_prefix).acquire(env.shop_id, 60_000)
    assert sync_token is not None
    ttls: list[int] = []

    async def probe() -> None:
        keys = [k async for k in env.deps.redis.scan_iter(f"{redis_prefix}:price_read:*")]
        ttls.extend([int(await env.deps.redis.pttl(k)) for k in keys])

    fake_client(env.deps).before_return = probe
    response = await env.post()
    assert response.status_code == 200, response.text
    assert len(ttls) == 1 and 30_000 < ttls[0] <= 40_000
    # снимается по выходу
    assert [k async for k in env.deps.redis.scan_iter(f"{redis_prefix}:price_read:*")] == []


async def test_repeat_timer_left_is_at_least_one_second(deps: ApiDeps) -> None:
    """Остаток таймера округляется вверх до 1, даже если ключа уже нет."""
    assert await deps.state.price_read_left(987654) == 1
    assert await deps.state.start_price_read_timer(987654) is None
    left = await deps.state.start_price_read_timer(987654)
    assert left is not None and 1 <= left <= api_deps.PRICE_READ_REPEAT_SECONDS
    await deps.state.clear_price_read_timer(987654)
