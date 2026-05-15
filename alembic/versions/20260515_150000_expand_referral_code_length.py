"""Expand referral code columns

Revision ID: 20260515_150000
Revises: 20260515_140000
Create Date: 2026-05-15 15:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260515_150000"
down_revision = "20260515_140000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "referral_code",
            existing_type=sa.String(length=10),
            type_=sa.String(length=64),
            existing_nullable=True,
        )

    with op.batch_alter_table("referral_events") as batch_op:
        batch_op.alter_column(
            "referral_code",
            existing_type=sa.String(length=10),
            type_=sa.String(length=64),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("referral_events") as batch_op:
        batch_op.alter_column(
            "referral_code",
            existing_type=sa.String(length=64),
            type_=sa.String(length=10),
            existing_nullable=True,
        )

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "referral_code",
            existing_type=sa.String(length=64),
            type_=sa.String(length=10),
            existing_nullable=True,
        )
