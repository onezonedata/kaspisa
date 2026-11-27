"""Один магазин на установку (ADR 0029): удаление магазинов, ни разу не подключавшихся.

Upgrade: удаляет магазины без `merchant_uid` и без товаров (в интерфейсе это «Магазин без
подключения»). Схема не меняется. Число удалённых строк пишется в журнал без идентификаторов.
Downgrade: ничего не делает, удалённые магазины не восстанавливаются (только из копии базы).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-21
"""

import logging
from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    result = op.get_bind().exec_driver_sql(
        "DELETE FROM shop s WHERE s.merchant_uid IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM product p WHERE p.shop_id = s.id)"
    )
    logger.info("removed_unconnected_shops count=%d", result.rowcount)


def downgrade() -> None:
    """Удалённые магазины не восстанавливаются."""
