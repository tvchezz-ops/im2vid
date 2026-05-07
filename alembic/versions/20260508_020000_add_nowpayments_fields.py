"""Add NOWPayments fields

Revision ID: 20260508_020000
Revises: 20260508_010000
Create Date: 2026-05-08 02:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260508_020000"
down_revision: Union[str, None] = "20260508_010000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("payment_orders", sa.Column("nowpayments_payment_id", sa.String(length=255), nullable=True))
    op.create_index("ix_payment_orders_nowpayments_payment_id", "payment_orders", ["nowpayments_payment_id"])

    op.add_column("crypto_payment_orders", sa.Column("nowpayments_payment_id", sa.String(length=255), nullable=True))
    op.add_column("crypto_payment_orders", sa.Column("payment_url", sa.String(length=1024), nullable=True))
    op.add_column("crypto_payment_orders", sa.Column("pay_address", sa.String(length=255), nullable=True))
    op.add_column("crypto_payment_orders", sa.Column("pay_currency", sa.String(length=50), nullable=True))
    op.add_column("crypto_payment_orders", sa.Column("price_amount", sa.String(length=100), nullable=True))
    op.add_column("crypto_payment_orders", sa.Column("price_currency", sa.String(length=20), nullable=False, server_default="usd"))
    op.add_column("crypto_payment_orders", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint(
        "uq_crypto_payment_orders_nowpayments_payment_id",
        "crypto_payment_orders",
        ["nowpayments_payment_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_crypto_payment_orders_nowpayments_payment_id", "crypto_payment_orders", type_="unique")
    op.drop_column("crypto_payment_orders", "expires_at")
    op.drop_column("crypto_payment_orders", "price_currency")
    op.drop_column("crypto_payment_orders", "price_amount")
    op.drop_column("crypto_payment_orders", "pay_currency")
    op.drop_column("crypto_payment_orders", "pay_address")
    op.drop_column("crypto_payment_orders", "payment_url")
    op.drop_column("crypto_payment_orders", "nowpayments_payment_id")
    op.drop_index("ix_payment_orders_nowpayments_payment_id", table_name="payment_orders")
    op.drop_column("payment_orders", "nowpayments_payment_id")