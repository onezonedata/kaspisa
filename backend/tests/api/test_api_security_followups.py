"""Замечания ревью этапа 6: выход, no-store, created_at в сессии."""

import hashlib
import json
import uuid
from typing import Any

from api_testing import (
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    deps,
    make_api,
    raw_engine,
    redis_client,
    redis_prefix,
    signed_in,
)

from kaspisa.api import ApiDeps
from kaspisa.db import TenantId

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

H = {"X-Requested-With": "kaspisa"}


async def test_logout_all_revokes_all_sessions_of_user(deps: ApiDeps) -> None:
    first = await signed_in(deps, "a@example.com")
    second = make_api(deps)
    login = await second.post(
        "/api/auth/login", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )
    assert login.status_code == 200
    other = await signed_in(deps, "b@example.com")

    response = await first.post("/api/auth/logout-all", headers=H)

    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert (await first.get("/api/auth/me")).status_code == 401
    assert (await second.get("/api/auth/me")).status_code == 401
    assert (await other.get("/api/auth/me")).status_code == 200


async def test_logout_ends_only_current_session(deps: ApiDeps) -> None:
    first = await signed_in(deps, "a@example.com")
    second = make_api(deps)
    await second.post(
        "/api/auth/login", json={"email": "a@example.com", "password": SERVICE_PASSWORD}
    )
    other = await signed_in(deps, "b@example.com")

    response = await first.post("/api/auth/logout", headers=H)

    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert (await first.get("/api/auth/me")).status_code == 401
    assert (await second.get("/api/auth/me")).status_code == 200
    assert (await other.get("/api/auth/me")).status_code == 200


async def test_logout_all_without_or_with_bad_session_touches_nothing(deps: ApiDeps) -> None:
    alive = await signed_in(deps, "a@example.com")
    anonymous = make_api(deps)
    forged = make_api(deps)
    forged.cookies.set("kaspisa_session", "forged")

    for client in (anonymous, forged):
        response = await client.post("/api/auth/logout-all", headers=H)
        assert response.status_code == 204

    assert (await alive.get("/api/auth/me")).status_code == 200


async def test_api_responses_are_no_store(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    ok = await client.get("/api/auth/me")
    anonymous = make_api(deps)
    denied = await anonymous.get("/api/auth/me")
    csrf = await client.post("/api/auth/logout")  # без CSRF-заголовка
    missing = await client.get("/api/no-such-route")

    for response in (ok, denied, csrf, missing):
        assert response.headers["cache-control"] == "no-store"


async def test_session_has_created_at_and_session_without_it_is_invalid(deps: ApiDeps) -> None:
    client = await signed_in(deps)
    keys = [k async for k in deps.redis.scan_iter(f"{deps.state._prefix}:sess:*")]
    assert len(keys) == 1
    raw = await deps.redis.get(keys[0])
    assert raw is not None
    payload = json.loads(raw)
    assert isinstance(payload["created_at"], int) and payload["created_at"] > 0

    del payload["created_at"]
    await deps.redis.set(keys[0], json.dumps(payload), keepttl=True)

    assert (await client.get("/api/auth/me")).status_code == 401
    assert await deps.redis.exists(keys[0]) == 0


async def test_revoke_user_sessions_is_atomic_with_concurrent_login(deps: ApiDeps) -> None:
    user_id = uuid.uuid4()
    tenant_id = TenantId(uuid.uuid4())
    old = [await deps.state.create_session(user_id, tenant_id) for _ in range(2)]
    members = deps.state._key("user_sess", str(user_id))

    created: list[str] = []
    original = deps.redis.smembers

    async def smembers_then_login(name: Any) -> Any:
        # вход, попавший между чтением набора и удалением, если они раздельные команды
        result = await original(name)
        created.append(await deps.state.create_session(user_id, tenant_id))
        return result

    deps.redis.smembers = smembers_then_login  # type: ignore[method-assign]
    try:
        await deps.state.revoke_user_sessions(user_id)
    finally:
        deps.redis.smembers = original  # type: ignore[method-assign]

    for token in old:
        assert (await deps.state.get_session(token))[0] is None
    # любая живая сессия пользователя обязана быть в наборе (иначе её нельзя отозвать)
    live = [k async for k in deps.redis.scan_iter(f"{deps.state._prefix}:sess:*")]
    assert len(live) == await deps.redis.scard(members)

    after = await deps.state.create_session(user_id, tenant_id)
    assert (await deps.state.get_session(after))[0] is not None
    assert await deps.redis.scard(members) == len(live) + 1


async def test_logout_all_with_invalid_session_keeps_other_sessions(deps: ApiDeps) -> None:
    """Украденная сессия без `created_at` не даёт выкинуть продавца со всех устройств."""
    user_id = uuid.uuid4()
    tenant_id = TenantId(uuid.uuid4())
    stale = await deps.state.create_session(user_id, tenant_id)
    alive = await deps.state.create_session(user_id, tenant_id)
    stale_key = deps.state._key("sess", hashlib.sha256(stale.encode()).hexdigest())
    raw = await deps.redis.get(stale_key)
    assert raw is not None
    payload = json.loads(raw)
    del payload["created_at"]
    await deps.redis.set(stale_key, json.dumps(payload), keepttl=True)

    await deps.state.revoke_sessions_of_token(stale)

    assert (await deps.state.get_session(alive))[0] is not None
    assert await deps.redis.exists(stale_key) == 0
