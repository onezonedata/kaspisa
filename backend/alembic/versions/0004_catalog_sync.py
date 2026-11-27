"""Суточная загрузка каталога обработчиком: метки загрузки и паузы (ADR 0010, Н7).

`catalog_synced_at` - последняя успешная загрузка (кнопкой или обработчиком);
`catalog_retry_at` - до этого срока обработчик не повторяет неудачную загрузку.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shop", sa.Column("catalog_synced_at", sa.DateTime(timezone=True)))
    op.add_column("shop", sa.Column("catalog_retry_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("shop", "catalog_retry_at")
    op.drop_column("shop", "catalog_synced_at")
