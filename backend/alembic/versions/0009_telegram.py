"""Уведомления в Telegram (ADR 0025): настройки арендатора и отметка уведомлённой остановки.

Upgrade: таблица `telegram_settings`, столбец `shop.halt_notified_at`; остановки, бывшие до
обновления, считаются уведомлёнными (`halt_notified_at = halted_at`).
Downgrade: таблица и столбец удаляются.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("notified_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_telegram_settings_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_telegram_settings")),
    )
    op.execute(
        "CREATE TRIGGER telegram_settings_tenant_id_immutable BEFORE UPDATE OF tenant_id "
        "ON telegram_settings FOR EACH ROW EXECUTE FUNCTION forbid_tenant_change()"
    )
    op.add_column("shop", sa.Column("halt_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE shop SET halt_notified_at = halted_at WHERE halted_at IS NOT NULL")


def downgrade() -> None:
    op.drop_table("telegram_settings")  # триггер уходит вместе с таблицей
    op.drop_column("shop", "halt_notified_at")
