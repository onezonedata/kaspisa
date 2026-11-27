"""Т7, Т69, Т70: ограничение попыток входа, регистраций и сброса."""

import asyncio
from typing import Any

from api_testing import (
    SERVICE_PASSWORD,
    api_cipher,
    api_database_url,
    deps,
    make_api,
    redis_client,
    redis_prefix,
    signed_in,
)

from kaspisa.api import ApiDeps

__all__ = ["api_cipher", "api_database_url", "deps", "redis_client", "redis_prefix"]

WRONG = {"email": "a@example.com", "password": "wrong-password-1"}
RIGHT = {"email": "a@example.com", "password": SERVICE_PASSWORD}


async def test_pair_limit_is_per_email_and_ip(deps: ApiDeps, redis_prefix: str) -> None:
    await signed_in(deps, "a@example.com", ip="10.9.9.9")
    await signed_in(deps, "b@example.com", ip="10.9.9.9")
    one = make_api(deps, ip="10.0.0.1")
    for _ in range(10):
        assert (await one.post("/api/auth/login", json=WRONG)).status_code == 401

    blocked = await one.post("/api/auth/login", json=RIGHT)
    other_ip = await make_api(deps, ip="10.0.0.2").post("/api/auth/login", json=RIGHT)
    other_email = await one.post(
        "/api/auth/login", json={"email": "b@example.com", "password": SERVICE_PASSWORD}
    )

    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_attempts"
    assert 0 < blocked.json()["retry_after_seconds"] <= 900
    assert other_ip.status_code == 200
    assert other_email.status_code == 200
    # Успешный вход с адреса чужой пары не снимает блок; снятие ключей пары снимает.
    keys: list[Any] = [k async for k in deps.redis.scan_iter(f"{redis_prefix}:api:login_fail:*")]
    pair_keys = [k for k in keys if k.decode().endswith(":10.0.0.1")]
    assert pair_keys
    await deps.redis.delete(*pair_keys)
    assert (await one.post("/api/auth/login", json=RIGHT)).status_code == 200


async def test_email_limit_across_addresses(deps: ApiDeps) -> None:
    await signed_in(deps, "c@example.com", ip="10.9.9.9")
    wrong = {"email": "c@example.com", "password": "wrong-password-1"}
    for index in range(100):
        response = await make_api(deps, ip=f"10.0.1.{index}").post("/api/auth/login", json=wrong)
        assert response.status_code == 401

    blocked = await make_api(deps, ip="10.0.0.3").post(
        "/api/auth/login", json={"email": "c@example.com", "password": SERVICE_PASSWORD}
    )

    assert blocked.status_code == 429


async def test_parallel_wrong_logins_are_counted_before_check(deps: ApiDeps) -> None:
    await signed_in(deps, "a@example.com", ip="10.9.9.9")
    client = make_api(deps, ip="10.0.0.1")

    attempts = (client.post("/api/auth/login", json=WRONG) for _ in range(20))
    responses = await asyncio.gather(*attempts)

    codes = sorted(r.status_code for r in responses)
    assert codes == [401] * 10 + [429] * 10


async def test_register_and_reset_rate_limits(deps: ApiDeps) -> None:
    client = make_api(deps, ip="10.0.0.1")
    for index in range(20):
        response = await client.post(
            "/api/auth/register",
            json={"email": f"user{index}@example.com", "password": SERVICE_PASSWORD},
        )
        # Первая регистрация проходит, остальные: регистрация закрыта (ADR 0027).
        assert response.status_code == (201 if index == 0 else 403)
        if index:
            assert response.json()["code"] == "registration_closed"

    blocked = await client.post(
        "/api/auth/register", json={"email": "blocked@example.com", "password": SERVICE_PASSWORD}
    )
    elsewhere = await make_api(deps, ip="10.0.0.2").post(
        "/api/auth/register", json={"email": "late@example.com", "password": SERVICE_PASSWORD}
    )

    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_attempts"
    assert elsewhere.status_code == 403
    assert elsewhere.json()["code"] == "registration_closed"
    async with deps.database.system() as system:
        assert (await system.find_user_by_email("blocked@example.com")) is None
        assert (await system.find_user_by_email("late@example.com")) is None
        assert (await system.find_user_by_email("user0@example.com")) is not None
    reset = make_api(deps, ip="10.0.0.5")
    body = {"token": "bad-token", "new_password": "service-pass-new1"}
    for _ in range(20):
        assert (await reset.post("/api/auth/password-reset", json=body)).status_code == 400
    assert (await reset.post("/api/auth/password-reset", json=body)).status_code == 429


async def test_successful_login_resets_the_pair_counter(deps: ApiDeps) -> None:
    await signed_in(deps, "a@example.com", ip="10.9.9.9")
    one = make_api(deps, ip="10.0.0.1")
    for _ in range(9):
        assert (await one.post("/api/auth/login", json=WRONG)).status_code == 401
    assert (await one.post("/api/auth/login", json=RIGHT)).status_code == 200

    # После успеха счёт пары начинается заново: ещё 10 неверных попыток проходят без 429.
    for _ in range(10):
        assert (await one.post("/api/auth/login", json=WRONG)).status_code == 401
    assert (await one.post("/api/auth/login", json=WRONG)).status_code == 429
