"""Add download links table

Revision ID: 20260505_021500
Revises: 20260505_003500
Create Date: 2026-05-05 02:15:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260505_021500"
down_revision: Union[str, None] = "20260505_003500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "download_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("r2_object_key", sa.String(length=1024), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("used_count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index(op.f("ix_download_links_expires_at"), "download_links", ["expires_at"], unique=False)
    op.create_index(op.f("ix_download_links_token"), "download_links", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_download_links_token"), table_name="download_links")
    op.drop_index(op.f("ix_download_links_expires_at"), table_name="download_links")
    op.drop_table("download_links")