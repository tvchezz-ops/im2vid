"""Add user language code safely

Revision ID: 20260507_234500
Revises: 20260505_041500
Create Date: 2026-05-07 23:45:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_234500"
down_revision: Union[str, None] = "20260505_041500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    return column_name in columns


def upgrade() -> None:
    if _has_column("users", "language_code"):
        return

    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("language_code", sa.String(length=10), nullable=True))


def downgrade() -> None:
    if not _has_column("users", "language_code"):
        return

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("language_code")