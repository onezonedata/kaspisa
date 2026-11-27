"""Отметка сброса остатка для уведомления в Telegram (ADR 0038).

Upgrade: два столбца `product` (время и точки сброса), ограничение «оба или ни одного» и
частичный индекс для опроса. Строки не заполняются: о товарах, выключенных до миграции,
сообщений нет.
Downgrade: удаляет индекс, ограничение и столбцы; ждущие сообщения теряются.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product", sa.Column("stock_reset_at", sa.DateTime(timezone=True)))
    op.add_column("product", sa.Column("stock_reset_points", sa.Text()))
    op.create_check_constraint(
        "stock_reset_with_points",
        "product",
        "(stock_reset_at IS NULL) = (stock_reset_points IS NULL)",
    )
    op.create_index(
        "ix_product_stock_reset_at",
        "product",
        ["tenant_id", "stock_reset_at", "id"],
        postgresql_where=sa.text("stock_reset_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_product_stock_reset_at", table_name="product")
    op.drop_constraint("stock_reset_with_points", "product", type_="check")
    op.drop_column("product", "stock_reset_points")
    op.drop_column("product", "stock_reset_at")
