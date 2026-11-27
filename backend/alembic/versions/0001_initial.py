"""Начальная схема: арендатор, пользователь, магазин, товар, журнал цен.

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")
_IMMUTABLE_TABLES = ("app_user", "shop", "product", "price_change")


def _ts(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=_NOW if default else None,
    )


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant")),
    )

    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_app_user_email_lowercase")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_app_user_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("email", name=op.f("uq_app_user_email")),
    )
    op.create_index("ix_app_user_tenant_id", "app_user", ["tenant_id"])

    op.create_table(
        "shop",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_uid", sa.Text(), nullable=True),
        sa.Column("login_enc", sa.LargeBinary(), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("session_enc", sa.LargeBinary(), nullable=True),
        sa.Column(
            "connection_state",
            sa.String(32),
            nullable=False,
            server_default="not_connected",
        ),
        sa.Column("connection_error", sa.Text(), nullable=True),
        _ts("connection_changed_at"),
        sa.Column("connection_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("strategy", sa.String(32), nullable=False, server_default="be_first"),
        sa.Column("below_min", sa.String(32), nullable=False, server_default="do_nothing"),
        sa.Column("city_id", sa.Text(), nullable=True),
        sa.Column(
            "excluded_merchant_ids",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("default_step", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "auto_raise_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("bot_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "connection_state IN "
            "('not_connected', 'awaiting_code', 'connected', 'reauth_required')",
            name=op.f("ck_shop_connection_state"),
        ),
        sa.CheckConstraint(
            "strategy IN ('be_first', 'match_first', 'hold_second')",
            name=op.f("ck_shop_strategy"),
        ),
        sa.CheckConstraint(
            "below_min IN ('do_nothing', 'drop_to_min', 'next_place')",
            name=op.f("ck_shop_below_min"),
        ),
        sa.CheckConstraint("default_step >= 1", name=op.f("ck_shop_default_step_positive")),
        sa.CheckConstraint(
            "connection_state <> 'connected' "
            "OR (merchant_uid IS NOT NULL AND session_enc IS NOT NULL)",
            name=op.f("ck_shop_connected_requires_session"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_shop_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shop")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_shop_tenant_id_id")),
    )
    op.create_index("ix_shop_tenant_id", "shop", ["tenant_id"])
    op.create_index(
        "uq_shop_merchant_uid",
        "shop",
        ["merchant_uid"],
        unique=True,
        postgresql_where=sa.text("merchant_uid IS NOT NULL"),
    )

    op.create_table(
        "product",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("master_sku", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("auto_decrease", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("auto_raise", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("min_price", sa.Integer(), nullable=True),
        sa.Column("max_price", sa.Integer(), nullable=True),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("current_price", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("competitors_count", sa.Integer(), nullable=True),
        sa.Column("first_place_price", sa.Integer(), nullable=True),
        _ts("last_checked_at", nullable=True, default=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "min_price IS NULL OR min_price >= 1", name=op.f("ck_product_min_price_positive")
        ),
        sa.CheckConstraint(
            "max_price IS NULL OR max_price >= 1", name=op.f("ck_product_max_price_positive")
        ),
        sa.CheckConstraint(
            "min_price IS NULL OR max_price IS NULL OR min_price <= max_price",
            name=op.f("ck_product_min_not_above_max"),
        ),
        sa.CheckConstraint("step >= 1", name=op.f("ck_product_step_positive")),
        sa.CheckConstraint(
            "current_price IS NULL OR current_price >= 1",
            name=op.f("ck_product_current_price_positive"),
        ),
        sa.CheckConstraint(
            "position IS NULL OR position >= 1", name=op.f("ck_product_position_positive")
        ),
        sa.CheckConstraint(
            "competitors_count IS NULL OR competitors_count >= 0",
            name=op.f("ck_product_competitors_count_not_negative"),
        ),
        sa.CheckConstraint(
            "first_place_price IS NULL OR first_place_price >= 1",
            name=op.f("ck_product_first_place_price_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "shop_id"],
            ["shop.tenant_id", "shop.id"],
            name=op.f("fk_product_tenant_id_shop_id_shop"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product")),
        sa.UniqueConstraint("shop_id", "sku", name=op.f("uq_product_shop_id_sku")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_product_tenant_id_id")),
    )

    op.create_table(
        "price_change",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        _ts("created_at"),
        sa.Column("old_price", sa.Integer(), nullable=False),
        sa.Column("new_price", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("target_price", sa.Integer(), nullable=True),
        sa.Column("competitor_merchant_id", sa.Text(), nullable=True),
        sa.Column("competitor_name", sa.Text(), nullable=True),
        sa.Column("competitor_price", sa.Integer(), nullable=True),
        sa.Column("capped_at_max", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("skipped_competitors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("old_price >= 1", name=op.f("ck_price_change_old_price_positive")),
        sa.CheckConstraint("new_price >= 1", name=op.f("ck_price_change_new_price_positive")),
        sa.CheckConstraint("new_price <> old_price", name=op.f("ck_price_change_price_changed")),
        sa.CheckConstraint(
            "skipped_competitors >= 0",
            name=op.f("ck_price_change_skipped_competitors_not_negative"),
        ),
        sa.CheckConstraint(
            "result IN ('applied', 'dry_run', 'failed', 'unconfirmed')",
            name=op.f("ck_price_change_result"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "shop_id"],
            ["shop.tenant_id", "shop.id"],
            name=op.f("fk_price_change_tenant_id_shop_id_shop"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["product.tenant_id", "product.id"],
            name=op.f("fk_price_change_tenant_id_product_id_product"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_change")),
    )
    op.create_index(
        "ix_price_change_tenant_id_shop_id_id",
        "price_change",
        ["tenant_id", "shop_id", sa.text("id DESC")],
    )
    op.create_index(
        "ix_price_change_tenant_id_product_id_id",
        "price_change",
        ["tenant_id", "product_id", sa.text("id DESC")],
    )

    # Неизменяемость tenant_id. Текст ошибки значений не содержит.
    op.execute(
        """
        CREATE FUNCTION forbid_tenant_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
                RAISE EXCEPTION 'tenant_id is immutable'
                    USING ERRCODE = '23000', CONSTRAINT = 'tenant_id_immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in _IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_tenant_id_immutable BEFORE UPDATE OF tenant_id ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_tenant_change()"
        )


def downgrade() -> None:
    for table in _IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER {table}_tenant_id_immutable ON {table}")
    op.execute("DROP FUNCTION forbid_tenant_change()")
    op.drop_table("price_change")
    op.drop_table("product")
    op.drop_table("shop")
    op.drop_table("app_user")
    op.drop_table("tenant")
