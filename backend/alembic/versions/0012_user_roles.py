"""Роль администратора и автор события по адресу (ADR 0039).

Upgrade: `app_user.is_admin` (администратором становится первый пользователь арендатора по
`(created_at, id)`, выбранные адреса пишутся в журнал alembic), частичный уникальный индекс «не
больше одного администратора в арендаторе»; `owner_event.user_id` допускает NULL, внешний ключ
переходит на `ON DELETE SET NULL`; `owner_event.user_email` - снимок адреса автора; в CHECK вида
события добавляются `user_added` и `user_removed`.
Downgrade: удаляет строки `owner_event` новых видов и строки с `user_id IS NULL` (история
удалённых пользователей при откате теряется; число строк печатается в журнал alembic), затем
возвращает схему к 0011. Откат - ручная операция.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-27
"""

import sys
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = "'login', 'password_confirm_failed', 'bot_mode_changed', 'bounds_changed'"
_NEW_KINDS = _OLD_KINDS + ", 'user_added', 'user_removed'"
_FK = "fk_owner_event_user_id_app_user"


def _recreate_kind_check(kinds: str) -> None:
    # имя «kind» расширяется соглашением до ck_owner_event_kind
    op.drop_constraint("kind", "owner_event", type_="check")
    op.create_check_constraint("kind", "owner_event", f"kind IN ({kinds})")


def _recreate_user_fk(ondelete: str) -> None:
    op.drop_constraint(_FK, "owner_event", type_="foreignkey")
    op.create_foreign_key(_FK, "owner_event", "app_user", ["user_id"], ["id"], ondelete=ondelete)


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "app_user",
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    bind.exec_driver_sql(
        "UPDATE app_user SET is_admin = true WHERE id IN ("
        "SELECT DISTINCT ON (tenant_id) id FROM app_user ORDER BY tenant_id, created_at, id)"
    )
    op.create_index(
        "uq_app_user_tenant_id_admin",
        "app_user",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_admin"),
    )
    op.alter_column("owner_event", "user_id", nullable=True)
    _recreate_user_fk("SET NULL")
    _recreate_kind_check(_NEW_KINDS)
    op.add_column("owner_event", sa.Column("user_email", sa.Text()))
    bind.exec_driver_sql(
        "UPDATE owner_event e SET user_email = u.email FROM app_user u WHERE u.id = e.user_id"
    )
    op.alter_column("owner_event", "user_email", nullable=False)
    for tenant_id, email in bind.exec_driver_sql(
        "SELECT tenant_id, email FROM app_user WHERE is_admin ORDER BY tenant_id"
    ):
        # print, а не logger: у логгера `alembic` стоит NullHandler, сообщение бы потерялось.
        print(f"администратор арендатора {tenant_id}: {email}", file=sys.stderr)


def downgrade() -> None:
    bind = op.get_bind()
    doomed = bind.exec_driver_sql(
        "SELECT count(*) FROM owner_event "
        "WHERE user_id IS NULL OR kind IN ('user_added', 'user_removed')"
    ).scalar_one()
    print(f"будет удалено строк owner_event: {doomed}", file=sys.stderr)
    bind.exec_driver_sql(
        "DELETE FROM owner_event WHERE user_id IS NULL OR kind IN ('user_added', 'user_removed')"
    )
    op.drop_column("owner_event", "user_email")
    op.alter_column("owner_event", "user_id", nullable=False)
    _recreate_user_fk("CASCADE")
    _recreate_kind_check(_OLD_KINDS)
    op.drop_index("uq_app_user_tenant_id_admin", table_name="app_user")
    op.drop_column("app_user", "is_admin")
