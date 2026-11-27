"""Режимы бота, якорь минимума и журнал действий владельца (ADR 0024).

Upgrade: `shop.bot_enabled` -> `shop.bot_mode` (`true` -> `dry_run`, `false` -> `off`),
`product.min_price_anchor` = `min_price`, таблица `owner_event`.
Downgrade: `bot_enabled = (bot_mode = 'live')` - откат не включает запись у магазинов,
которые её не делали; якорь и журнал владельца удаляются.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODES = "bot_mode IN ('off', 'dry_run', 'live')"
_KINDS = "kind IN ('login', 'password_confirm_failed', 'bot_mode_changed', 'bounds_changed')"


def upgrade() -> None:
    op.add_column(
        "shop", sa.Column("bot_mode", sa.String(16), nullable=False, server_default="off")
    )
    op.create_check_constraint("bot_mode", "shop", _MODES)
    op.execute("UPDATE shop SET bot_mode = CASE WHEN bot_enabled THEN 'dry_run' ELSE 'off' END")
    op.drop_column("shop", "bot_enabled")

    op.add_column("product", sa.Column("min_price_anchor", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "min_price_anchor_positive",
        "product",
        "min_price_anchor IS NULL OR min_price_anchor >= 1",
    )
    op.execute("UPDATE product SET min_price_anchor = min_price")

    op.create_table(
        "owner_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ip", sa.Text(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=True),
        sa.Column("sku", sa.Text(), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_KINDS, name=op.f("ck_owner_event_kind")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_owner_event_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_owner_event_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_owner_event")),
    )
    op.create_index(
        "ix_owner_event_tenant_id_created_at", "owner_event", ["tenant_id", "created_at"]
    )
    op.execute(
        "CREATE TRIGGER owner_event_tenant_id_immutable BEFORE UPDATE OF tenant_id "
        "ON owner_event FOR EACH ROW EXECUTE FUNCTION forbid_tenant_change()"
    )


def downgrade() -> None:
    op.drop_table("owner_event")  # триггер и индекс уходят вместе с таблицей

    op.drop_constraint(op.f("ck_product_min_price_anchor_positive"), "product", type_="check")
    op.drop_column("product", "min_price_anchor")

    op.add_column(
        "shop",
        sa.Column("bot_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE shop SET bot_enabled = (bot_mode = 'live')")
    op.drop_constraint(op.f("ck_shop_bot_mode"), "shop", type_="check")
    op.drop_column("shop", "bot_mode")
