"""Add settings column to generation requests

Revision ID: 20260504_223000
Revises: 20260504_170000
Create Date: 2026-05-04 22:30:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260504_223000"
down_revision: Union[str, None] = "20260504_170000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("generation_requests", sa.Column("settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("generation_requests", "settings")