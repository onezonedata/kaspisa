"""ADR 0024, Т16, Т16а: чистые правила опасной правки границ и якоря минимума."""

import pytest

from kaspisa.api.guard import bulk_is_dangerous, min_lowering_is_dangerous, next_anchor
from kaspisa.api.schemas import BulkChanges
from kaspisa.db import BotMode


@pytest.mark.parametrize(
    ("new", "old", "anchor", "current", "dangerous"),
    [
        (800, 1000, 1000, None, False),  # ровно 20% от якоря: не опасно
        (799, 1000, 1000, None, True),
        (900, 1000, 1000, 1200, True),  # 90 000 < 96 000: ниже текущей цены больше чем на 20%
        (800, None, None, 1000, False),
        (799, None, None, 1000, True),
        (500, None, None, None, True),  # опор нет (Н2)
        (None, 1000, 1000, 1000, False),  # минимум стёрт
        (1100, 1000, 1000, 1000, False),  # повышение
        (600, 500, 500, 1000, False),  # повышение, даже ниже цены больше чем на 20% (Н18)
        (1000, 1000, 1000, 2000, False),  # повтор прежнего значения
        (999, 1000, 1000, 2000, True),  # снижение, и оно ниже цены больше чем на 20%
        (700, None, 1000, None, True),  # минимум стёрт ранее, якорь 1000: 70 000 < 80 000
        (900, None, 1000, None, False),  # задан заново в пределах 20% от якоря
    ],
)
def test_min_lowering_is_dangerous(
    new: int | None, old: int | None, anchor: int | None, current: int | None, dangerous: bool
) -> None:
    assert min_lowering_is_dangerous(new, old, anchor, current) is dangerous


@pytest.mark.parametrize(
    ("new", "anchor", "flag", "expected"),
    [
        (None, 1000, False, 1000),  # стирание минимума якорь не трогает
        (700, 1000, True, 700),  # правка с паролем ставит якорь равным новому минимуму
        (900, None, False, 900),
        (900, 1000, False, 1000),  # без пароля якорь не опускается
        (1200, 1000, False, 1200),  # и растёт
    ],
)
def test_next_anchor(new: int | None, anchor: int | None, flag: bool, expected: int) -> None:
    assert next_anchor(new, anchor, flag) == expected


@pytest.mark.parametrize(
    ("changes", "mode", "dangerous"),
    [
        ({"tracked": True}, BotMode.LIVE, True),
        ({"auto_decrease": True}, BotMode.LIVE, True),
        ({"tracked": False, "step": 5}, BotMode.LIVE, False),
        ({"auto_raise": True}, BotMode.LIVE, False),
        ({"tracked": True, "auto_decrease": True}, BotMode.OFF, False),
        ({"tracked": True, "auto_decrease": True}, BotMode.DRY_RUN, False),
    ],
)
def test_bulk_is_dangerous(changes: dict[str, object], mode: BotMode, dangerous: bool) -> None:
    assert bulk_is_dangerous(BulkChanges.model_validate(changes), mode) is dangerous
