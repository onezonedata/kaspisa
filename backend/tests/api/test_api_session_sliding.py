"""Н3 (ADR 0010): скользящий срок сессии - 30 дней с последнего использования."""

import hashlib
import json
import time
import uuid

from api_testing import (
    api_cipher,
    api_database_url,
    deps,
    raw_engine,
    redis_client,
    redis_prefix,
    seed_shop,
    signed_in,
    tenant_of,
)
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api.deps import (
    SESSION_ABSOLUTE_TTL_SECONDS,
    SESSION_RENEW_AFTER_SECONDS,
    SESSION_TTL_SECONDS,
)

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

DAY = 86_400


def _keys(deps: ApiDeps, client: AsyncClient) -> tuple[str, str]:
    token = client.cookies["kaspisa_session"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    return f"{deps.state._prefix}:sess:{digest}", ""


async def _members_key(deps: ApiDeps, sess_key: str) -> str:
    keys = [k async for k in deps.redis.scan_iter(f"{deps.state._prefix}:user_sess:*")]
    assert len(keys) == 1
    key = keys[0]
    return key.decode() if isinstance(key, bytes) else str(key)


async def test_old_session_is_renewed_with_cookie(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    members = await _members_key(deps, sess_key)
    await deps.redis.expire(sess_key, 10 * DAY)
    await deps.redis.expire(members, 10 * DAY)

    response = await client.get("/api/shops")

    assert response.status_code == 200
    assert 2591990 < await deps.redis.ttl(sess_key) <= SESSION_TTL_SECONDS
    assert 2591990 < await deps.redis.ttl(members) <= SESSION_TTL_SECONDS
    cookie = response.headers["set-cookie"]
    token = client.cookies["kaspisa_session"]
    assert f"kaspisa_session={token}" in cookie
    assert f"Max-Age={SESSION_TTL_SECONDS}" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie


async def test_no_renewal_within_first_day(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    await deps.redis.expire(sess_key, SESSION_TTL_SECONDS - DAY + 600)

    response = await client.get("/api/shops")

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert await deps.redis.ttl(sess_key) <= SESSION_TTL_SECONDS - DAY + 600


async def test_no_cookie_on_fresh_session(deps: ApiDeps) -> None:
    client = await signed_in(deps)

    response = await client.get("/api/shops")

    assert "set-cookie" not in response.headers


async def test_revoked_session_is_not_resurrected(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    await deps.redis.delete(sess_key)

    response = await client.get("/api/shops")

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert await deps.redis.exists(sess_key) == 0


async def test_renewed_session_is_revoked_after_30_days_from_login(deps: ApiDeps) -> None:
    """Набор сессий пользователя продлевается: выход всех сессий всё ещё работает."""
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    members = await _members_key(deps, sess_key)
    await deps.redis.expire(sess_key, 5 * DAY)
    await deps.redis.expire(members, 5 * DAY)
    assert (await client.get("/api/shops")).status_code == 200
    # Прошло больше 30 дней от входа: без продления набор уже исчез бы.
    await deps.redis.expire(members, 5 * DAY)
    assert await deps.redis.ttl(members) > 0
    user_id = members.rsplit(":", 1)[1]

    await deps.state.revoke_user_sessions(uuid.UUID(user_id))

    assert (await client.get("/api/shops")).status_code == 401


async def test_members_set_outlives_login_plus_30_days(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    members = await _members_key(deps, sess_key)
    await deps.redis.expire(sess_key, 5 * DAY)
    await deps.redis.expire(members, 5 * DAY)

    await client.get("/api/shops")

    assert await deps.redis.ttl(members) > 29 * DAY


async def test_renewal_on_route_returning_response_directly(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    tenant_id = await tenant_of(raw_engine, "a@example.com")
    shop_id = await seed_shop(deps, raw_engine, tenant_id)
    await deps.redis.expire(sess_key, 10 * DAY)

    response = await client.delete(f"/api/shops/{shop_id}")

    assert response.status_code == 204
    assert f"Max-Age={SESSION_TTL_SECONDS}" in response.headers["set-cookie"]


async def test_renewal_boundary_is_exclusive(deps: ApiDeps) -> None:
    """Остаток ровно на пороге (сутки с продления) - ещё без продления; на секунду меньше - с ним.

    TTL в Redis округляется до секунды, поэтому остаток ставится с запасом в миллисекундах.
    """
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    threshold_ms = SESSION_RENEW_AFTER_SECONDS * 1000
    await deps.redis.pexpire(sess_key, threshold_ms + 400)

    at_threshold = await client.get("/api/shops")

    assert "set-cookie" not in at_threshold.headers
    assert await deps.redis.ttl(sess_key) <= SESSION_RENEW_AFTER_SECONDS

    await deps.redis.pexpire(sess_key, threshold_ms - 1000)

    below = await client.get("/api/shops")

    assert "set-cookie" in below.headers
    assert await deps.redis.ttl(sess_key) > SESSION_TTL_SECONDS - 10


async def _age_session(deps: ApiDeps, sess_key: str, days: float) -> None:
    """Состаривает сессию: `created_at` сдвигается в прошлое, срок ключа не меняется."""
    raw = await deps.redis.get(sess_key)
    assert raw is not None
    payload = json.loads(raw)
    payload["created_at"] = int(time.time() - days * DAY)
    await deps.redis.set(sess_key, json.dumps(payload), keepttl=True)


def test_absolute_ttl_is_90_days() -> None:
    assert SESSION_ABSOLUTE_TTL_SECONDS == 90 * DAY


async def test_renewal_is_capped_by_absolute_limit(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    members = await _members_key(deps, sess_key)
    await _age_session(deps, sess_key, 89)
    await deps.redis.expire(sess_key, 10 * DAY)

    response = await client.get("/api/shops")

    assert response.status_code == 200
    ttl = await deps.redis.ttl(sess_key)
    assert DAY - 60 < ttl <= DAY
    assert await deps.redis.ttl(members) > 29 * DAY
    assert f"Max-Age={ttl}" in response.headers["set-cookie"] or (
        f"Max-Age={ttl + 1}" in response.headers["set-cookie"]
    )
    assert f"Max-Age={SESSION_TTL_SECONDS}" not in response.headers["set-cookie"]


async def test_no_renewal_cookie_storm_near_the_end(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    await _age_session(deps, sess_key, 89)
    first = await client.get("/api/shops")
    second = await client.get("/api/shops")

    assert "set-cookie" in first.headers
    assert "set-cookie" not in second.headers


async def test_session_older_than_90_days_is_invalid(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    await _age_session(deps, sess_key, 90.01)

    response = await client.get("/api/shops")

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert await deps.redis.exists(sess_key) == 0
    assert (await client.get("/api/shops")).status_code == 401


async def test_session_without_created_at_is_invalid(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    sess_key, _ = _keys(deps, client)
    raw = await deps.redis.get(sess_key)
    assert raw is not None
    payload = json.loads(raw)
    del payload["created_at"]
    await deps.redis.set(sess_key, json.dumps(payload), keepttl=True)

    assert (await client.get("/api/shops")).status_code == 401
