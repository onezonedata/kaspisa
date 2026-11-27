"""Т28: pricing_settings_for собирает настройки движка из модели без запросов к базе."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from kaspisa.db import Product, Shop, pricing_settings_for
from kaspisa.pricing.engine import decide_price
from kaspisa.pricing.types import Offer, PricingSettings, Strategy


def _shop(**overrides: object) -> Shop:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "merchant_uid": "my-shop",
        "strategy": "hold_second",
        "excluded_merchant_ids": ["bad-1", "bad-2"],
        "default_step": 5,
        "auto_raise_enabled": True,
    }
    values.update(overrides)
    return Shop(**values)


def _product(**overrides: object) -> Product:
    values: dict[str, object] = {
        "sku": "s",
        "master_sku": "m",
        "title": "t",
        "auto_decrease": True,
        "auto_raise": False,
        "min_price": 900,
        "max_price": 1500,
        "step": 7,
    }
    values.update(overrides)
    return Product(**values)


def test_all_fields_come_from_the_right_columns() -> None:
    settings = pricing_settings_for(_shop(), _product())
    assert settings == PricingSettings(
        strategy=Strategy.HOLD_SECOND,
        auto_decrease=True,
        auto_raise=False,
        shop_auto_raise=True,
        min_price=900,
        max_price=1500,
        step=7,
        own_merchant_id="my-shop",
        excluded_merchant_ids=frozenset({"bad-1", "bad-2"}),
    )


def test_auto_raise_flags_are_independent() -> None:
    both = pricing_settings_for(_shop(auto_raise_enabled=False), _product(auto_raise=True))
    assert both is not None
    assert both.auto_raise is True
    assert both.shop_auto_raise is False


@pytest.mark.parametrize(
    ("shop", "product"),
    [
        (_shop(), _product(min_price=None)),
        (_shop(), _product(max_price=None)),
        (_shop(), _product(min_price=None, max_price=None)),
        (_shop(merchant_uid=None), _product()),
    ],
)
def test_none_without_bounds_or_merchant_uid(shop: Shop, product: Product) -> None:
    assert pricing_settings_for(shop, product) is None


def test_result_is_accepted_by_engine() -> None:
    settings = pricing_settings_for(_shop(), _product())
    assert settings is not None
    offers = [Offer("my-shop", 1200), Offer("rival", 1100)]
    decision = decide_price(1200, offers, settings)
    assert decision.new_price is None or 900 <= decision.new_price <= 1500
    # Граничный случай: границы совпадают.
    pinned = replace(settings, min_price=1000, max_price=1000)
    assert decide_price(1200, offers, pinned).new_price in (None, 1000)
