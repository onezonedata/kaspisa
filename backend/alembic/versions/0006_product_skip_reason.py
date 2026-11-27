"""Причина пропуска последней проверки товара (ADR 0017).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product", sa.Column("skip_reason", sa.Text()))
    op.add_column("product", sa.Column("skipped_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "skip_reason_with_time", "product", "(skip_reason IS NULL) = (skipped_at IS NULL)"
    )


def downgrade() -> None:
    op.drop_constraint("skip_reason_with_time", "product", type_="check")
    op.drop_column("product", "skipped_at")
    op.drop_column("product", "skip_reason")
