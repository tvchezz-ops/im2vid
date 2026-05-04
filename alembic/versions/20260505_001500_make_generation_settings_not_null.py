"""Make generation settings non-null

Revision ID: 20260505_001500
Revises: 20260504_235500
Create Date: 2026-05-05 00:15:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260505_001500"
down_revision: Union[str, None] = "20260504_235500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE generation_requests SET settings = '{}' WHERE settings IS NULL"))
    with op.batch_alter_table("generation_requests") as batch_op:
        batch_op.alter_column(
            "settings",
            existing_type=sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_requests") as batch_op:
        batch_op.alter_column(
            "settings",
            existing_type=sa.JSON(),
            nullable=True,
            server_default=None,
        )