"""Add referral system backend tables and fields

Revision ID: 20260515_120000
Revises: 20260508_040000
Create Date: 2026-05-15 12:00:00.000000

"""
from __future__ import annotations

import secrets
import string

from alembic import op
import sqlalchemy as sa


revision = "20260515_120000"
down_revision = "20260508_040000"
branch_labels = None
depends_on = None

_REFERRAL_CODE_ALPHABET = string.ascii_letters + string.digits


def _generate_referral_code(length: int = 8) -> str:
    return "".join(secrets.choice(_REFERRAL_CODE_ALPHABET) for _ in range(length))


def _backfill_referral_codes() -> None:
    connection = op.get_bind()
    users = connection.execute(sa.text("SELECT id FROM users WHERE referral_code IS NULL")).fetchall()
    used_codes = {
        row[0]
        for row in connection.execute(sa.text("SELECT referral_code FROM users WHERE referral_code IS NOT NULL"))
        if row[0]
    }

    for (user_id,) in users:
        # Existing users need codes too, but codes intentionally do not reveal Telegram IDs.
        referral_code = _generate_referral_code()
        while referral_code in used_codes:
            referral_code = _generate_referral_code()
        used_codes.add(referral_code)
        connection.execute(
            sa.text("UPDATE users SET referral_code = :referral_code WHERE id = :user_id"),
            {"referral_code": referral_code, "user_id": user_id},
        )


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("referral_code", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("referred_by_user_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("referred_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_users_referred_by_user_id_users",
            "users",
            ["referred_by_user_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_users_not_self_referred",
            "referred_by_user_id IS NULL OR referred_by_user_id != id",
        )

    _backfill_referral_codes()

    op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)
    op.create_index("ix_users_referred_by_user_id", "users", ["referred_by_user_id"])

    op.create_table(
        "referral_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("referrer_user_id", sa.BigInteger(), nullable=True),
        sa.Column("referred_user_id", sa.BigInteger(), nullable=False),
        sa.Column("referral_code", sa.String(length=10), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reject_reason", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["referred_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["referrer_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_referral_events_referrer_user_id", "referral_events", ["referrer_user_id"])
    op.create_index("ix_referral_events_referred_user_id", "referral_events", ["referred_user_id"])
    op.create_index("ix_referral_events_status", "referral_events", ["status"])
    op.create_index(
        "uq_referral_events_accepted_referred_user_id",
        "referral_events",
        ["referred_user_id"],
        unique=True,
        sqlite_where=sa.text("status = 'accepted'"),
        postgresql_where=sa.text("status = 'accepted'"),
    )

    op.create_table(
        "credit_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("referral_event_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["referral_event_id"], ["referral_events.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_credit_transactions_type", "credit_transactions", ["type"])
    op.create_index("ix_credit_transactions_user_id", "credit_transactions", ["user_id"])
    op.create_index("ix_credit_transactions_referral_event_id", "credit_transactions", ["referral_event_id"])
    op.create_index(
        "uq_credit_transactions_referral_bonus_event_user",
        "credit_transactions",
        ["user_id", "referral_event_id"],
        unique=True,
        sqlite_where=sa.text("type = 'referral_bonus'"),
        postgresql_where=sa.text("type = 'referral_bonus'"),
    )


def downgrade() -> None:
    op.drop_index("uq_credit_transactions_referral_bonus_event_user", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_referral_event_id", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_user_id", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_type", table_name="credit_transactions")
    op.drop_table("credit_transactions")

    op.drop_index("uq_referral_events_accepted_referred_user_id", table_name="referral_events")
    op.drop_index("ix_referral_events_status", table_name="referral_events")
    op.drop_index("ix_referral_events_referred_user_id", table_name="referral_events")
    op.drop_index("ix_referral_events_referrer_user_id", table_name="referral_events")
    op.drop_table("referral_events")

    op.drop_index("ix_users_referred_by_user_id", table_name="users")
    op.drop_index("ix_users_referral_code", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_not_self_referred", type_="check")
        batch_op.drop_constraint("fk_users_referred_by_user_id_users", type_="foreignkey")
        batch_op.drop_column("referred_at")
        batch_op.drop_column("referred_by_user_id")
        batch_op.drop_column("referral_code")
