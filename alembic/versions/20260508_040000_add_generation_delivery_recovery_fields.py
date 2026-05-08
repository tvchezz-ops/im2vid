"""Add generation delivery recovery fields

Revision ID: 20260508_040000
Revises: 20260508_030000
Create Date: 2026-05-08 04:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260508_040000"
down_revision = "20260508_030000"
branch_labels = None
depends_on = None


old_generation_status_enum = sa.Enum(
    "created",
    "processing",
    "completed",
    "cancelled",
    "timeout",
    "failed",
    name="generationrequeststatus",
    native_enum=False,
)


new_generation_status_enum = sa.Enum(
    "created",
    "pending",
    "processing",
    "completed",
    "delivery_failed",
    "cancelled",
    "timeout",
    "failed",
    name="generationrequeststatus",
    native_enum=False,
)


def upgrade() -> None:
    with op.batch_alter_table("generation_requests") as batch_op:
        batch_op.add_column(sa.Column("chat_id", sa.BigInteger(), nullable=True))
        batch_op.alter_column(
            "status",
            existing_type=old_generation_status_enum,
            type_=new_generation_status_enum,
            existing_nullable=False,
            existing_server_default="created",
        )
        batch_op.create_index("ix_generation_requests_chat_id", ["chat_id"])


def downgrade() -> None:
    with op.batch_alter_table("generation_requests") as batch_op:
        batch_op.drop_index("ix_generation_requests_chat_id")
        batch_op.alter_column(
            "status",
            existing_type=new_generation_status_enum,
            type_=old_generation_status_enum,
            existing_nullable=False,
            existing_server_default="created",
        )
        batch_op.drop_column("chat_id")