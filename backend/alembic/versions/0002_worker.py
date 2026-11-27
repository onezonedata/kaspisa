"""Обработчик: аренда и остановка магазина, отслеживание товара, таблица price_write.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "('sending', 'receipt', 'unknown', 'confirmed', 'expired')"


def _ts(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    # --- магазин ---
    op.add_column("shop", sa.Column("claimed_by", sa.Text(), nullable=True))
    op.add_column("shop", _ts("claimed_until"))
    op.add_column("shop", sa.Column("halt_reason", sa.Text(), nullable=True))
    op.add_column("shop", _ts("halted_at"))
    op.add_column(
        "shop", sa.Column("login_error_streak", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "shop", sa.Column("write_error_streak", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("shop", sa.Column("unproven_login_version", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_shop_halt_both_or_none"), "shop", "(halt_reason IS NULL) = (halted_at IS NULL)"
    )
    op.create_check_constraint(
        op.f("ck_shop_login_error_streak_not_negative"), "shop", "login_error_streak >= 0"
    )
    op.create_check_constraint(
        op.f("ck_shop_write_error_streak_not_negative"), "shop", "write_error_streak >= 0"
    )
    op.create_index("ix_shop_claimed_until", "shop", ["claimed_until"])

    # --- товар ---
    op.add_column(
        "product",
        sa.Column("tracked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("product", sa.Column("refusal_reason", sa.Text(), nullable=True))
    op.add_column("product", _ts("refusal_since"))
    op.add_column("product", sa.Column("refusal_fingerprint", sa.Text(), nullable=True))
    op.add_column("product", _ts("retry_at"))
    op.create_index(
        "ix_product_shop_id_last_checked_at",
        "product",
        ["shop_id", "last_checked_at"],
        postgresql_where=sa.text("tracked"),
    )

    # --- журнал: составной внешний ключ price_write требует уникальности (tenant_id, id) ---
    op.create_unique_constraint(
        op.f("uq_price_change_tenant_id_id"), "price_change", ["tenant_id", "id"]
    )

    # --- запрет повторной записи ---
    op.create_table(
        "price_write",
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("expected_prices", postgresql.JSONB(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=True),
        _ts("next_verify_at"),
        sa.Column(
            "deadline_reported", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("error_counted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("price_change_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(f"status IN {_STATUSES}", name=op.f("ck_price_write_status")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["product.tenant_id", "product.id"],
            name=op.f("fk_price_write_tenant_id_product_id_product"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "shop_id"],
            ["shop.tenant_id", "shop.id"],
            name=op.f("fk_price_write_tenant_id_shop_id_shop"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "price_change_id"],
            ["price_change.tenant_id", "price_change.id"],
            name=op.f("fk_price_write_tenant_id_price_change_id_price_change"),
        ),
        sa.PrimaryKeyConstraint("product_id", name=op.f("pk_price_write")),
    )
    op.create_index(
        "ix_price_write_tenant_id_shop_id_next_verify_at",
        "price_write",
        ["tenant_id", "shop_id", "next_verify_at"],
    )
    op.execute(
        "CREATE TRIGGER price_write_tenant_id_immutable BEFORE UPDATE OF tenant_id "
        "ON price_write FOR EACH ROW EXECUTE FUNCTION forbid_tenant_change()"
    )


def downgrade() -> None:
    op.drop_table("price_write")  # триггер и индекс уходят вместе с таблицей
    op.drop_constraint(op.f("uq_price_change_tenant_id_id"), "price_change", type_="unique")
    op.drop_index("ix_product_shop_id_last_checked_at", table_name="product")
    for column in ("retry_at", "refusal_fingerprint", "refusal_since", "refusal_reason", "tracked"):
        op.drop_column("product", column)
    op.drop_index("ix_shop_claimed_until", table_name="shop")
    for name in (
        "ck_shop_write_error_streak_not_negative",
        "ck_shop_login_error_streak_not_negative",
        "ck_shop_halt_both_or_none",
    ):
        op.drop_constraint(op.f(name), "shop", type_="check")
    for column in (
        "unproven_login_version",
        "write_error_streak",
        "login_error_streak",
        "halted_at",
        "halt_reason",
        "claimed_until",
        "claimed_by",
    ):
        op.drop_column("shop", column)
