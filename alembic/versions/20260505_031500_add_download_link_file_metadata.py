"""Add file metadata to download links

Revision ID: 20260505_031500
Revises: 20260505_021500
Create Date: 2026-05-05 03:15:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260505_031500"
down_revision: Union[str, None] = "20260505_021500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("download_links") as batch_op:
        batch_op.add_column(sa.Column("filename", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("file_size_bytes", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("content_type", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("download_links") as batch_op:
        batch_op.drop_column("content_type")
        batch_op.drop_column("file_size_bytes")
        batch_op.drop_column("filename")