"""Add opaque user start payloads

Revision ID: 20260515_130000
Revises: 20260515_120000
Create Date: 2026-05-15 13:00:00.000000

"""
from __future__ import annotations

import secrets
import string

from alembic import op
import sqlalchemy as sa


revision = "20260515_130000"
down_revision = "20260515_120000"
branch_labels = None
depends_on = None

_START_PAYLOAD_ALPHABET = string.ascii_letters + string.digits


def _generate_start_payload(length: int = 12) -> str:
    return "".join(secrets.choice(_START_PAYLOAD_ALPHABET) for _ in range(length))


def _backfill_start_payloads() -> None:
    connection = op.get_bind()
    users = connection.execute(sa.text("SELECT id FROM users WHERE start_payload IS NULL")).fetchall()
    used_payloads = {
        row[0]
        for row in connection.execute(sa.text("SELECT start_payload FROM users WHERE start_payload IS NOT NULL"))
        if row[0]
    }

    for (user_id,) in users:
        start_payload = _generate_start_payload()
        while start_payload in used_payloads:
            start_payload = _generate_start_payload()
        used_payloads.add(start_payload)
        connection.execute(
            sa.text("UPDATE users SET start_payload = :start_payload WHERE id = :user_id"),
            {"start_payload": start_payload, "user_id": user_id},
        )


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("start_payload", sa.String(length=24), nullable=True))

    _backfill_start_payloads()
    op.create_index("ix_users_start_payload", "users", ["start_payload"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_start_payload", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("start_payload")
