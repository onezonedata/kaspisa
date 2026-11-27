"""Т27, Т28: единственный вызов `set_price` и свойство границ цены."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    Rig,
    execute,
    fetch,
    journal,
    redis_client,
    redis_prefix,
    rig,
    worker_cipher,
    worker_database_url,
)
from writer_testing import decision, prepare

import kaspisa

WORKER = Path(kaspisa.__file__).resolve().parent / "worker"


def test_exactly_one_set_price_call_in_write_price() -> None:
    """Т27: один вызов `.set_price(` во всём worker/, внутри `write_price`."""
    found: list[tuple[str, str]] = []
    for path in sorted(WORKER.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set_price"
                ):
                    found.append((path.name, function.name))
    # вложенные функции видны и во внешней: сворачиваем в множество вхождений по файлу
    assert {file for file, _ in found} == {"writer.py"}
    assert "write_price" in {name for _, name in found}
    total = sum(
        1
        for path in WORKER.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "set_price"
    )
    assert total == 1


bound = st.one_of(st.none(), st.integers(1, 10**6))


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    low=bound,
    high=bound,
    current=st.integers(1, 10**6),
    new=st.integers(-5, 10**6 + 5),
    mode=st.sampled_from(["off", "dry_run", "live"]),
    flag=st.booleans(),
)
async def test_only_prices_inside_bounds_are_sent(
    low: int | None,
    high: int | None,
    current: int,
    new: int,
    mode: str,
    flag: bool,
    rig: Rig,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Т10: цена уходит ⇔ режим `live`, запрет сервера снят и цена в границах."""
    from kaspisa.worker.writer import WriteOutcome

    if low is not None and high is not None and low > high:
        return
    p = await prepare(rig, redis_client, redis_prefix, dry_run=flag)
    p.client.price = current
    await execute(rig, "UPDATE shop SET bot_mode=:m WHERE id=:s", m=mode, s=p.shop.shop_id)
    p.sctx.access.shop.bot_mode = mode
    await execute(
        rig,
        "UPDATE product SET min_price=:lo, max_price=:hi WHERE id=:p",
        lo=low,
        hi=high,
        p=p.product_id,
    )
    await execute(rig, "DELETE FROM price_write WHERE shop_id=:s", s=p.shop.shop_id)
    await execute(rig, "DELETE FROM price_change WHERE shop_id=:s", s=p.shop.shop_id)
    product = await p.product()
    result = await p.write(new, current, product=product, decision=decision(new))
    allowed = low is not None and high is not None and low <= new <= high and new != current
    writes = mode == "live" and not flag
    if allowed and not writes:  # граница соблюдена, но запись запрещена: только строка журнала
        assert result.outcome is WriteOutcome.DRY_RUN
        assert p.client.sent == []
        assert p.client.calls == []
        assert [row[3] for row in await journal(rig, p.shop.shop_id)] == ["dry_run"]
        assert (
            await fetch(rig, "SELECT 1 FROM price_write WHERE shop_id=:s", s=p.shop.shop_id) == []
        )
    elif allowed:
        assert result.outcome is WriteOutcome.SENT
        assert low is not None and high is not None
        sent = [price for _, prices in p.client.sent for price in prices.values()]
        assert sent == [new]
        assert all(low <= price <= high and price != current for price in sent)
    else:
        in_bounds = low is not None and high is not None and low <= new <= high
        if in_bounds:  # границы соблюдены, цена не меняется
            assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "same_price")
        else:
            assert result.outcome is WriteOutcome.BLOCKED_BOUNDS
        assert p.client.sent == []
        assert p.client.calls == []
        assert (
            await fetch(rig, "SELECT 1 FROM price_write WHERE shop_id=:s", s=p.shop.shop_id) == []
        )
        assert (
            await fetch(rig, "SELECT 1 FROM price_change WHERE shop_id=:s", s=p.shop.shop_id) == []
        )


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    low=st.integers(1, 1000),
    span=st.integers(0, 1000),
    others=st.dictionaries(
        st.sampled_from(["901", "902", "903"]), st.integers(1, 3000), min_size=1, max_size=3
    ),
    new=st.integers(-5, 3005),
)
async def test_multi_city_write_sends_one_in_bounds_price_to_every_city(
    low: int,
    span: int,
    others: dict[str, int],
    new: int,
    rig: Rig,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Н12: во всех городах строки уходит одна и та же цена, не ниже min и не выше max."""
    from kaspisa.worker.writer import WriteOutcome

    high = low + span
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.price = 5000  # цена города сверки; новая цена всегда отличается
    p.client.extra_cities = others
    await execute(
        rig,
        "UPDATE product SET min_price=:lo, max_price=:hi WHERE id=:p",
        lo=low,
        hi=high,
        p=p.product_id,
    )
    result = await p.write(new, 5000, product=await p.product(), decision=decision(new))
    if low <= new <= high:
        assert result.outcome is WriteOutcome.SENT
        (_, prices) = p.client.sent[0]
        assert set(prices) == {"750000000", *others}
        assert set(prices.values()) == {new}
    else:
        assert result.outcome is WriteOutcome.BLOCKED_BOUNDS
        assert p.client.sent == []


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    own=st.integers(1, 10_000),
    others=st.lists(st.integers(1, 10_000), max_size=12),
    own_in_card=st.booleans(),
    listing=st.booleans(),
    excluded=st.sets(st.integers(0, 11), max_size=4),
    bounds=st.sampled_from([(None, None), (100, None), (None, 5000)]),
)
async def test_unbounded_products_are_never_written(
    own: int,
    others: list[int],
    own_in_card: bool,
    listing: bool,
    excluded: set[int],
    bounds: tuple[int | None, int | None],
    rig: Rig,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Т12: без границ цена не уходит ни в каком исходе (режим «работает», запись разрешена)."""
    from kaspisa.kaspi.models import Offer, OffersPage
    from kaspisa.worker.cycle import check_product

    p = await prepare(rig, redis_client, redis_prefix, dry_run=False)
    p.client.price = own
    if not listing:  # строки товара в кабинете нет: пропуск `listing_missing`
        p.client.find_results = [None]
    offers = [
        Offer.model_validate({"merchantId": f"c{i}", "merchantName": f"c{i}", "price": price})
        for i, price in enumerate(others)
    ]
    if own_in_card:
        offers.insert(
            0,
            Offer.model_validate(
                {"merchantId": p.shop.merchant_uid, "merchantName": "own", "price": own}
            ),
        )
    p.client.offers_page = OffersPage(offers=offers, total=len(offers))
    await execute(
        rig,
        "UPDATE product SET min_price=:lo, max_price=:hi WHERE id=:p",
        lo=bounds[0],
        hi=bounds[1],
        p=p.product_id,
    )
    await execute(rig, "DELETE FROM price_change WHERE shop_id=:s", s=p.shop.shop_id)
    await execute(
        rig,
        "UPDATE shop SET bot_mode='live', claimed_by='w1', claimed_until=:u WHERE id=:s",
        u=NOW.replace(year=2040),
        s=p.shop.shop_id,
    )
    p.sctx.access.shop.bot_mode = "live"
    p.sctx.shop.excluded_merchant_ids = [f"c{i}" for i in excluded]
    await check_product(p.sctx, await p.product())
    assert p.client.sent == []
    assert "set_price" not in p.client.methods()
    results = {row[3] for row in await journal(rig, p.shop.shop_id)}
    assert not results & {"applied", "dry_run", "failed", "unconfirmed"}
    row = (
        await fetch(
            rig,
            "SELECT decision_write, decision_reason, skip_reason FROM product WHERE id=:p",
            p=p.product_id,
        )
    )[0]
    assert row[0] is None
    assert row[1] == "bounds_missing" or row[2] is not None
