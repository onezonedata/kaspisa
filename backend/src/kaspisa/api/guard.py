"""Защита границ цены (ADR 0024): опасные правки, якорь минимума, подтверждение паролем.

Правила - чистые функции на целых числах. Якорь минимума - минимум, от которого меряется
порог в 20%: без пароля он только растёт, поэтому цепочка мелких снижений упирается в пароль.
"""

from __future__ import annotations

from fastapi import Request

from kaspisa.api.deps import SESSION_COOKIE, ApiDeps
from kaspisa.api.errors import ApiError
from kaspisa.api.schemas import BulkChanges
from kaspisa.db import BotMode


def min_lowering_is_dangerous(
    new_min: int | None, old_min: int | None, anchor: int | None, current: int | None
) -> bool:
    """Опасно ли снижение (или установка) минимума; ровно 20% опасным не считается."""
    if new_min is None:
        return False  # минимум стёрт: без обеих границ бот товар не трогает
    if old_min is not None and new_min >= old_min:
        return False  # повышение и повтор прежнего значения
    if anchor is None and current is None:
        return True  # опор нет: нельзя доказать, что снижение меньше 20%
    if anchor is not None and new_min * 100 < anchor * 80:
        return True
    return current is not None and new_min * 100 < current * 80


def next_anchor(new_min: int | None, anchor: int | None, dangerous: bool) -> int | None:
    """Якорь после правки минимума: без пароля не опускается, стирание минимума его не трогает."""
    if new_min is None:
        return anchor
    if dangerous or anchor is None:
        return new_min
    return max(anchor, new_min)


def bulk_is_dangerous(changes: BulkChanges, bot_mode: BotMode) -> bool:
    """Массовое включение отслеживания или автоснижения опасно только в режиме `live`."""
    if bot_mode != BotMode.LIVE:
        return False
    fields = changes.model_fields_set
    return ("tracked" in fields and changes.tracked) or (
        "auto_decrease" in fields and changes.auto_decrease
    )


async def ensure_password_confirmed(request: Request, deps: ApiDeps) -> None:
    """403 `password_required`, если пароль в этой сессии не подтверждён за последние 10 минут."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not await deps.state.password_confirmed(token):
        raise ApiError(403, "password_required")
