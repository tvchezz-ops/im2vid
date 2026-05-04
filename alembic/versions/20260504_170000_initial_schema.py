"""Initial schema

Revision ID: 20260504_170000
Revises:
Create Date: 2026-05-04 17:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260504_170000"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


generation_status_enum = sa.Enum(
    "created",
    "processing",
    "completed",
    "failed",
    name="generationrequeststatus",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("language_code", sa.String(length=10), nullable=True),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_premium", sa.Boolean(), nullable=True),
        sa.Column("photo_url", sa.String(length=1024), nullable=True),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_generations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successful_generations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_generations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "generation_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("model_key", sa.String(length=100), nullable=False),
        sa.Column("model_endpoint", sa.String(length=255), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("input_image_file_ids", sa.JSON(), nullable=False),
        sa.Column("input_image_urls", sa.JSON(), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=50), nullable=True),
        sa.Column("resolution", sa.String(length=50), nullable=True),
        sa.Column("size", sa.String(length=50), nullable=True),
        sa.Column("output_format", sa.String(length=50), nullable=True),
        sa.Column("wavespeed_prediction_id", sa.String(length=255), nullable=True),
        sa.Column("status", generation_status_enum, nullable=False, server_default="created"),
        sa.Column("output_urls", sa.JSON(), nullable=False),
        sa.Column("nsfw_flags", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("cost", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "user_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_index("ix_generation_requests_model_key", "generation_requests", ["model_key"])
    op.create_index("ix_generation_requests_user_id", "generation_requests", ["user_id"])
    op.create_index(
        "ix_generation_requests_wavespeed_prediction_id",
        "generation_requests",
        ["wavespeed_prediction_id"],
        unique=False,
    )
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index("ix_payments_user_id", "payments", ["user_id"])
    op.create_index("ix_user_events_event_type", "user_events", ["event_type"])
    op.create_index("ix_user_events_user_id", "user_events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_events_user_id", table_name="user_events")
    op.drop_index("ix_user_events_event_type", table_name="user_events")
    op.drop_index("ix_payments_user_id", table_name="payments")
    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_generation_requests_wavespeed_prediction_id", table_name="generation_requests")
    op.drop_index("ix_generation_requests_user_id", table_name="generation_requests")
    op.drop_index("ix_generation_requests_model_key", table_name="generation_requests")
    op.drop_table("payments")
    op.drop_table("user_events")
    op.drop_table("generation_requests")
    op.drop_table("users")