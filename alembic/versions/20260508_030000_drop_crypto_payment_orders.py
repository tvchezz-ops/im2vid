"""drop crypto payment orders

Revision ID: 20260508_030000
Revises: 20260508_020000
Create Date: 2026-05-08 03:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260508_030000"
down_revision = "20260508_020000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("crypto_payment_orders")


def downgrade() -> None:
    op.create_table(
        "crypto_payment_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("payment_order_id", sa.Uuid(), sa.ForeignKey("payment_orders.id"), nullable=False),
        sa.Column("network", sa.String(length=100), nullable=True),
        sa.Column("asset", sa.String(length=100), nullable=True),
        sa.Column("wallet_address", sa.String(length=255), nullable=True),
        sa.Column("expected_amount", sa.String(length=100), nullable=True),
        sa.Column("nowpayments_payment_id", sa.String(length=255), nullable=True),
        sa.Column("payment_url", sa.String(length=1024), nullable=True),
        sa.Column("pay_address", sa.String(length=255), nullable=True),
        sa.Column("pay_currency", sa.String(length=50), nullable=True),
        sa.Column("price_amount", sa.String(length=100), nullable=True),
        sa.Column("price_currency", sa.String(length=20), nullable=False, server_default="usd"),
        sa.Column("tx_hash", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_crypto_payment_orders_payment_order_id", "crypto_payment_orders", ["payment_order_id"])
    op.create_unique_constraint(
        "uq_crypto_payment_orders_nowpayments_payment_id",
        "crypto_payment_orders",
        ["nowpayments_payment_id"],
    )
