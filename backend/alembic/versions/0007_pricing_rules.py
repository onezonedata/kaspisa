"""Новые правила цен: решение товара, строки журнала `no_change`, без `shop.below_min` (ADR 0020).

Upgrade удаляет `below_min` без перевода значений и `bot_enabled` не меняет (ADR 0021).
Downgrade различие `do_nothing` / `next_place` / `drop_to_min` не восстанавливает (у всех
`do_nothing`), `bot_enabled` не меняет и удаляет строки журнала `no_change`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESULTS_OLD = "result IN ('applied', 'dry_run', 'failed', 'unconfirmed')"
_RESULTS_NEW = "result IN ('applied', 'dry_run', 'failed', 'unconfirmed', 'no_change')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_shop_below_min"), "shop", type_="check")
    op.drop_column("shop", "below_min")

    op.add_column("product", sa.Column("decision_reason", sa.Text()))
    op.add_column("product", sa.Column("decision_target_price", sa.Integer()))
    op.add_column("product", sa.Column("decision_write", sa.Text()))
    op.create_check_constraint(
        "skip_or_decision", "product", "skip_reason IS NULL OR decision_reason IS NULL"
    )
    op.create_check_constraint(
        "decision_target_with_reason",
        "product",
        "decision_target_price IS NULL OR decision_reason IS NOT NULL",
    )
    op.create_check_constraint(
        "decision_write_with_reason",
        "product",
        "decision_write IS NULL OR decision_reason IS NOT NULL",
    )
    op.create_check_constraint(
        "decision_target_price_positive",
        "product",
        "decision_target_price IS NULL OR decision_target_price >= 1",
    )

    op.drop_constraint(op.f("ck_price_change_result"), "price_change", type_="check")
    op.create_check_constraint("result", "price_change", _RESULTS_NEW)
    op.drop_constraint(op.f("ck_price_change_price_changed"), "price_change", type_="check")
    op.create_check_constraint(
        "price_changed", "price_change", "(result = 'no_change') = (new_price = old_price)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM price_change WHERE result = 'no_change'")
    op.drop_constraint(op.f("ck_price_change_price_changed"), "price_change", type_="check")
    op.create_check_constraint("price_changed", "price_change", "new_price <> old_price")
    op.drop_constraint(op.f("ck_price_change_result"), "price_change", type_="check")
    op.create_check_constraint("result", "price_change", _RESULTS_OLD)

    for name in (
        "decision_target_price_positive",
        "decision_write_with_reason",
        "decision_target_with_reason",
        "skip_or_decision",
    ):
        op.drop_constraint(op.f(f"ck_product_{name}"), "product", type_="check")
    op.drop_column("product", "decision_write")
    op.drop_column("product", "decision_target_price")
    op.drop_column("product", "decision_reason")

    op.add_column(
        "shop",
        sa.Column("below_min", sa.String(32), nullable=False, server_default="do_nothing"),
    )
    op.create_check_constraint(
        "below_min", "shop", "below_min IN ('do_nothing', 'drop_to_min', 'next_place')"
    )
