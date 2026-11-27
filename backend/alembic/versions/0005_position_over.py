"""Позиция «N+»: своего магазина нет среди прочитанных страниц карточки (ADR 0016).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product", sa.Column("position_over", sa.Integer()))
    op.create_check_constraint(
        "position_over_positive", "product", "position_over IS NULL OR position_over >= 1"
    )
    op.create_check_constraint(
        "position_or_over", "product", "position IS NULL OR position_over IS NULL"
    )


def downgrade() -> None:
    op.drop_constraint("position_or_over", "product", type_="check")
    op.drop_constraint("position_over_positive", "product", type_="check")
    op.drop_column("product", "position_over")
