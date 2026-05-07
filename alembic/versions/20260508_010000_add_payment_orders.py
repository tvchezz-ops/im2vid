"""Add payment orders

Revision ID: 20260508_010000
Revises: 20260507_234500
Create Date: 2026-05-08 01:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260508_010000"
down_revision: Union[str, None] = "20260507_234500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="created"),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=20), nullable=False),
        sa.Column("external_payment_id", sa.String(length=255), nullable=True),
        sa.Column("telegram_payment_charge_id", sa.String(length=255), nullable=True),
        sa.Column("payload", sa.String(length=255), nullable=True, unique=True),
        sa.Column("metadata", sa.JSON(), nullable=True, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payment_orders_provider", "payment_orders", ["provider"])
    op.create_index("ix_payment_orders_status", "payment_orders", ["status"])
    op.create_index("ix_payment_orders_user_id", "payment_orders", ["user_id"])

    op.create_table(
        "crypto_payment_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("payment_order_id", sa.Uuid(), sa.ForeignKey("payment_orders.id"), nullable=False),
        sa.Column("network", sa.String(length=100), nullable=True),
        sa.Column("asset", sa.String(length=100), nullable=True),
        sa.Column("wallet_address", sa.String(length=255), nullable=True),
        sa.Column("expected_amount", sa.String(length=100), nullable=True),
        sa.Column("tx_hash", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_crypto_payment_orders_payment_order_id",
        "crypto_payment_orders",
        ["payment_order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_crypto_payment_orders_payment_order_id", table_name="crypto_payment_orders")
    op.drop_table("crypto_payment_orders")
    op.drop_index("ix_payment_orders_user_id", table_name="payment_orders")
    op.drop_index("ix_payment_orders_status", table_name="payment_orders")
    op.drop_index("ix_payment_orders_provider", table_name="payment_orders")
    op.drop_table("payment_orders")
