"""Add timeout generation status

Revision ID: 20260505_003500
Revises: 20260505_001500
Create Date: 2026-05-05 00:35:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260505_003500"
down_revision: Union[str, None] = "20260505_001500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


old_generation_status_enum = sa.Enum(
    "created",
    "processing",
    "completed",
    "cancelled",
    "failed",
    name="generationrequeststatus",
    native_enum=False,
)


new_generation_status_enum = sa.Enum(
    "created",
    "processing",
    "completed",
    "cancelled",
    "timeout",
    "failed",
    name="generationrequeststatus",
    native_enum=False,
)


def upgrade() -> None:
    with op.batch_alter_table("generation_requests") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=old_generation_status_enum,
            type_=new_generation_status_enum,
            existing_nullable=False,
            existing_server_default="created",
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_requests") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=new_generation_status_enum,
            type_=old_generation_status_enum,
            existing_nullable=False,
            existing_server_default="created",
        )