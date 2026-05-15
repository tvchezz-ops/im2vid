"""Set initial user balance to 30 credits

Revision ID: 20260515_140000
Revises: 20260515_130000
Create Date: 2026-05-15 14:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260515_140000"
down_revision = "20260515_130000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "balance",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default="30",
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "balance",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default="5",
        )
